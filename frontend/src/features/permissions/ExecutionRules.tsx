import { Button } from '../../shared/ui/Button';
import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { messageOf, send, useResource } from '../../shared/api/api';
import { ErrorBox } from '../../shared/ui/components';

type Rule = {
  id: string;
  argv: string[];
  cwd: string;
  conversation_id: string | null;
  enabled: boolean;
  effect: string;
  match: string;
  revision: number;
};

/** 确定性命令许可，不写入用户记忆；跨会话规则仍绑定当前挂载工作区。 */
export function ExecutionRules() {
  const [query] = useSearchParams();
  const conversation = query.get('conversation_id');
  const [version, setVersion] = useState(0);
  const result = useResource<Rule[]>(
    `/api/execution-rules${conversation ? `?conversation_id=${encodeURIComponent(conversation)}` : ''}`,
    version,
  );
  const root = useResource<{ workspace: string; cwd: string }>('/api/execution-rules/context');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [argv, setArgv] = useState('["git", "status"]');
  const [cwd, setCwd] = useState('');
  const [match, setMatch] = useState('exact');
  const [effect, setEffect] = useState('ask');
  const [scope, setScope] = useState('persistent');
  async function update(row: Rule, fields: Record<string, unknown>) {
    setBusy(true);
    setError('');
    try {
      await send(
        `/api/execution-rules/${row.id}`,
        { expected_revision: row.revision, ...fields },
        'PATCH',
      );
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
      setVersion((value) => value + 1);
    }
  }
  return (
    <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
      <h2>命令执行规则</h2>
      <p>
        审批与能力开关分别管理。自动执行仍遵守拒绝和询问规则；修改或删除规则将在下一次命令执行前重新核对。
      </p>
      <p className="muted text-muted small text-xs">
        工作区：{root.data?.workspace ?? '尚未加载'}。规则约束是否启动命令，不提供操作系统隔离。
      </p>
      <ErrorBox message={error || result.error || root.error} />
      {conversation && (
        <p>
          正在查看本会话和跨会话规则 · <Link to="/tools">只看跨会话规则</Link>
        </p>
      )}
      {result.data?.map((row) => (
        <article
          className="list-card flex items-center gap-[17px] rounded-[9px] border border-line bg-white px-[23px] py-4.5 text-[#5c7057] hover:border-[#aec2a5] hover:no-underline max-md:flex-wrap max-md:gap-3 max-md:p-4 [&>div]:min-w-0 [&>div]:flex-1 [&_h3]:mt-0 [&_h3]:mb-[5px] [&_h3]:text-[#344c37] [&_h3]:[overflow-wrap:anywhere] max-md:[&_h3]:text-[13px] [&_p]:m-0 [&_p]:text-[11px] [&_p]:text-[#85917e] [&_p]:[overflow-wrap:anywhere] [&>span:last-child]:text-[11px] [&>span:last-child]:whitespace-nowrap max-md:[&>span:last-child]:hidden"
          key={row.id}
        >
          <div>
            <pre>{JSON.stringify(row.argv)}</pre>
            <p className="muted text-muted small text-xs">
              {row.conversation_id ? '本会话' : '跨会话'} · 工作目录 {row.cwd} ·{' '}
              {row.match === 'prefix' ? '参数前缀匹配，允许追加参数' : '全部参数相同'}
            </p>
            <div className="row flex items-center gap-3 wrap flex-wrap">
              <label>
                规则动作
                <select
                  aria-label={`规则动作 ${row.argv.join(' ')}`}
                  disabled={busy}
                  value={row.effect}
                  onChange={(event) => update(row, { effect: event.target.value })}
                >
                  <option value="allow">允许</option>
                  <option value="ask">询问</option>
                  <option value="deny">拒绝</option>
                </select>
              </label>
              <Button
                className="secondary button-secondary"
                disabled={busy}
                onClick={() => update(row, { enabled: !row.enabled })}
              >
                {row.enabled ? '停用规则' : '启用规则'}
              </Button>
              <Button
                className="quiet button-quiet"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  setError('');
                  try {
                    const response = await fetch(
                      `/api/execution-rules/${row.id}?expected_revision=${row.revision}`,
                      { method: 'DELETE' },
                    );
                    if (!response.ok) {
                      const body = await response.json();
                      throw new Error(body.detail ?? '删除失败');
                    }
                  } catch (failure) {
                    setError(messageOf(failure));
                  } finally {
                    setBusy(false);
                    setVersion((value) => value + 1);
                  }
                }}
              >
                删除规则
              </Button>
            </div>
          </div>
        </article>
      ))}
      {result.data?.length === 0 && <p className="muted text-muted">暂无命令规则。</p>}
      <details className="mt-5">
        <summary>手动添加规则</summary>
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            setError('');
            setBusy(true);
            try {
              await send('/api/execution-rules', {
                argv: JSON.parse(argv),
                cwd: cwd || root.data?.cwd,
                effect,
                match,
                conversation_id: scope === 'session' ? conversation : null,
              });
              setVersion((value) => value + 1);
            } catch (failure) {
              setError(messageOf(failure));
            } finally {
              setBusy(false);
            }
          }}
        >
          <label>
            命令参数（JSON 数组）
            <input required value={argv} onChange={(event) => setArgv(event.target.value)} />
          </label>
          <label>
            工作目录
            <input
              required
              value={cwd || root.data?.cwd || ''}
              onChange={(event) => setCwd(event.target.value)}
            />
          </label>
          <div className="row flex items-center gap-3 wrap flex-wrap">
            <label>
              匹配范围
              <select value={match} onChange={(event) => setMatch(event.target.value)}>
                <option value="exact">全部参数相同</option>
                <option value="prefix">参数前缀</option>
              </select>
            </label>
            <label>
              动作
              <select value={effect} onChange={(event) => setEffect(event.target.value)}>
                <option value="ask">询问</option>
                <option value="deny">拒绝</option>
                <option value="allow">允许</option>
              </select>
            </label>
            <label>
              作用域
              <select value={scope} onChange={(event) => setScope(event.target.value)}>
                <option value="persistent">当前工作区跨会话</option>
                {conversation && <option value="session">仅本会话</option>}
              </select>
            </label>
          </div>
          <p className="muted text-muted small text-xs">
            解释器和复杂脚本不能创建可复用允许规则。前缀规则允许追加任意参数，请核对展示的范围。
          </p>
          <Button disabled={busy || !root.data}>保存命令规则</Button>
        </form>
      </details>
    </section>
  );
}
