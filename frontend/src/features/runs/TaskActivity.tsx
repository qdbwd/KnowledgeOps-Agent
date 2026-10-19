import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useResource } from '../../shared/api/api';
import type { Run } from '../../shared/types/types';
import { Button } from '../../shared/ui/Button';
import { ErrorBox, Loading } from '../../shared/ui/components';

type Summary = {
  tools: { id: string; name: string; display_name: string; status: string; created_at: string }[];
  tools_truncated: boolean;
  drafts: { id: string; title: string | null; status: string }[];
  drafts_truncated: boolean;
  deliveries: { id: string; name: string; bytes: number; url: string; view_url: string }[];
  deliveries_truncated: boolean;
};
const statuses: Record<string, string> = {
  started: '已开始',
  returned: '已返回',
  replayed: '复用已保存结果',
  error: '未完成或出错',
  unsettled: '结果尚未核对',
};

/** 日常任务详情只读取有界摘要，内部产物和模型输入留在诊断入口。 */
export function TaskActivity({ run, refreshKey }: { run: Run; refreshKey: string }) {
  const [expanded, setExpanded] = useState(false);
  const [retry, setRetry] = useState(0);
  const result = useResource<Summary>(`/api/runs/${run.id}/summary`, `${refreshKey}:${retry}`);
  return (
    <section
      className="mb-6 rounded-xl border border-line bg-white p-6 max-md:p-4"
      aria-label="任务产出与活动"
    >
      <h2>任务产出与活动</h2>
      <ErrorBox message={result.error} />
      {result.error && (
        <Button className="secondary button-secondary" onClick={() => setRetry((n) => n + 1)}>
          重试加载任务摘要
        </Button>
      )}
      {result.loading && !result.data && <Loading />}
      {result.data && (
        <>
          <h3>已交付文件</h3>
          {result.data.deliveries.map((file) => (
            <div
              key={file.id}
              className="mb-2 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line px-4 py-3"
            >
              <Link className="min-w-0 break-all" to={file.view_url}>
                {file.name}
              </Link>
              <a className="text-sm" href={file.url} download>
                下载 · {(file.bytes / 1024).toFixed(1)} KB
              </a>
            </div>
          ))}
          {!result.data.deliveries.length && <p className="text-sm text-muted">暂无已交付文件。</p>}
          {result.data.deliveries_truncated && (
            <p className="text-sm text-muted">这里显示最近 50 份交付。</p>
          )}
          <Link className="text-sm" to="/sources">
            管理材料与交付文件 →
          </Link>
          {result.data.drafts.length > 0 && (
            <>
              <h3>知识草稿与变更</h3>
              {result.data.drafts.map((draft) => (
                <p key={draft.id}>
                  <Link to={`/changes/${draft.id}`}>{draft.title || '查看知识变更'} →</Link>
                </p>
              ))}
              {result.data.drafts_truncated && <Link to="/changes">查看全部知识变更 →</Link>}
            </>
          )}
          {run.details_available !== false && (
            <>
              <h3>工具活动</h3>
              <ul className="m-0 list-none p-0">
                {(expanded ? result.data.tools : result.data.tools.slice(0, 6)).map((tool) => (
                  <li
                    key={tool.id}
                    className="flex flex-wrap items-center justify-between gap-2 border-b border-line py-3 last:border-b-0"
                  >
                    <span title={tool.name}>{tool.display_name}</span>
                    <span className="text-xs text-muted">
                      {statuses[tool.status] || tool.status} ·{' '}
                      {new Date(tool.created_at).toLocaleTimeString()}
                    </span>
                  </li>
                ))}
              </ul>
              {!result.data.tools.length && <p className="text-sm text-muted">暂无工具活动。</p>}
              {result.data.tools.length > 6 && (
                <Button
                  className="secondary button-secondary mt-3"
                  onClick={() => setExpanded(!expanded)}
                >
                  {expanded ? '收起工具活动' : `展开工具活动（${result.data.tools.length}）`}
                </Button>
              )}
              {result.data.tools_truncated && (
                <p className="text-sm text-muted">显示最近 50 项活动，完整事件可点击“诊断”查看。</p>
              )}
            </>
          )}
        </>
      )}
    </section>
  );
}
