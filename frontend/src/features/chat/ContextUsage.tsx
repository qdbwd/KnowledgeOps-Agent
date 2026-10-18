import { Button } from '../../shared/ui/Button';
import { useEffect, useRef, useState } from 'react';
import { messageOf, send, terminal, useResource } from '../../shared/api/api';
import type { Run } from '../../shared/types/types';

type Usage = {
  basis?: string;
  input_tokens: number | null;
  model_window: number;
  working_window: number;
  percentage: number | null;
  estimated: boolean;
  output_reserve: number;
  framing_reserve: number;
  summary_trigger_tokens: number;
  summary_trigger_basis?: string;
  unavailable_reason?: string;
  components?: {
    system_rules: number;
    required_background: number;
    optional_background: number;
    history: number;
    tool_schemas: number;
    protocol_base: number;
  } | null;
};

/** 展示最近一次主模型输入。只在运行期间轮询，切换会话由useResource取消旧请求。 */
export function ContextUsage({
  conversationId,
  active,
  version,
  onChanged,
}: {
  conversationId: string;
  active: boolean;
  version: number;
  onChanged?: () => void;
}) {
  const [tick, setTick] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [actionError, setActionError] = useState('');
  const key = useRef('');
  const watched = useRef('');
  const details = useRef<HTMLDetailsElement>(null);
  const control = useResource<{ available: boolean; reason: string; run: Run | null }>(
    `/api/conversations/${conversationId}/context-compaction`,
    `${version}:${tick}`,
  );
  const compacting = Boolean(control.data?.run && !terminal(control.data.run.status));
  const { data, error } = useResource<Usage>(
    `/api/conversations/${conversationId}/context-usage`,
    `${version}:${tick}`,
  );
  useEffect(() => {
    if (!active && !accepted && !compacting) return;
    const timer = window.setInterval(() => setTick((value) => value + 1), 2000);
    return () => window.clearInterval(timer);
  }, [active, accepted, compacting, conversationId]);
  useEffect(() => {
    const run = control.data?.run;
    if (run && terminal(run.status) && watched.current === run.id) {
      watched.current = '';
      const frame = requestAnimationFrame(() => {
        setAccepted(false);
        onChanged?.();
      });
      return () => cancelAnimationFrame(frame);
    }
  }, [control.data, onChanged]);
  useEffect(() => {
    const close = (event: Event) => {
      if (details.current?.open && !details.current.contains(event.target as Node))
        details.current.open = false;
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && details.current?.open) {
        details.current.open = false;
        details.current.querySelector('summary')?.focus();
      }
    };
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', close);
      document.removeEventListener('keydown', escape);
    };
  }, []);
  const known = !error && typeof data?.percentage === 'number' && Number.isFinite(data.percentage);
  const usageLabel = `上下文 ${known ? `${data!.percentage}%` : '—'}`;
  // 圆环只把已测得的比例映射到弧长；无记录时保留空轨道，不伪装成已用0%。
  const progress = known ? Math.max(0, Math.min(100, data!.percentage!)) : 0;
  const format = (value: number) => value.toLocaleString('en-US');
  return (
    <details
      className="context-usage group/context static m-0 max-w-full shrink-0 text-xs"
      ref={details}
    >
      <summary
        className="flex size-[34px] cursor-pointer list-none items-center justify-center rounded-full p-0 hover:bg-[#eff3ef] group-open/context:bg-[#eff3ef] max-md:h-8 max-md:w-[30px] [&::-webkit-details-marker]:hidden"
        aria-label={usageLabel}
        title={`${usageLabel} · 点击查看详情与压缩`}
      >
        <svg
          className={`context-ring size-4.5 fill-none stroke-[2.4] ${known ? '' : 'is-unknown opacity-65'}`}
          viewBox="0 0 20 20"
          aria-hidden="true"
        >
          <circle className="context-ring-track stroke-[#dce2de]" cx="10" cy="10" r="7" />
          {known && (
            <circle
              className="context-ring-value stroke-[#617b6d]"
              cx="10"
              cy="10"
              r="7"
              pathLength="100"
              strokeDasharray={`${progress} 100`}
              transform="rotate(-90 10 10)"
            />
          )}
        </svg>
        <span className="sr-only">{usageLabel}</span>
      </summary>
      <div className="context-usage-details absolute right-3.5 bottom-[58px] z-20 max-h-[55dvh] w-[min(350px,calc(100%_-_28px))] overflow-y-auto rounded-lg border border-line bg-white px-4 py-3 leading-[1.7] whitespace-normal shadow-[0_8px_24px_#172b1920] [&_dl]:mt-2 [&_dl]:mb-0 [&_dl>div]:flex [&_dl>div]:justify-between [&_dl>div]:gap-3 [&_dd]:m-0 [&_dd]:text-right">
        <strong className="context-usage-heading mb-2 block text-[#314b3e]">
          {known ? `上下文使用量 ${data!.percentage}%` : '上下文使用量'}
        </strong>
        <p>
          {known
            ? `${data!.basis === 'manual_compaction_estimate' ? '主动压缩后的工作上下文' : data!.basis === 'after_turn_compaction_estimate' ? '轮末压缩后的工作上下文' : '最近一次模型输入'}：${format(data!.input_tokens!)} / ${format(data!.working_window)} tokens（${data!.estimated ? '本地估计' : '供应商用量'}）。`
            : error
              ? `暂时无法读取：${error}`
              : data?.unavailable_reason === 'legacy_byte_estimate'
                ? '旧记录只有字节估算，无法显示可靠的 token 百分比。'
                : data?.unavailable_reason === 'full_prompt_unavailable'
                  ? '已压缩工作历史；旧请求详情已回收，完整占用将在下次模型调用后更新。'
                  : '尚无模型输入记录。'}
        </p>
        {data?.model_window && (
          <p>
            应用工作窗口 {format(data.working_window)} tokens，模型容量 {format(data.model_window)}{' '}
            tokens。
            {data.summary_trigger_basis === 'input_tokens' ? '输入上下文' : '输入加输出预留与余量'}
            超过 {format(data.summary_trigger_tokens)} tokens 时尝试压缩；另预留输出{' '}
            {format(data.output_reserve)} 和余量 {format(data.framing_reserve)}，调用前检查总容量。
          </p>
        )}
        <p>
          含系统规则、工具说明和工作历史。轮末摘要后显示整理后的估计；新输入尚未计入。这不是会话累计
          token 消费。
        </p>
        {data?.components && (
          <div>
            <p>输入组成（本地估计；总量仍以上述实际请求计数为准）：</p>
            <dl>
              {(
                [
                  ['系统规则', data.components.system_rules],
                  ['必需任务背景', data.components.required_background],
                  ['可补读背景', data.components.optional_background],
                  ['工作对话', data.components.history],
                  ['工具定义', data.components.tool_schemas],
                  ['协议基础量', data.components.protocol_base],
                ] as const
              ).map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{format(value)} tokens</dd>
                </div>
              ))}
            </dl>
          </div>
        )}
        <div className="context-compaction-action mt-3 border-t border-line pt-3">
          <Button
            type="button"
            className="secondary button-secondary small text-xs"
            disabled={submitting || accepted || compacting || !control.data?.available}
            onClick={async () => {
              setSubmitting(true);
              setActionError('');
              key.current ||= crypto.randomUUID();
              try {
                const run = await send<Run>(
                  `/api/conversations/${conversationId}/context-compaction`,
                  { request_key: key.current },
                );
                watched.current = run.id;
                key.current = '';
                setAccepted(true);
                setTick((value) => value + 1);
                onChanged?.();
              } catch (failure) {
                setActionError(messageOf(failure));
              } finally {
                setSubmitting(false);
              }
            }}
          >
            {submitting || accepted || compacting ? '正在压缩…' : '立即压缩上下文'}
          </Button>
          <p className="muted text-muted">生成工作摘要，保留原聊天记录；会使用少量模型请求。</p>
          {control.data?.reason && <p>{control.data.reason}</p>}
          {control.data?.run && terminal(control.data.run.status) && (
            <p role="status">{control.data.run.stop_reason || control.data.run.outcome.message}</p>
          )}
          {(actionError || control.error) && <p role="alert">{actionError || control.error}</p>}
        </div>
      </div>
    </details>
  );
}
