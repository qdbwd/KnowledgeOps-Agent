import { useEffect, useState } from 'react';
import { useResource } from './api';

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
};

/** 展示最近一次主模型输入。只在运行期间轮询，切换会话由useResource取消旧请求。 */
export function ContextUsage({
  conversationId,
  active,
  version,
}: {
  conversationId: string;
  active: boolean;
  version: number;
}) {
  const [tick, setTick] = useState(0);
  const { data, error } = useResource<Usage>(
    `/api/conversations/${conversationId}/context-usage`,
    `${version}:${tick}`,
  );
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setTick((value) => value + 1), 2000);
    return () => window.clearInterval(timer);
  }, [active, conversationId]);
  const known = !error && typeof data?.percentage === 'number' && Number.isFinite(data.percentage);
  const format = (value: number) => value.toLocaleString('en-US');
  return (
    <details className="context-usage">
      <summary>上下文 {known ? `${data!.percentage}%` : '—'}</summary>
      <div className="context-usage-details">
        <p>
          {known
            ? `${data!.basis === 'after_turn_compaction_estimate' ? '轮末压缩后的工作上下文' : '最近一次模型输入'}：${format(data!.input_tokens!)} / ${format(data!.working_window)} tokens（${data!.estimated ? '本地估计' : '供应商用量'}）。`
            : error
              ? `暂时无法读取：${error}`
              : data?.unavailable_reason === 'legacy_byte_estimate'
                ? '旧记录只有字节估算，无法显示可靠的 token 百分比。'
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
      </div>
    </details>
  );
}
