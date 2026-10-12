import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, messageOf, send, useResource } from './api';
import { ErrorBox, Loading, Markdown, PageTitle } from './components';
import type { LearningTask } from './LearningPage';

type Profile = {
  id: string;
  task_id: string | null;
  revision: number;
  content: string;
  enabled: boolean;
  source: { kind: string; filename?: string };
  sha256: string;
};
type Preview = { id: string; content: string; sha256: string; notice: string };
type Effective = {
  default: string;
  profiles: Profile[];
  automatic: { id: string; revision: number; content: string; category: string }[];
  task_configuration?: Record<
    string,
    { value: string; source: { kind: string; message_id?: string } }
  >;
};

const configurationLabels: Record<string, string> = {
  purpose: '用途',
  background: '自述基础',
  focus: '重点',
  detail: '详略与讲解方式',
  examples: '例子要求',
  output: '成果要求',
};

function TaskConfigurationEditor({ task, changed }: { task: LearningTask; changed: () => void }) {
  const [values, setValues] = useState<Record<string, string>>(
    Object.fromEntries(
      Object.keys(configurationLabels).map((key) => [key, task.configuration?.[key]?.value ?? '']),
    ),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return (
    <section className="card">
      <h2>本任务具体要求</h2>
      <p className="muted small">
        这些要求只用于本任务，不自动提升为通用偏好，也不覆盖上方人工文档。
      </p>
      <ErrorBox message={error} />
      {Object.entries(configurationLabels).map(([key, label]) => (
        <label key={key}>
          {label}
          <textarea
            value={values[key]}
            maxLength={3000}
            rows={2}
            onChange={(event) => setValues({ ...values, [key]: event.target.value })}
          />
          {task.configuration?.[key]?.source.message_id && (
            <Link to={`/chat/${task.conversation_id}`}>查看真实对话来源</Link>
          )}
        </label>
      ))}
      <button
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError('');
          try {
            await send(`/api/learning-tasks/${task.id}/configuration`, {
              expected_revision: task.revision,
              changes: Object.fromEntries(
                Object.entries(values).map(([key, value]) => [key, value || null]),
              ),
            });
            changed();
          } catch (error) {
            setError(messageOf(error));
          } finally {
            setBusy(false);
          }
        }}
      >
        保存本任务要求
      </button>
    </section>
  );
}

function TaskConfigurationPanel({ taskId, onChanged }: { taskId: string; onChanged: () => void }) {
  const [tick, setTick] = useState(0);
  const resource = useResource<LearningTask>(`/api/learning-tasks/${taskId}`, tick);
  return (
    <>
      <ErrorBox message={resource.error} />
      {resource.loading && <Loading />}
      {resource.data && (
        <TaskConfigurationEditor
          key={`${taskId}:${resource.data.revision}`}
          task={resource.data}
          changed={() => {
            setTick((value) => value + 1);
            onChanged();
          }}
        />
      )}
      {resource.data && (
        <ConfigurationReuse
          key={`reuse:${taskId}:${resource.data.revision}`}
          task={resource.data}
          changed={() => {
            setTick((value) => value + 1);
            onChanged();
          }}
        />
      )}
      <button className="quiet" onClick={() => setTick((value) => value + 1)}>
        重新加载任务要求（放弃未保存修改）
      </button>
    </>
  );
}

