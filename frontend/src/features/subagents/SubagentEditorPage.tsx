import { useCallback, useRef, useState } from 'react';
import {
  Link,
  useBeforeUnload,
  useBlocker,
  useNavigate,
  useParams,
  useSearchParams,
} from 'react-router-dom';
import { messageOf, send, useResource } from '../../shared/api/api';
import { Button } from '../../shared/ui/Button';
import { Dialog } from '../../shared/ui/Dialog';
import { ErrorBox, Loading, PageTitle } from '../../shared/ui/components';
import {
  RoleFields,
  emptyFields,
  fieldsOf,
  type Fields,
  type Options,
  type Role,
} from './SubagentFields';

export type RoleTemplate = {
  id: string;
  display_name: string;
  description: string;
  fields: Fields & { enabled: boolean };
};

/** 离开、刷新与冲突恢复分别处理；普通文本编辑不重新展开工具组。 */
function RoleEditor({
  role,
  template,
  options,
  reload,
}: {
  role: Role | null;
  template?: RoleTemplate;
  options: Options;
  reload: () => void;
}) {
  const initial = role ? fieldsOf(role) : (template?.fields ?? emptyFields);
  const [fields, setFields] = useState<Fields>(initial);
  const [enabled, setEnabled] = useState(role?.enabled ?? template?.fields.enabled ?? false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [confirmReload, setConfirmReload] = useState(false);
  const leavingAfterSave = useRef(false);
  const navigate = useNavigate();
  const dirty =
    JSON.stringify(fields) !== JSON.stringify(initial) ||
    enabled !== (role?.enabled ?? template?.fields.enabled ?? false);
  const blocker = useBlocker(() => !leavingAfterSave.current && (dirty || busy));
  useBeforeUnload(
    useCallback(
      (event) => {
        if ((dirty || busy) && !leavingAfterSave.current) {
          event.preventDefault();
          event.returnValue = '';
        }
      },
      [dirty, busy],
    ),
  );

  async function save() {
    setBusy(true);
    setError('');
    try {
      if (role) {
        const { display_name, description, system_prompt } = fields;
        const body: Record<string, unknown> = {
          expected_revision: role.revision,
          display_name,
          description,
          system_prompt,
        };
        for (const field of ['tools', 'tool_groups', 'skills'] as const) {
          if (JSON.stringify(fields[field]) !== JSON.stringify(initial[field]))
            body[field] = fields[field];
        }
        if (enabled !== role.enabled) body.enabled = enabled;
        await send(`/api/subagents/${encodeURIComponent(role.id)}`, body, 'PATCH');
      } else {
        const { tool_groups, ...individual } = fields;
        await send('/api/subagents', {
          ...individual,
          ...(tool_groups === null ? {} : { tool_groups }),
          enabled,
        });
      }
      leavingAfterSave.current = true;
      navigate('/subagents', { replace: true });
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <form
        aria-label={role ? `编辑 ${role.display_name}` : '创建子Agent'}
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
        className="space-y-5 pb-4"
      >
        <section className="rounded-2xl border border-line bg-white p-6 max-md:p-4">
          <h2 className="mt-0 text-lg font-semibold">基本信息与能力</h2>
          {template && (
            <p className="text-sm text-muted">
              已填入「{template.display_name}」模板，可自由调整；保存后成为你的独立角色。
            </p>
          )}
          <RoleFields
            value={fields}
            onChange={setFields}
            options={options}
            disabled={busy}
            creating={!role}
          />
          <label className="check mt-5 flex-row items-center gap-2 font-normal">
            <input
              type="checkbox"
              checked={enabled}
              disabled={busy}
              onChange={(event) => setEnabled(event.target.checked)}
            />
            {role ? '启用此角色' : '创建后启用'}
          </label>
        </section>
        <ErrorBox message={error} />
        {error && role && (
          <Button
            type="button"
            className="button-secondary"
            disabled={busy}
            onClick={() => setConfirmReload(true)}
          >
            放弃本地修改并刷新
          </Button>
        )}
        <div className="sticky bottom-3 z-10 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line bg-white p-4 shadow-sm max-md:bottom-2">
          <span role="status" className="text-xs text-muted">
            {busy
              ? '正在保存配置…'
              : dirty
                ? '有未保存的修改'
                : role
                  ? `当前修订 ${role.revision}`
                  : '保存不会启动任务'}
          </span>
          <div className="flex gap-3">
            <Button
              type="button"
              className="button-secondary"
              disabled={busy}
              onClick={() => navigate('/subagents')}
            >
              取消
            </Button>
            <Button type="submit" disabled={busy || (!!role && !dirty)}>
              {role ? '保存配置' : '创建子Agent'}
            </Button>
          </div>
        </div>
      </form>
      {blocker.state === 'blocked' && (
        <Dialog
          title="离开编辑页面？"
          busy={busy}
          onClose={() => blocker.reset()}
          footer={
            <>
              <Button
                type="button"
                className="button-secondary"
                disabled={busy}
                onClick={() => blocker.reset()}
              >
                继续编辑
              </Button>
              <Button type="button" disabled={busy} onClick={() => blocker.proceed()}>
                放弃修改并离开
              </Button>
            </>
          }
        >
          <p>{busy ? '配置仍在保存，请等待结果。' : '当前修改尚未保存。离开后将丢失这些输入。'}</p>
        </Dialog>
      )}
      {confirmReload && (
        <Dialog
          title="重新加载配置？"
          onClose={() => setConfirmReload(false)}
          footer={
            <>
              <Button
                type="button"
                className="button-secondary"
                onClick={() => setConfirmReload(false)}
              >
                继续编辑
              </Button>
              <Button type="button" onClick={reload}>
                确认放弃并刷新
              </Button>
            </>
          }
        >
          <p>将丢弃本地输入，并读取服务器上的最新配置。</p>
        </Dialog>
      )}
    </>
  );
}

export function SubagentEditorPage() {
  const { definitionId } = useParams();
  const [search] = useSearchParams();
  const [version, setVersion] = useState(0);
  const role = useResource<Role>(
    definitionId ? `/api/subagents/${encodeURIComponent(definitionId)}` : null,
    version,
  );
  const options = useResource<Options>('/api/subagents/options', version);
  const templates = useResource<RoleTemplate[]>(
    !definitionId && search.get('template') ? '/api/subagents/templates' : null,
    version,
  );
  const template = templates.data?.find((item) => item.id === search.get('template'));
  const templateMissing =
    !templates.loading &&
    !templates.error &&
    !!search.get('template') &&
    !definitionId &&
    !template;
  const error =
    role.error ||
    options.error ||
    templates.error ||
    (templateMissing ? '该模板不存在，请返回列表重新选择。' : '');
  const loading = role.loading || options.loading || templates.loading;
  return (
    <>
      <PageTitle eyebrow="子角色配置" title={definitionId ? '编辑子Agent' : '创建子Agent'} />
      <Link to="/subagents">返回子Agent列表</Link>
      <p className="text-muted">
        设置角色的任务分工、提示与可用能力。子Agent保持只读，继承主任务模型并共享预算。
      </p>
      <ErrorBox message={error} />
      {error && (
        <Button className="button-secondary" onClick={() => setVersion((value) => value + 1)}>
          重试加载
        </Button>
      )}
      {loading && <Loading />}
      {!loading &&
        !error &&
        options.data &&
        (!definitionId || role.data) &&
        (role.data?.builtin ? (
          <p>这是旧任务使用的固定定义，仅供历史恢复。请从列表选择模板创建可编辑的角色。</p>
        ) : (
          <RoleEditor
            key={`${definitionId ?? 'new'}:${version}:${search.get('template') ?? ''}`}
            role={role.data}
            template={template}
            options={options.data}
            reload={() => setVersion((value) => value + 1)}
          />
        ))}
    </>
  );
}
