import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { messageOf, send, useResource } from '../../shared/api/api';
import { Button } from '../../shared/ui/Button';
import { Dialog } from '../../shared/ui/Dialog';
import { ErrorBox, Loading } from '../../shared/ui/components';

type Permission = 'deny' | 'read' | 'confirm';
type Installation = { manager: 'npm' | 'uv'; packages: string[]; entrypoint: string | null };
type Preview = {
  name: string;
  config: unknown;
  installation: Installation | null;
  provided_secret_refs: string[];
};
type Operation = {
  id: string;
  target: string;
  status: string;
  attempts: number;
  cancel_requested: boolean;
  parameters: Preview;
  result: {
    config?: unknown;
    progress?: { phase: string; status: string }[];
    tools?: { name: string; description: string; schema: unknown; schema_hash: string }[];
  };
  error_code?: string;
};
const statuses: Record<string, string> = {
  importing: '正在保存配置',
  imported: '等待准备依赖',
  queued: '准备已排队',
  preparing: '正在准备依赖',
  prepared: '等待测试连接',
  testing: '正在测试连接',
  tested: '等待审查工具许可',
  failed: '操作未完成',
  interrupted: '操作已中断',
  cancelled: '已取消',
  active: '已应用',
  expired: '候选已到期，请重新导入',
};
const errors: Record<string, string> = {
  restored_requires_review: '从备份恢复；请补充环境值，重新准备、测试并授权。',
  restore_lock_mismatch: '恢复锁文件与安装身份不符，请检查同批备份。',
  secret_write_failed: '环境文件无法保存，请检查目录权限后重新填写凭据。',
  dispatch_failed: '后台队列暂不可用，请求已保留，会自动重新投递。',
  worker_interrupted: '执行进程已退出，请明确重试。',
  package_not_found: '未找到满足条件的包，请核对包名与版本。',
  package_command_failed: '依赖准备失败，请核对包版本及运行环境。',
  preparation_timeout: '依赖准备超时，可以重试。',
  ambiguous_entrypoint: '此包有多个入口，请在配置中明确指定命令。',
  entrypoint_not_installed: '依赖已处理，但未找到配置的服务入口，请检查命令名。',
  npm_not_installed: '容器中没有 npm。',
  uv_not_installed: '容器中没有 uv。',
  connection_test_failed: '连接测试失败，请检查入口、服务地址、运行依赖和凭据。',
  preparation_failed: '准备未完成，请核对安装目录及运行环境后重试。',
};

function parseConfig(content: string): unknown {
  try {
    return JSON.parse(content);
  } catch {
    // JSON解析异常可能包含原始配置片段，不能将凭据片段复制到错误提示。
    throw new Error('配置不是合法 JSON，请检查引号、逗号及括号。');
  }
}

