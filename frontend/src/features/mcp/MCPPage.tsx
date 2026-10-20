import { Button } from '../../shared/ui/Button';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { messageOf, send, useResource } from '../../shared/api/api';
import { Empty, ErrorBox, Loading, PageTitle } from '../../shared/ui/components';
import {
  MCPCleanup,
  MCPCredentialsDialog,
  MCPImportDialog,
  MCPPreparations,
  MCPRemoveDialog,
} from './MCPManagementPanels';

type Permission = 'deny' | 'read' | 'confirm';
type MCPTool = {
  name: string;
  description: string;
  schema: unknown;
  schema_hash: string;
  permission: Permission;
  annotations: unknown;
};
type Connection = {
  id: string;
  name: string;
  revision: number;
  authorization_epoch: number;
  enabled: boolean;
  config: Record<string, unknown>;
  tools: MCPTool[];
};
export type Receipt = {
  id: string;
  input_hash: string;
  status: string;
  tool_name: string;
  connection_revision: number;
  request: { service_name: string; description: string; arguments: unknown; schema_hash: string };
  decision?: { actor?: string; review?: { result?: { verdict: string; reason: string } } };
  result?: { error?: string; source_id?: string; source_version_id?: string };
};

/** 表单持有显示修订号；服务器拒绝旧页面覆盖新配置。连接测试与许可分开。 */
function ConnectionCard({ row, changed }: { row: Connection; changed: () => void }) {
  const [editing, setEditing] = useState(false);
  const [credentials, setCredentials] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [permissions, setPermissions] = useState<Record<string, Permission>>(
    Object.fromEntries(row.tools.map((tool) => [tool.name, tool.permission])),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function act(body: unknown, discover = false) {
    setBusy(true);
    setError('');
    try {
      await send(
        `/api/mcp/connections/${row.id}${discover ? '/discover' : ''}`,
        body,
        discover ? 'POST' : 'PATCH',
      );
      changed();
    } catch (error) {
      setError(messageOf(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      aria-label={`连接：${row.name}`}
      className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5"
    >
      <div className="row flex items-center gap-3 wrap flex-wrap">
        <h2>{row.name}</h2>
        <span>
          {row.enabled ? '已启用' : '已停用'} · 修订 {row.revision}
        </span>
      </div>
      <ErrorBox message={error} />
      {busy && <p role="status">正在处理连接操作…</p>}
      <div className="row flex items-center gap-3 wrap flex-wrap">
        <Button disabled={busy} onClick={() => act({ expected_revision: row.revision }, true)}>
          测试连接并发现工具
        </Button>
        <Button
          className="secondary button-secondary"
          disabled={busy}
          onClick={() => act({ expected_revision: row.revision, enabled: !row.enabled })}
        >
          {row.enabled ? '停用连接' : '启用连接'}
        </Button>
        <a
          href={`/api/mcp/connections/${row.id}/export`}
          className="button secondary button-secondary"
        >
          导出非秘密配置
        </a>
      </div>
      <p className="muted text-muted">
        启用后仍须为工具选择许可。停用会撤回当前任务的旧绑定，再次启用从新任务生效。
      </p>
      <details className="mt-5">
        <summary>配置与管理</summary>
        <div className="my-4 flex flex-wrap gap-2">
          <Button disabled={busy} onClick={() => setEditing(true)}>
            准备更新候选
          </Button>
          <Button
            className="secondary button-secondary"
            disabled={busy}
            onClick={() => setCredentials(true)}
          >
            环境与凭据
          </Button>
        </div>
        <p className="text-sm text-muted">
          更新先生成候选，测试与审查后才替换当前连接。已有任务的旧绑定在替换后撤回。
        </p>
        <Button
          className="secondary button-secondary"
          disabled={busy}
          onClick={() => setRemoving(true)}
        >
          删除连接配置
        </Button>
      </details>
      {removing && (
        <MCPRemoveDialog connection={row} onClose={() => setRemoving(false)} onRemoved={changed} />
      )}
      {editing && (
        <MCPImportDialog
          connection={row}
          onClose={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            changed();
          }}
        />
      )}
      {credentials && (
        <MCPCredentialsDialog
          path={`/api/mcp/connections/${row.id}/credentials`}
          revision={row.revision}
          onClose={() => setCredentials(false)}
        />
      )}
      {!row.tools.length ? (
        <p>尚未发现工具。</p>
      ) : (
        <>
          {row.tools.map((tool) => (
            <div
              className="list-card flex items-center gap-[17px] rounded-[9px] border border-line bg-white px-[23px] py-4.5 text-[#5c7057] hover:border-[#aec2a5] hover:no-underline max-md:flex-wrap max-md:gap-3 max-md:p-4 [&>div]:min-w-0 [&>div]:flex-1 [&_h3]:mt-0 [&_h3]:mb-[5px] [&_h3]:text-[#344c37] [&_h3]:[overflow-wrap:anywhere] max-md:[&_h3]:text-[13px] [&_p]:m-0 [&_p]:text-[11px] [&_p]:text-[#85917e] [&_p]:[overflow-wrap:anywhere] [&>span:last-child]:text-[11px] [&>span:last-child]:whitespace-nowrap max-md:[&>span:last-child]:hidden"
              key={tool.name}
            >
              <div>
                <strong>{tool.name}</strong>
                <p>{tool.description}</p>
                <details className="mt-5">
                  <summary>参数结构与服务提示</summary>
                  <pre>
                    {JSON.stringify(
                      { schema: tool.schema, annotations: tool.annotations },
                      null,
                      2,
                    )}
                  </pre>
                </details>
                <label>
                  调用许可
                  <select
                    value={permissions[tool.name]}
                    onChange={(event) =>
                      setPermissions({
                        ...permissions,
                        [tool.name]: event.target.value as Permission,
                      })
                    }
                  >
                    <option value="deny">禁止调用</option>
                    <option value="read">已审查无副作用，允许自动调用</option>
                    <option value="confirm">每次展示具体操作后确认</option>
                  </select>
                </label>
              </div>
            </div>
          ))}
          <p className="muted text-muted">
            服务提供的只读提示不等于实际无副作用，请按工具用途审查。
          </p>
          <Button
            disabled={busy}
            onClick={() => act({ expected_revision: row.revision, permissions })}
          >
            保存工具许可
          </Button>
        </>
      )}
    </section>
  );
}

export function MCPPage() {
  const [version, setVersion] = useState(0);
  const result = useResource<Connection[]>('/api/mcp/connections', version);
  const [importing, setImporting] = useState(false);
  const changed = () => setVersion((value) => value + 1);
  return (
    <>
      <PageTitle eyebrow="CONNECTIONS" title="MCP 工具服务">
        <Button onClick={() => setImporting(true)}>导入 MCP 配置</Button>
        <Button className="secondary button-secondary" onClick={changed}>
          刷新
        </Button>
      </PageTitle>
      <p>按需连接自己的工具服务。默认没有外部服务；连接配置、工具许可和每次操作确认分别管理。</p>
      <p className="muted text-muted">
        连接按聊天和执行角色隔离复用。需要服务端连续会话时，可在配置中加入
        "protocol_mode":"legacy"。
        默认自动协商；空闲回收或应用重启后会建立新连接，服务端状态是否保留由对应服务决定。
      </p>
      <ErrorBox message={result.error} />
      {result.loading && <Loading />}
      {result.data?.length === 0 && (
        <Empty title="尚未添加 MCP 服务">
          添加或导入配置后，先测试连接、发现工具，再选择许可。
        </Empty>
      )}
      {importing && (
        <MCPImportDialog
          onClose={() => setImporting(false)}
          onSaved={() => {
            setImporting(false);
            changed();
          }}
        />
      )}
      <MCPPreparations version={version} changed={changed} />
      {result.data?.map((row) => (
        <ConnectionCard key={`${row.id}:${row.revision}`} row={row} changed={changed} />
      ))}
      <MCPCleanup version={version} />
    </>
  );
}

/** 与聊天和运行详情共用：显示精确参数，后端在全批决定齐全后自动续跑。 */
export function MCPApprovals({
  rootId,
  refreshKey,
  onChanged,
  receipts,
}: {
  rootId: string;
  refreshKey: string | number;
  onChanged: () => void;
  receipts?: Receipt[];
}) {
  const [version, setVersion] = useState(0);
  const result = useResource<Receipt[]>(
    receipts ? null : `/api/runs/${rootId}/mcp`,
    `${refreshKey}:${version}`,
  );
  const rows = receipts ?? result.data;
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  async function decide(row: Receipt, approve: boolean) {
    setBusy(row.id);
    setError('');
    try {
      await send(`/api/runs/${rootId}/mcp/${row.id}/decision`, {
        expected_hash: row.input_hash,
        approve,
      });
      setVersion((value) => value + 1);
      onChanged();
    } catch (error) {
      setError(messageOf(error));
    } finally {
      setBusy('');
    }
  }
  if (!rows?.length) return <ErrorBox message={result.error} />;
  return (
    <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
      <h2>MCP 调用与确认</h2>
      <ErrorBox message={error || result.error} />
      <p>
        确认只针对以下服务、版本和参数。所有待确认项处理后会自动继续；拒绝会作为反馈交给 Agent。
      </p>
      {rows.map((row) => (
        <article
          key={row.id}
          className="list-card flex items-center gap-[17px] rounded-[9px] border border-line bg-white px-[23px] py-4.5 text-[#5c7057] hover:border-[#aec2a5] hover:no-underline max-md:flex-wrap max-md:gap-3 max-md:p-4 [&>div]:min-w-0 [&>div]:flex-1 [&_h3]:mt-0 [&_h3]:mb-[5px] [&_h3]:text-[#344c37] [&_h3]:[overflow-wrap:anywhere] max-md:[&_h3]:text-[13px] [&_p]:m-0 [&_p]:text-[11px] [&_p]:text-[#85917e] [&_p]:[overflow-wrap:anywhere] [&>span:last-child]:text-[11px] [&>span:last-child]:whitespace-nowrap max-md:[&>span:last-child]:hidden"
        >
          <div>
            <strong>
              {row.request.service_name} · {row.tool_name}
            </strong>
            <p>{row.request.description}</p>
            <p className="muted text-muted">
              连接修订 {row.connection_revision} · {row.status}
            </p>
            <pre>{JSON.stringify(row.request.arguments, null, 2)}</pre>
            {row.decision?.review?.result && (
              <p role="status" className="my-2 text-sm">
                审查说明：{row.decision.review.result.reason}
              </p>
            )}
            {row.status === 'pending' && row.decision?.review?.result?.verdict === 'deny' && (
              <p className="my-2 text-sm">
                审查未批准，尚未调用服务。你可以保持拒绝，或核对后仅允许这一次；连接及工具的固定禁止仍然有效。
              </p>
            )}
            {row.status === 'pending' && (
              <div className="row flex items-center gap-3">
                <Button disabled={Boolean(busy)} onClick={() => decide(row, true)}>
                  允许这次调用
                </Button>
                <Button
                  className="secondary button-secondary"
                  disabled={Boolean(busy)}
                  onClick={() => decide(row, false)}
                >
                  拒绝这次调用
                </Button>
              </div>
            )}
            {row.result?.error && (
              <p className="notice border-l-[3px] border-[#c0a76b] bg-[#f5f0e0] px-4 py-3 text-[13px] [overflow-wrap:anywhere]">
                {row.result.error}
              </p>
            )}
            {row.result?.source_id && (
              <Link to={`/sources/${row.result.source_id}?version=${row.result.source_version_id}`}>
                查看固定来源快照 →
              </Link>
            )}
            <details className="mt-5">
              <summary>调用绑定信息</summary>
              <p>{row.id}</p>
              <p>{row.input_hash}</p>
            </details>
          </div>
        </article>
      ))}
    </section>
  );
}
