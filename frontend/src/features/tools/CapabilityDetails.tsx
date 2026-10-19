import { useState } from 'react';
import { useResource } from '../../shared/api/api';
import { Button } from '../../shared/ui/Button';
import { Dialog } from '../../shared/ui/Dialog';
import { ErrorBox, Loading, Markdown } from '../../shared/ui/components';
import { capabilityName, type CapabilityChoice } from './capabilities';

type Detail = {
  definition?: { name: string; description: string; parameters: Record<string, unknown> };
  content?: string;
  version?: string;
  compatibility?: string;
  resources?: { path: string; size: number; binary: boolean }[];
};

/** 只读取选中的定义或技能资源，不把管理页阅读变成执行或授权。 */
export function CapabilityDetails({
  choice,
  onClose,
}: {
  choice: CapabilityChoice;
  onClose: () => void;
}) {
  const [retry, setRetry] = useState(0);
  const [resource, setResource] = useState<string | null>(null);
  const url =
    choice.detail_url ??
    (/^(builtin|middleware|mcp):/.test(choice.id)
      ? `/api/tools/detail?tool_id=${encodeURIComponent(choice.id)}`
      : null);
  const detail = useResource<Detail>(url, retry);
  const source = useResource<{ content: string | null; binary: boolean }>(
    resource && choice.detail_url
      ? `${choice.detail_url}/resource?path=${encodeURIComponent(resource)}`
      : null,
  );
  return (
    <Dialog title={capabilityName(choice)} onClose={onClose}>
      <p className="mt-0 text-xs text-muted [overflow-wrap:anywhere]">{choice.name}</p>
      <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
        {choice.description || choice.summary}
      </p>
      {choice.unavailable_reason && (
        <p className="rounded-lg bg-[#fbf4e9] p-3 text-sm">{choice.unavailable_reason}</p>
      )}
      {detail.loading && <Loading />}
      <ErrorBox message={detail.error} />
      {detail.error && (
        <Button
          type="button"
          className="secondary button-secondary"
          onClick={() => setRetry(retry + 1)}
        >
          重新加载详情
        </Button>
      )}
      {detail.data?.definition && (
        <>
          <h3>完整用途</h3>
          <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
            {detail.data.definition.description}
          </p>
          <h3>参数定义</h3>
          <pre className="max-h-80 overflow-auto rounded-lg bg-canvas p-4 text-xs">
            {JSON.stringify(detail.data.definition.parameters, null, 2)}
          </pre>
        </>
      )}
      {detail.data?.content !== undefined && (
        <>
          <p className="text-xs text-muted">版本：{detail.data.version}</p>
          <h3>使用条件</h3>
          <p className="text-sm">
            {detail.data.compatibility ||
              '详细要求见技能正文。启用技能不会安装依赖或授予工具权限。'}
          </p>
          <h3>技能正文</h3>
          <Markdown text={detail.data.content.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, '')} />
          <h3>配套资源</h3>
          <ul className="list-none space-y-2 p-0">
            {detail.data.resources?.map((item) => (
              <li
                key={item.path}
                className="flex items-center justify-between gap-3 rounded-lg border border-line p-3"
              >
                <span className="min-w-0 text-sm [overflow-wrap:anywhere]">
                  {item.path}
                  <small className="ml-2 text-muted">{item.size.toLocaleString()} 字节</small>
                </span>
                <button
                  type="button"
                  className="shrink-0 text-sm text-brand"
                  onClick={() => setResource(item.path)}
                >
                  {item.binary ? '资源信息' : '查看内容'}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
      {choice.members && (
        <>
          <h3>包含的工具（{choice.members.length}）</h3>
          <ul className="list-none space-y-2 p-0">
            {choice.members.map((item) => (
              <li key={item.id} className="rounded-lg border border-line p-3 text-sm">
                <strong>{capabilityName(item)}</strong>
                <p className="mb-0 text-xs text-muted">{item.summary || item.description}</p>
              </li>
            ))}
          </ul>
        </>
      )}
      {resource && (
        <Dialog title={resource} onClose={() => setResource(null)}>
          {source.loading && <Loading />}
          <ErrorBox message={source.error} />
          {source.data?.binary ? (
            <p>二进制配套资源，管理页面不执行或渲染此文件。</p>
          ) : (
            source.data && (
              <pre className="overflow-auto whitespace-pre-wrap rounded-lg bg-canvas p-4 text-xs [overflow-wrap:anywhere]">
                {source.data.content}
              </pre>
            )
          )}
        </Dialog>
      )}
    </Dialog>
  );
}