export function MCPImportDialog({
  connection,
  onClose,
  onSaved,
}: {
  connection?: { id: string; revision: number; name: string };
  onClose: () => void;
  onSaved: () => void;
}) {
  const source = useResource<unknown>(
    connection ? `/api/mcp/connections/${connection.id}/export` : null,
  );
  const [content, setContent] = useState('');
  const [preview, setPreview] = useState<Preview[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    if (source.data) setContent(JSON.stringify(source.data, null, 2));
  }, [source.data]);
  const body = () => ({
    value: parseConfig(content),
    connection_id: connection?.id,
    expected_revision: connection?.revision,
  });
  async function check() {
    setBusy(true);
    setError('');
    try {
      setPreview(await send<Preview[]>('/api/mcp/import-preview', body()));
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    setBusy(true);
    setError('');
    try {
      await send('/api/mcp/import', body());
      setContent('');
      onSaved();
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      title={connection ? `更新 MCP：${connection.name}` : '导入 MCP 配置'}
      onClose={onClose}
      busy={busy}
      footer={
        <>
          <Button className="secondary button-secondary" onClick={onClose} disabled={busy}>
            取消
          </Button>
          {preview ? (
            <Button onClick={save} disabled={busy}>
              确认导入候选
            </Button>
          ) : (
            <Button onClick={check} disabled={busy || source.loading || !content.trim()}>
              检查配置
            </Button>
          )}
        </>
      }
    >
      <ErrorBox message={error || source.error} />
      {source.loading && <Loading />}
      <p>
        支持 mcpServers、单连接、npx/uvx 启动配置及
        HTTP。导入只保存候选，之后分别准备依赖、测试和授权。
      </p>
      {connection && (
        <p className="rounded-lg bg-canvas p-3 text-sm">
          现有连接保持可用，直到新候选通过测试并确认替换。
        </p>
      )}
      <label>
        配置 JSON
        <textarea
          aria-label="配置 JSON"
          rows={10}
          value={content}
          onChange={(event) => {
            setContent(event.target.value);
            setPreview(null);
          }}
          spellCheck={false}
          autoComplete="off"
          placeholder={'{"mcpServers":{"clock":{"command":"uvx","args":["mcp-server-time"]}}}'}
        />
      </label>
      <label>
        选择 JSON 文件
        <input
          type="file"
          accept=".json,application/json"
          onChange={async (event) => {
            const file = event.target.files?.[0];
            if (!file) return;
            if (file.size > 128 * 1024) {
              setError('配置文件不能超过 128 KB');
              return;
            }
            try {
              setContent(await file.text());
              setPreview(null);
              setError('');
            } catch {
              setError('无法读取配置文件，请重新选择。');
            }
          }}
        />
      </label>
      <p className="text-sm text-muted">
        env 或 headers
        中的密钥只写入项目专用环境文件；普通配置和导出仅保留引用。请核对服务来源，准备依赖可能执行包的安装代码，测试会启动或连接服务。
      </p>
      {preview && (
        <div aria-label="配置检查结果" className="mt-4 grid gap-3">
          {preview.map((item, index) => (
            <section key={index} className="rounded-xl border border-line p-4">
              <strong>{item.name}</strong>
              <p>
                {item.installation
                  ? `需明确准备 ${item.installation.manager} 依赖：${item.installation.packages.join('、')}`
                  : '无需应用安装依赖，可直接测试连接'}
              </p>
              {!!item.provided_secret_refs.length && (
                <p className="text-sm text-muted">
                  提供的 {item.provided_secret_refs.length} 项变量将单独保存。
                </p>
              )}
              <details>
                <summary>查看非秘密配置</summary>
                <pre className="max-h-64 overflow-auto text-xs">
                  {JSON.stringify(item.config, null, 2)}
                </pre>
              </details>
            </section>
          ))}
        </div>
      )}
    </Dialog>
  );
}

export function MCPCredentialsDialog({
  path,
  revision,
  onClose,
}: {
  path: string;
  revision?: number;
  onClose: () => void;
}) {
  const [version, setVersion] = useState(0);
  const result = useResource<{ reference: string; source: string; configured: boolean }[]>(
    path,
    version,
  );
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  return (
    <Dialog
      title="连接环境与凭据"
      onClose={onClose}
      busy={busy}
      footer={
        <>
          <Button className="secondary button-secondary" onClick={onClose} disabled={busy}>
            关闭
          </Button>
          <Button
            disabled={busy || !Object.values(values).some(Boolean)}
            onClick={async () => {
              setBusy(true);
              setError('');
              setNotice('');
              try {
                await send(
                  path,
                  {
                    values: Object.fromEntries(
                      Object.entries(values).filter(([, value]) => value !== ''),
                    ),
                    expected_revision: revision,
                  },
                  'PUT',
                );
                setValues({});
                setVersion((v) => v + 1);
                setNotice('已保存；原值不会回显。');
              } catch (failure) {
                setError(messageOf(failure));
              } finally {
                setBusy(false);
              }
            }}
          >
            保存环境值
          </Button>
        </>
      }
    >
      <ErrorBox message={error || result.error} />
      {result.loading && <Loading />}
      {notice && <p role="status">{notice}</p>}
      <p className="text-sm text-muted">
        部署环境变量优先。留空表示保持现值；同名引用共用值，变更后相关连接会重新建立。
      </p>
      {result.data?.length === 0 && <p>此连接没有声明环境或请求头引用。</p>}
      {result.data?.map((row) => (
        <label key={row.reference} className="my-4 block break-all">
          {row.reference}
          <span className="ml-2 text-xs text-muted">
            {row.configured ? '已配置' : '未配置'} ·{' '}
            {row.source === 'environment'
              ? '部署环境优先'
              : row.source === 'project'
                ? '项目环境文件'
                : '等待填写'}
          </span>
          <input
            type="password"
            autoComplete="new-password"
            value={values[row.reference] || ''}
            placeholder="输入新值，现值不回显"
            onChange={(event) => setValues({ ...values, [row.reference]: event.target.value })}
          />
        </label>
      ))}
    </Dialog>
  );
}

function Candidate({ row, changed }: { row: Operation; changed: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [credentials, setCredentials] = useState(false);
  const [permissions, setPermissions] = useState<Record<string, Permission>>(
    Object.fromEntries((row.result.tools || []).map((tool) => [tool.name, 'deny'])),
  );
  const [enable, setEnable] = useState(false);
  const working = ['importing', 'queued', 'preparing', 'testing'].includes(row.status);
  async function act(action: string, body?: unknown, method = 'POST') {
    setBusy(true);
    setError('');
    try {
      await send(`/api/mcp/operations/${row.id}${action}`, body, method);
      changed();
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="mb-4 rounded-xl border border-line bg-white p-5 max-md:p-4"
      aria-label={`候选：${row.parameters.name}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="m-0">{row.parameters.name}</h3>
        <span role="status" className="text-sm text-muted">
          {statuses[row.status] || row.status}
          {row.cancel_requested && working ? ' · 正在取消' : ''}
        </span>
      </div>
      <ErrorBox
        message={
          error ||
          (row.error_code ? errors[row.error_code] || '操作未完成，请核对配置后重试。' : '')
        }
      />
      {row.parameters.installation && (
        <p className="my-3 break-all text-sm">
          {row.parameters.installation.manager} · {row.parameters.installation.packages.join('、')}
        </p>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        {row.parameters.installation &&
          ['imported', 'failed', 'interrupted', 'cancelled'].includes(row.status) && (
            <Button disabled={busy} onClick={() => act('/prepare')}>
              {row.attempts ? '重新准备依赖' : '准备依赖'}
            </Button>
          )}
        {!!row.result.config && !working && !['cancelled', 'expired'].includes(row.status) && (
          <Button disabled={busy} onClick={() => act('/test')}>
            {row.status === 'tested' ? '重新测试连接' : '测试连接并发现工具'}
          </Button>
        )}
        {!['cancelled', 'expired'].includes(row.status) && (
          <Button
            className="secondary button-secondary"
            disabled={busy || working}
            onClick={() => setCredentials(true)}
          >
            环境与凭据
          </Button>
        )}
        {working ? (
          <Button
            className="secondary button-secondary"
            disabled={busy || row.cancel_requested}
            onClick={() => act('/cancel')}
          >
            取消操作
          </Button>
        ) : (
          <Button
            className="secondary button-secondary"
            disabled={busy}
            onClick={() => act('', undefined, 'DELETE')}
          >
            移除候选
          </Button>
        )}
      </div>
      {row.parameters.installation && row.status === 'imported' && (
        <p className="mt-3 text-sm text-muted">
          准备会下载依赖并可能运行安装代码。成功后仍需测试和授权；聊天调用时使用固定安装。
        </p>
      )}
      {!!row.result.progress?.length && (
        <details className="mt-3">
          <summary>准备进展</summary>
          <ol className="text-sm">
            {row.result.progress.map((item, index) => (
              <li key={index}>
                {(
                  {
                    resolve: '解析并锁定依赖',
                    environment: '创建运行环境',
                    install: '安装固定依赖',
                    prepared: '固定入口已准备',
                  } as Record<string, string>
                )[item.phase] || item.phase}
                ：
                {item.status === 'succeeded'
                  ? '完成'
                  : item.status === 'running'
                    ? '开始'
                    : '未完成'}
              </li>
            ))}
          </ol>
        </details>
      )}
      {row.status === 'tested' && (
        <div className="mt-5 border-t border-line pt-4">
          <p>
            已发现 {row.result.tools?.length || 0}{' '}
            个工具，请逐项审查。服务端的只读提示不会自动授予许可。
          </p>
          <div className="my-4 grid gap-3">
            {row.result.tools?.map((tool) => (
              <div key={tool.name} className="rounded-lg border border-line p-3">
                <strong className="break-all">{tool.name}</strong>
                <p className="text-sm">{tool.description}</p>
                <details>
                  <summary>参数结构</summary>
                  <pre className="max-h-60 overflow-auto text-xs">
                    {JSON.stringify(tool.schema, null, 2)}
                  </pre>
                </details>
                <label className="mt-2 block">
                  {tool.name} 调用许可
                  <select
                    aria-label={`${tool.name} 调用许可`}
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
            ))}
          </div>
          <label className="mb-3 flex flex-row items-center gap-2">
            <input
              type="checkbox"
              checked={enable}
              onChange={(event) => setEnable(event.target.checked)}
            />
            应用后启用连接
          </label>
          <Button
            disabled={busy}
            onClick={() => act('/activate', { permissions, enabled: enable })}
          >
            应用此候选配置
          </Button>
        </div>
      )}
      {credentials && (
        <MCPCredentialsDialog
          path={`/api/mcp/operations/${row.id}/credentials`}
          onClose={() => setCredentials(false)}
        />
      )}
    </section>
  );
}

type Cleanup = {
  id: string;
  name: string;
  status: string;
  error_code?: string;
  result: {
    removed_installations?: number;
    retained_installations?: number;
    waiting_for_use?: boolean;
  };
};

export function MCPCleanup({ version }: { version: number }) {
  const [params] = useSearchParams();
  const selected = params.get('cleanup');
  const [tick, setTick] = useState(0);
  const [error, setError] = useState('');
  const result = useResource<Cleanup[]>('/api/mcp/cleanup', `${version}:${tick}`);
  const detail = useResource<Cleanup>(selected ? `/api/mcp/cleanup/${encodeURIComponent(selected)}` : null, `${version}:${tick}`);
  const rows = detail.data ? [detail.data, ...(result.data ?? []).filter((row) => row.id !== detail.data!.id)] : result.data;
  const pending = rows?.some((row) => ['queued', 'running', 'waiting'].includes(row.status));
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(() => { if (document.visibilityState !== 'hidden') setTick((value) => value + 1); }, 5000);
    return () => clearInterval(timer);
  }, [pending]);
  if (!rows?.length && !result.error && !selected) return null;
  return (
    <details className="mt-6 rounded-xl border border-line bg-white p-5" open={Boolean(selected) || undefined}>
      <summary>后台清理与失败重试</summary>
      <ErrorBox message={error || result.error || detail.error} />
      {detail.loading && <Loading />}
      <p className="text-sm text-muted">
        保留仍在使用的安装与必要历史记录，只清理应用拥有的文件。第三方服务和外部目录由其所有者管理。
      </p>
      {rows?.map((row) => (
        <section
          key={row.id}
          aria-label={`清理：${row.name}`}
          className="mt-3 border-t border-line pt-3"
        >
          <strong>{row.name || 'MCP管理操作'}</strong>
          <p role="status">
            {(
              {
                queued: '等待后台清理',
                running: '正在清理',
                waiting: '等待使用结束或依赖解除',
                failed: '清理未完成，请检查目录状态后重试',
                completed: '本次可回收内容已清理',
              } as Record<string, string>
            )[row.status] || row.status}
          </p>
          <p className="text-sm text-muted">
            已回收安装 {row.result.removed_installations || 0} 个；仍需保留{' '}
            {row.result.retained_installations || 0} 个。
          </p>
          {['failed', 'waiting'].includes(row.status) && (
            <Button
              className="secondary button-secondary"
              onClick={async () => {
                setError('');
                try {
                  await send(`/api/mcp/cleanup/${row.id}/retry`);
                  setTick((value) => value + 1);
                } catch (failure) {
                  setError(messageOf(failure));
                }
              }}
            >
              重新检查并清理
            </Button>
          )}
        </section>
      ))}
    </details>
  );
}

export function MCPRemoveDialog({
  connection,
  onClose,
  onRemoved,
}: {
  connection: { id: string; name: string };
  onClose: () => void;
  onRemoved: () => void;
}) {
  const preview = useResource<{
    revision: number;
    owned_installations: number;
    unresolved_calls: number;
    has_managed_data: boolean;
  }>(`/api/mcp/connections/${connection.id}/removal-preview`);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return (
    <Dialog
      title={`移除 MCP：${connection.name}`}
      onClose={onClose}
      busy={busy}
      footer={
        <>
          <Button className="secondary button-secondary" onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button
            disabled={busy || preview.loading || !preview.data}
            onClick={async () => {
              setBusy(true);
              setError('');
              try {
                await send(
                  `/api/mcp/connections/${connection.id}`,
                  { expected_revision: preview.data!.revision, deleted: true },
                  'PATCH',
                );
                onRemoved();
              } catch (failure) {
                setError(messageOf(failure));
              } finally {
                setBusy(false);
              }
            }}
          >
            确认移除连接
          </Button>
        </>
      }
    >
      <ErrorBox message={error || preview.error} />
      {preview.loading && <Loading />}
      {preview.data && (
        <>
          <p>
            移除后撤回当前任务的连接许可，从列表移除。后台将回收 {preview.data.owned_installations}{' '}
            个无依赖的受管安装、专属缓存和不再共享的页面凭据。
          </p>
          {preview.data.has_managed_data && (
            <p>此服务的应用专属数据目录也会在使用结束后删除。需要保留的服务数据请先备份。</p>
          )}
          {!!preview.data.unresolved_calls && (
            <p>
              当前有 {preview.data.unresolved_calls}{' '}
              个未决调用；它们需要的安装与数据会继续保留，不会自动重发调用。
            </p>
          )}
          <p className="text-sm text-muted">
            历史引用保留必要身份；远程服务、预安装的外部程序、外部工作目录和部署环境变量不会被删除。
          </p>
        </>
      )}
    </Dialog>
  );
}

export function MCPPreparations({ version, changed }: { version: number; changed: () => void }) {
  const [params] = useSearchParams();
  const selected = params.get('operation');
  const [tick, setTick] = useState(0);
  const result = useResource<Operation[]>('/api/mcp/operations', `${version}:${tick}`);
  const detail = useResource<Operation>(selected ? `/api/mcp/operations/${encodeURIComponent(selected)}` : null, `${version}:${tick}`);
  const rows = detail.data ? [detail.data, ...(result.data ?? []).filter((row) => row.id !== detail.data!.id)] : result.data;
  const working = rows?.some((row) =>
    ['importing', 'queued', 'preparing', 'testing'].includes(row.status),
  );
  useEffect(() => {
    if (!working) return;
    const timer = setInterval(() => { if (document.visibilityState !== 'hidden') setTick((v) => v + 1); }, 1500);
    return () => clearInterval(timer);
  }, [working]);
  return (
    <>
      <ErrorBox message={result.error || detail.error} />
      {detail.loading && <Loading />}
      {!!rows?.length && <h2>待处理的候选配置</h2>}
      {rows?.map((row) => (
        ['active', 'removed', 'archived'].includes(row.status)
          ? <p key={row.id} className="rounded-lg border border-line bg-white p-4 text-sm">{row.parameters.name}：{row.status === 'active' ? '已应用，后续操作请在连接中管理。' : '候选已移除，仅保留历史记录。'}</p>
          : <Candidate key={`${row.id}:${row.status}:${row.attempts}`} row={row} changed={changed} />
      ))}
    </>
  );
}