function ConfigurationReuse({ task, changed }: { task: LearningTask; changed: () => void }) {
  const tasks = useResource<LearningTask[]>('/api/learning-tasks');
  const [sourceId, setSourceId] = useState(task.id);
  const history = useResource<LearningTask[]>(`/api/learning-tasks/${sourceId}/history`);
  const [revision, setRevision] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const selected = history.data?.find((row) => String(row.revision) === revision);
  return (
    <details className="card">
      <summary>比较、恢复或复用任务要求</summary>
      <p>
        选择固定版本后预览；应用会替换本任务具体要求。不会复制任务进度、预算、授权或人工角色正文。人工正文可以通过上方导入、导出复用。
      </p>
      <ErrorBox message={error || tasks.error || history.error} />
      <label>
        来源任务
        <select
          value={sourceId}
          onChange={(e) => {
            setSourceId(e.target.value);
            setRevision('');
          }}
        >
          {tasks.data?.map((row) => (
            <option key={row.id} value={row.id}>
              {row.title}
            </option>
          ))}
        </select>
      </label>
      <label>
        来源修订
        <select value={revision} onChange={(e) => setRevision(e.target.value)}>
          <option value="">选择要比较的修订</option>
          {history.data?.map((row) => (
            <option key={row.revision} value={row.revision}>
              修订 {row.revision}
            </option>
          ))}
        </select>
      </label>
      {selected && (
        <>
          <table>
            <thead>
              <tr>
                <th>要求</th>
                <th>当前内容</th>
                <th>选中版本</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(configurationLabels).map(([key, label]) => (
                <tr key={key}>
                  <td>{label}</td>
                  <td>{task.configuration?.[key]?.value ?? '（无）'}</td>
                  <td>{selected.configuration?.[key]?.value ?? '（无）'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <button
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError('');
              try {
                await send(`/api/learning-tasks/${task.id}/configuration-reuse`, {
                  expected_revision: task.revision,
                  source_task_id: selected.id,
                  source_revision: selected.revision,
                });
                changed();
              } catch (error) {
                setError(messageOf(error));
              } finally {
                setBusy(false);
              }
            }}
          >
            应用已预览的任务要求
          </button>
        </>
      )}
    </details>
  );
}

/** 表单绑定加载时的修订；后台出现新编辑时由服务拒绝覆盖，用户文字仍留在表单。 */
function ProfileEditor({ profile, changed }: { profile: Profile; changed: () => void }) {
  const [content, setContent] = useState(profile.content);
  const [history, setHistory] = useState<Profile[] | null>(null);
  const [compareRevision, setCompareRevision] = useState<number | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function change(path: string, body: unknown, method = 'POST') {
    setBusy(true);
    setError('');
    try {
      await send(`/api/learning-profiles/${profile.id}${path}`, body, method);
      changed();
    } catch (error) {
      setError(messageOf(error));
    } finally {
      setBusy(false);
    }
  }
  const previous = history?.find((row) => row.revision === compareRevision);
  return (
    <section className="card">
      <h2>{profile.task_id ? '任务个性化配置' : '通用学习配置'}</h2>
      <p className="muted small">
        {profile.enabled ? '已启用' : '已停用'} · 修订 {profile.revision} · 来源：
        {profile.source.kind === 'import'
          ? `导入 ${profile.source.filename}`
          : profile.source.kind === 'default'
            ? '默认空白人工配置'
            : '人工编辑'}
      </p>
      <ErrorBox message={error} />
      <label>
        人工配置正文
        <textarea
          value={content}
          maxLength={20000}
          rows={10}
          onChange={(e) => setContent(e.target.value)}
          placeholder={
            profile.task_id
              ? '本任务的用途、范围、自述基础、重点、例子与成果要求。'
              : '希望Agent和读者长期遵守的讲解、追问与组织方式。'
          }
        />
      </label>
      <p className="muted small">
        自动记忆不会改写这里的正文。普通修改供下一次运行使用，停用从下一次相关调用开始检查。
      </p>
      <div className="row wrap">
        <button
          disabled={busy}
          onClick={() => void change('', { expected_revision: profile.revision, content }, 'PATCH')}
        >
          保存配置
        </button>
        <button
          className="secondary"
          disabled={busy}
          onClick={() =>
            void change(
              '',
              { expected_revision: profile.revision, enabled: !profile.enabled },
              'PATCH',
            )
          }
        >
          {profile.enabled ? '停用配置' : '启用配置'}
        </button>
        <a href={`/api/learning-profiles/${profile.id}/export`} download>
          导出人工配置
        </a>
        <button
          className="quiet"
          onClick={async () => {
            try {
              const rows = await api<Profile[]>(`/api/learning-profiles/${profile.id}/history`);
              setHistory(rows);
              setCompareRevision(rows[0]?.revision ?? null);
            } catch (error) {
              setError(messageOf(error));
            }
          }}
        >
          比较与恢复版本
        </button>
      </div>
      <label>
        导入 Markdown / SKILL.md
        <input
          type="file"
          accept=".md,text/markdown"
          disabled={busy}
          onChange={async (e) => {
            const file = e.target.files?.[0];
            if (!file) return;
            setBusy(true);
            setError('');
            setPreview(null);
            try {
              if (file.size > 80000) throw new Error('文件过大，请使用20000字符以内的配置。');
              setPreview(
                await send<Preview>(`/api/learning-profiles/${profile.id}/import-preview`, {
                  expected_revision: profile.revision,
                  filename: file.name,
                  content: await file.text(),
                }),
              );
            } catch (error) {
              setError(messageOf(error));
            } finally {
              setBusy(false);
              e.target.value = '';
            }
          }}
        />
      </label>
      {preview && (
        <div className="card">
          <h3>待应用的导入内容</h3>
          <p>{preview.notice}</p>
          <p>仅作为角色说明文本使用；不会执行脚本或开启工具。导入后会替换这一层人工正文。</p>
          <Markdown text={preview.content} />
          <button
            disabled={busy}
            onClick={() =>
              void change('/import-apply', {
                preview_id: preview.id,
                expected_hash: preview.sha256,
              })
            }
          >
            确认应用导入
          </button>
          <button className="quiet" onClick={() => setPreview(null)}>
            放弃导入
          </button>
        </div>
      )}
      {history && (
        <div>
          <label>
            比较历史版本
            <select
              value={compareRevision ?? ''}
              onChange={(e) => setCompareRevision(Number(e.target.value))}
            >
              {history.map((row) => (
                <option key={row.revision} value={row.revision}>
                  修订 {row.revision}
                </option>
              ))}
            </select>
          </label>
          {previous && (
            <div className="row wrap">
              <section className="card">
                <h3>历史修订 {previous.revision}</h3>
                <Markdown text={previous.content || '（无人工配置）'} />
              </section>
              <section className="card">
                <h3>当前修订 {profile.revision}</h3>
                <Markdown text={profile.content || '（无人工配置）'} />
              </section>
            </div>
          )}
          {previous && previous.revision !== profile.revision && (
            <button
              disabled={busy}
              onClick={() =>
                void change('/rollback', {
                  expected_revision: profile.revision,
                  target_revision: previous.revision,
                })
              }
            >
              恢复为修订 {previous.revision} 的内容
            </button>
          )}
        </div>
      )}
    </section>
  );
}

function ProfilePanel({ taskId, onChanged }: { taskId?: string; onChanged: () => void }) {
  const [tick, setTick] = useState(0);
  const resource = useResource<Profile>(
    `/api/learning-profiles${taskId ? `?task_id=${encodeURIComponent(taskId)}` : ''}`,
    tick,
  );
  return (
    <>
      <ErrorBox message={resource.error} />
      {resource.loading && <Loading />}
      {resource.data && (
        <ProfileEditor
          key={`${resource.data.id}:${resource.data.revision}`}
          profile={resource.data}
          changed={() => {
            setTick((value) => value + 1);
            onChanged();
          }}
        />
      )}
      <button className="quiet" onClick={() => setTick((value) => value + 1)}>
        重新加载配置（放弃未保存修改）
      </button>
    </>
  );
}

export function LearningProfilesPage() {
  const [params] = useSearchParams();
  const taskId = params.get('task_id') ?? undefined;
  const [tick, setTick] = useState(0);
  const effective = useResource<Effective>(
    `/api/learning-profiles/effective${taskId ? `?task_id=${encodeURIComponent(taskId)}` : ''}`,
    tick,
  );
  const refresh = () => setTick((value) => value + 1);
  return (
    <>
      <PageTitle eyebrow="LEARNING / PROFILE" title="学习配置与读者角色" />
      <p>
        读者角色使用默认职责、通用学习配置、当前任务配置和相关记忆。它会帮助发现理解障碍，不代表真人同意或学习成果。
      </p>
      <ProfilePanel onChanged={refresh} />
      {taskId && <ProfilePanel key={taskId} taskId={taskId} onChanged={refresh} />}
      {taskId && (
        <TaskConfigurationPanel
          key={`requirements-${taskId}`}
          taskId={taskId}
          onChanged={refresh}
        />
      )}
      <p>
        <Link to={taskId ? `/learning/${taskId}` : '/learning'}>返回学习任务</Link> ·{' '}
        <Link to="/memories">维护自动部分的记忆来源</Link>
      </p>
      {
        <section className="card">
          <h2>{taskId ? '本任务有效配置预览' : '通用有效配置预览'}</h2>
          <a
            href={`/api/learning-profiles/effective/export${taskId ? `?task_id=${encodeURIComponent(taskId)}` : ''}`}
            download
          >
            导出当前完整角色说明
          </a>
          <ErrorBox message={effective.error} />
          {effective.loading && <Loading />}
          {effective.data && (
            <>
              <h3>默认职责</h3>
              <Markdown text={effective.data.default} />
              {effective.data.profiles.map((row) => (
                <div key={row.id}>
                  <h3>
                    {row.task_id ? '任务人工配置' : '通用人工配置'} · 修订 {row.revision}
                  </h3>
                  <Markdown text={row.content || '（无人工配置）'} />
                </div>
              ))}
              <h3>相关记忆生成部分</h3>
              {Object.entries(effective.data.task_configuration ?? {}).map(([key, entry]) => (
                <p key={key}>
                  <strong>{configurationLabels[key] ?? key}：</strong>
                  {entry.value}
                </p>
              ))}
              <p className="muted small">
                由当前有效记忆选择，运行时会根据具体问题调整。修改或停用请进入长期记忆页面。
              </p>
              <ul>
                {effective.data.automatic.map((row) => (
                  <li key={row.id}>
                    {row.content} <Link to="/memories">来源记忆 · 修订 {row.revision}</Link>
                  </li>
                ))}
              </ul>
              {effective.data.automatic.length === 0 && <p>当前没有选中的学习习惯。</p>}
            </>
          )}
        </section>
      }
    </>
  );
}
