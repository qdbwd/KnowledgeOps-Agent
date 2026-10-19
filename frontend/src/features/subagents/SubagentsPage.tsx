import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { messageOf, send, useResource } from '../../shared/api/api';
import { Button } from '../../shared/ui/Button';
import { Dialog } from '../../shared/ui/Dialog';
import { Empty, ErrorBox, Loading, PageTitle } from '../../shared/ui/components';
import { EffectiveCapabilities } from '../tools/EffectiveCapabilities';
import type { Role } from './SubagentFields';
import type { RoleTemplate } from './SubagentEditorPage';

function RoleCard({ row, changed }: { row: Role; changed: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function act(body: Record<string, unknown>) {
    setBusy(true);
    setError('');
    try {
      await send(
        `/api/subagents/${encodeURIComponent(row.id)}`,
        { expected_revision: row.revision, ...body },
        'PATCH',
      );
      setConfirming(false);
      changed();
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      aria-label={row.display_name}
      className="rounded-2xl border border-line bg-white p-6 max-md:p-4"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="m-0 text-lg font-semibold">{row.display_name}</h2>
          <p className="mb-0 mt-1 text-xs text-muted">{row.name}</p>
        </div>
        <span className="rounded-md bg-canvas px-2.5 py-1 text-xs text-muted">
          {row.enabled ? '已启用' : '已停用'} · 修订 {row.revision}
        </span>
      </div>
      <p className="text-sm">{row.description}</p>
      <p className="text-xs text-muted">
        {(row.resolved_tools ?? row.tools).length} 项已选工具 · {row.skills.length} 项 Skills
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <Link
          className="button button-secondary"
          to={`/subagents/${encodeURIComponent(row.id)}/edit`}
        >
          编辑配置
        </Link>
        <EffectiveCapabilities roleId={row.id} version={row.revision} />
        <Button
          className="button-quiet"
          disabled={busy}
          onClick={() => void act({ enabled: !row.enabled })}
        >
          {row.enabled ? '停用' : '启用'}
        </Button>
        <Button
          className="button-quiet text-danger"
          disabled={busy}
          onClick={() => setConfirming(true)}
        >
          删除
        </Button>
      </div>
      <ErrorBox message={error} />
      {error && !confirming && (
        <Button className="button-secondary" disabled={busy} onClick={changed}>
          刷新配置
        </Button>
      )}
      {confirming && (
        <Dialog
          title={`删除${row.display_name}？`}
          busy={busy}
          onClose={() => setConfirming(false)}
          footer={
            <>
              <Button
                className="button-secondary"
                disabled={busy}
                onClick={() => setConfirming(false)}
              >
                取消删除
              </Button>
              <Button
                className="button-secondary text-danger"
                disabled={busy}
                onClick={() => void act({ deleted: true })}
              >
                确认删除此角色
              </Button>
            </>
          }
        >
          <p>
            确认删除「{row.display_name}」（{row.name}
            ）？旧绑定将停止，已产生的任务结果保留，名称不可复用。
          </p>
          <ErrorBox message={error} />
          {error && (
            <Button className="button-secondary" disabled={busy} onClick={changed}>
              刷新配置
            </Button>
          )}
        </Dialog>
      )}
    </section>
  );
}

/** 历史直达链接继续可读；常规目录不再展开历史或内嵌长表单。 */
export function SubagentsPage() {
  const [version, setVersion] = useState(0);
  const [search, setSearch] = useSearchParams();
  const result = useResource<Role[]>('/api/subagents', version);
  const templates = useResource<RoleTemplate[]>('/api/subagents/templates', version);
  const historyId = search.get('history');
  const history = useResource<Role[]>(
    historyId ? `/api/subagents/${encodeURIComponent(historyId)}/history` : null,
    version,
  );
  const changed = () => setVersion((value) => value + 1);
  return (
    <>
      <PageTitle eyebrow="角色与分工" title="子Agent">
        <Button className="button-secondary" onClick={changed}>
          刷新
        </Button>
        <Link className="button" to="/subagents/new">
          新建子Agent
        </Link>
      </PageTitle>
      <Link to="/tools">返回工具与能力</Link>
      <p className="text-muted">
        为独立子任务保存角色提示、工具和
        Skills。角色只读、无递归，继承主任务的模型并共享预算，最多两个并发。
      </p>
      <p className="text-sm text-muted">
        新任务使用新配置；停用、删除或收窄许可会阻止旧绑定的后续调用。创建配置不会自动启动任务。
      </p>
      <ErrorBox message={result.error || templates.error} />
      {result.loading && <Loading />}
      {search.get('effective') && (
        <EffectiveCapabilities
          key={search.get('effective')}
          roleId={search.get('effective')!}
          version={version}
          initialOpen
        />
      )}
      {!result.loading && !result.error && result.data?.length === 0 && (
        <Empty title="尚无子Agent配置">
          可新建角色，或选用下方模板。未启用角色时，主Agent不会委派给默认研究者。
        </Empty>
      )}
      <div className="grid gap-5 xl:grid-cols-2">
        {result.data?.map((row) => (
          <RoleCard key={`${row.id}:${row.revision}`} row={row} changed={changed} />
        ))}
      </div>
      {!!templates.data?.length && (
        <section aria-label="可选角色模板" className="mt-8">
          <h2 className="text-lg font-semibold">从模板开始</h2>
          <p className="text-sm text-muted">模板只填入初始内容，保存后可以自由修改、停用或删除。</p>
          <div className="grid gap-4 xl:grid-cols-2">
            {templates.data.map((template) => (
              <article
                key={template.id}
                className="rounded-xl border border-dashed border-line p-5"
              >
                <h3 className="mt-0 text-base font-semibold">{template.display_name}</h3>
                <p className="text-sm text-muted">{template.description}</p>
                <Link
                  className="button button-secondary"
                  to={`/subagents/new?template=${encodeURIComponent(template.id)}`}
                >
                  使用{template.display_name}模板
                </Link>
              </article>
            ))}
          </div>
        </section>
      )}
      {historyId && (
        <section aria-label="配置历史" className="mt-6 rounded-xl border border-line bg-white p-5">
          <div className="flex items-center justify-between gap-3">
            <h2>配置历史</h2>
            <Button className="button-quiet" onClick={() => setSearch({})}>
              关闭历史
            </Button>
          </div>
          <p className="text-sm text-muted">
            旧地址保留供核对，不会恢复已删除的角色或改变当前配置。
          </p>
          <ErrorBox message={history.error} />
          {history.loading && <Loading />}
          {!history.loading && history.data?.length === 0 && <p>暂无历史版本。</p>}
          {history.data?.map((row) => (
            <details key={row.revision} className="mb-4 rounded-lg border border-line p-4" open>
              <summary>
                {row.display_name} · 修订 {row.revision} ·{' '}
                {row.deleted ? '已删除' : row.enabled ? '已启用' : '已停用'}
              </summary>
              <p>{row.description}</p>
              <p className="text-xs text-muted">
                已保存工具：{(row.resolved_tools ?? row.tools).join('、') || '无'} · Skills：
                {row.skills.join('、') || '无'}
              </p>
              <pre className="whitespace-pre-wrap break-words">{row.system_prompt}</pre>
            </details>
          ))}
        </section>
      )}
    </>
  );
}
