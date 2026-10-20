import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useResource } from '../../shared/api/api';
import type { ModelRecord, Run, RunEvent } from '../../shared/types/types';
import { Button } from '../../shared/ui/Button';
import { ErrorBox, Loading } from '../../shared/ui/components';
import { RuntimeResources, type RuntimeManifest, type Continuation } from './RuntimeResources';

type Records = {
  models: ModelRecord[];
  events: RunEvent[];
  event_page: { after: number; next_after: number | null; total: number };
  artifacts: { id: string; kind: string; length: number | null; available?: boolean }[];
  resources?: RuntimeManifest | null;
  continuation?: Continuation | null;
};

/** 仅在诊断路由内挂载；离页/切换任务即取消未完成请求并释放大记录。 */
export function RunDiagnostics({ run, refreshKey }: { run: Run; refreshKey: string }) {
  const [after, setAfter] = useState(0);
  const [retry, setRetry] = useState(0);
  const records = useResource<Records>(
    run.details_available === false ? null : `/api/runs/${run.id}/records?after=${after}`,
    `${refreshKey}:${retry}`,
  );
  if (run.details_available === false) return <p role="status">诊断详情已过期或正在清理。</p>;
  return (
    <section
      id="run-diagnostics"
      className="mt-6 min-w-0 rounded-xl border border-line bg-white p-6 max-md:p-4"
      aria-label="任务诊断"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2>诊断记录</h2>
        <a className="button secondary button-secondary" href={`/api/runs/${run.id}/export`}>
          导出诊断记录
        </a>
      </div>
      <p className="text-sm text-muted">
        用于核对执行问题。完整请求、事件与恢复资料按保留期限清理，已交付文件独立保存。
      </p>
      {run.details_expires_at && (
        <p className="text-xs text-muted">
          详情保留至：{new Date(run.details_expires_at).toLocaleString()}
        </p>
      )}
      <ErrorBox message={records.error} />
      {records.error && (
        <Button className="secondary button-secondary" onClick={() => setRetry((n) => n + 1)}>
          重试加载诊断
        </Button>
      )}
      {records.loading && !records.data && <Loading />}
      <details className="my-4">
        <summary>运行标识与检查点</summary>
        <dl className="grid gap-2 text-xs [&_dd]:m-0 [&_dd]:break-all">
          <dt>运行 ID</dt>
          <dd>{run.id}</dd>
          <dt>根任务 ID</dt>
          <dd>{run.root_id}</dd>
          <dt>检查点</dt>
          <dd>{run.checkpoint_id}</dd>
          <dt>调度代次</dt>
          <dd>{run.generation}</dd>
        </dl>
      </details>
      {records.data ? (
        <RuntimeResources
          resources={records.data.resources}
          continuation={records.data.continuation}
        />
      ) : (
        <p className="text-sm text-muted">
          {records.error ? '运行依据暂不可用。' : '正在加载运行依据…'}
        </p>
      )}
      <h3>
        模型调用{' '}
        {records.loading
          ? records.data
            ? '更新中…'
            : '加载中…'
          : records.data
            ? `${records.data.models.length} 次`
            : '记录暂不可用'}
      </h3>
      {records.data?.models.map((record, index) => (
        <details
          className="my-3 border-t border-line pt-3 [&_pre]:max-h-96 [&_pre]:overflow-auto"
          key={record.id}
        >
          <summary className="cursor-pointer text-sm">
            #{index + 1} · {record.purpose} · {record.status} ·{' '}
            {record.usage?.total_tokens ?? '未知'} tokens
          </summary>
          <p className="text-xs text-muted">
            耗时：
            {record.duration_ms == null ? '未知' : `${(record.duration_ms / 1000).toFixed(1)}s`} ·
            首响应：
            {record.first_token_ms == null ? '未观测到' : `${record.first_token_ms.toFixed(0)}ms`}
          </p>
          {record.error && <ErrorBox message={record.error} />}
          <h4>实际输入</h4>
          <pre>{JSON.stringify(record.request, null, 2)}</pre>
          <h4>实际输出</h4>
          <pre>{JSON.stringify(record.response, null, 2)}</pre>
        </details>
      ))}
      <h3>内部运行资料</h3>
      <p className="text-sm text-muted">长工具结果、压缩前历史与工作摘要用于执行核对和恢复。</p>
      {records.data?.artifacts.map((artifact) => (
        <p className="text-sm break-all" key={artifact.id}>
          {artifact.available === false ? (
            `${artifact.kind}：正文已过期`
          ) : (
            <Link to={`/runs/${run.id}/artifacts/${artifact.id}`}>
              {artifact.kind} · {artifact.length ?? '未知'} 字符 · 查看 →
            </Link>
          )}
        </p>
      ))}
      {records.data && !records.data.artifacts.length && (
        <p className="text-sm text-muted">没有可查看的内部资料。</p>
      )}
      <h3>事件时间线</h3>
      <p className="text-sm text-muted">
        {records.data
          ? `事件 ${records.data.events[0]?.sequence ?? 0}–${records.data.events.at(-1)?.sequence ?? 0} / ${records.data.event_page.total}`
          : records.error
            ? '事件记录暂不可用。'
            : '正在加载事件记录…'}
      </p>
      <div className="flex flex-wrap gap-3">
        <Button
          className="secondary button-secondary"
          disabled={records.loading || after === 0}
          onClick={() => setAfter(Math.max(0, after - 200))}
        >
          上一页事件
        </Button>
        <Button
          className="secondary button-secondary"
          disabled={records.loading || records.data?.event_page.next_after == null}
          onClick={() => setAfter(records.data!.event_page.next_after!)}
        >
          下一页事件
        </Button>
      </div>
      {records.data?.events.map((event) => (
        <details
          className="my-3 border-t border-line pt-3 [&_pre]:max-h-96 [&_pre]:overflow-auto"
          key={event.sequence}
        >
          <summary className="cursor-pointer text-sm break-all">
            #{event.sequence} · {event.kind} · {new Date(event.created_at).toLocaleTimeString()}
          </summary>
          <pre>{JSON.stringify(event.data, null, 2)}</pre>
        </details>
      ))}
    </section>
  );
}
