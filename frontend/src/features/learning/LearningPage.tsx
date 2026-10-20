import { Button } from '../../shared/ui/Button';
import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { messageOf, send, useResource } from '../../shared/api/api';
import { Empty, ErrorBox, Loading, PageTitle } from '../../shared/ui/components';
import { useWorkspace } from '../../shared/state/workspace';
import type { Conversation } from '../../shared/types/types';

type Unit = {
  id?: string;
  key: string;
  title: string;
  goal: string;
  dependencies: string[];
  status?: string;
  model_calls?: number;
  tool_calls?: number;
  searches?: number;
  draft_id?: string | null;
  result_version_id?: string | null;
  stop_reason?: string | null;
};
export type LearningTask = {
  selected?: boolean;
  id: string;
  conversation_id: string | null;
  conversation_available?: boolean;
  revision: number;
  requirements_revision: number;
  title: string;
  goal: string;
  scope: string;
  topic: string;
  mode: 'interactive' | 'delegated';
  status: string;
  auto_publish: boolean;
  configuration?: Record<string, { value: string; source: { kind: string; message_id?: string } }>;
  units: Unit[];
  stop_reason?: string;
};
const statuses: Record<string, string> = {
  draft: '待明确范围',
  active: '进行中',
  paused: '已暂停',
  waiting: '等待处理',
  completed: '已完成',
  partial: '部分完成',
  cancelled: '已取消',
  pending: '待开始',
  drafting: '整理中',
  reading: '读者追问',
  reviewing: '审读中',
  ready: '待保存',
  published: '已保存',
  blocked: '受阻',
  dependency_blocked: '前置单元受阻',
  awaiting_approval: '等待确认保存',
};

type Collaboration = {
  id: string;
  unit_id: string;
  role: 'reader' | 'reviewer';
  status: string;
  round: number;
  child_run_id: string | null;
  draft_id: string;
  draft_revision: number;
  requirements_revision: number;
  read_ranges: [number, number][];
  material_length: number;
  error: string | null;
  report: {
    verdict?: string;
    sufficient?: boolean;
    summary?: string;
    questions?: { question: string; reason: string }[];
    findings?: { severity: string; problem: string; location: string; evidence: string }[];
    reader_resolutions?: { question_id: string; resolved: boolean; explanation: string }[];
  };
};

function CollaborationPanel({ taskId }: { taskId: string }) {
  const [tick, setTick] = useState(0);
  const task = useResource<LearningTask>(`/api/learning-tasks/${taskId}`, tick);
  const reports = useResource<Collaboration[]>(`/api/learning-tasks/${taskId}/collaboration`, tick);
  const advances = useResource<
    {
      id: string;
      unit_id: string;
      run_id: string;
      sequence: number;
      status: string;
      reason: string | null;
    }[]
  >(`/api/learning-tasks/${taskId}/advances`, tick);
  useEffect(() => {
    if (task.data?.status !== 'active') return;
    const timer = window.setInterval(() => setTick((v) => v + 1), 3000);
    return () => window.clearInterval(timer);
  }, [task.data?.status]);
  return (
    <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
      <h2>当前进展与协作记录</h2>
      <ErrorBox message={task.error || reports.error || advances.error} />
      <Button className="quiet button-quiet" onClick={() => setTick((v) => v + 1)}>
        刷新进展
      </Button>
      {task.data && (
        <p>
          {statuses[task.data.status] ?? task.data.status} · {task.data.stop_reason}
        </p>
      )}
      {task.data?.units.map((unit) => (
        <div
          className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5"
          key={unit.key}
        >
          <h3>
            {unit.title} · {statuses[unit.status ?? 'pending'] ?? unit.status}
          </h3>
          <p>{unit.goal}</p>
          <p className="muted text-muted small text-xs">
            本单元累计：{unit.model_calls ?? 0} 次模型请求，{unit.tool_calls ?? 0} 次工具尝试，
            {unit.searches ?? 0} 次搜索。恢复后继续累计。
          </p>
          {(advances.data ?? [])
            .filter((step) => step.unit_id === unit.id)
            .map((step) => (
              <p key={step.id}>
                {task.data?.conversation_available === false ? (
                  <span>第 {step.sequence} 次推进（原会话已删除）</span>
                ) : (
                  <Link to={`/runs/${step.run_id}`}>第 {step.sequence} 次推进</Link>
                )}{' '}
                · {statuses[step.status] ?? step.status}
                {step.reason ? `：${step.reason}` : ''}
              </p>
            ))}
          {unit.stop_reason && (
            <p className="notice border-l-[3px] border-[#c0a76b] bg-[#f5f0e0] px-4 py-3 text-[13px] [overflow-wrap:anywhere]">
              {unit.stop_reason}
            </p>
          )}
          {unit.draft_id && (
            <Link to={`/knowledge/reviews/${unit.draft_id}`}>查看草稿或保存结果</Link>
          )}
          {(reports.data ?? [])
            .filter((report) => report.unit_id === unit.id)
            .map((report) => (
              <article key={report.id}>
                <h4>
                  {report.role === 'reader' ? '读者追问' : '独立审读'} · 第 {report.round} 轮
                </h4>
                <p className="muted text-muted small text-xs">
                  草稿修订 {report.draft_revision} · 任务要求修订 {report.requirements_revision} ·
                  {report.status === 'completed'
                    ? '已形成报告'
                    : report.status === 'pending'
                      ? '执行中'
                      : '报告未通过程序检查'}
                </p>
                {report.child_run_id && task.data?.conversation_available !== false && (
                  <Link to={`/runs/${report.child_run_id}`}>查看实际子任务</Link>
                )}
                <p>
                  已提供{' '}
                  {report.read_ranges.reduce((total, [start, end]) => total + end - start, 0)} /{' '}
                  {report.material_length} 字符的固定材料
                </p>
                {report.error && (
                  <p className="notice border-l-[3px] border-[#c0a76b] bg-[#f5f0e0] px-4 py-3 text-[13px] [overflow-wrap:anywhere]">
                    {report.error}
                  </p>
                )}
                {report.report.verdict && (
                  <p>
                    {report.report.verdict === 'pass'
                      ? '模型报告通过；正式保存仍检查当前版本与权限。'
                      : '模型建议修订。'}
                  </p>
                )}
                <p>{report.report.summary}</p>
                {report.report.questions?.map((question, index) => (
                  <p key={index}>
                    <strong>{question.question}</strong>
                    <br />
                    {question.reason}
                  </p>
                ))}
                {report.report.findings?.map((finding, index) => (
                  <div key={index}>
                    <p>
                      <strong>
                        {finding.severity === 'blocking' ? '需要处理' : '建议'}：{finding.problem}
                      </strong>
                    </p>
                    <p>
                      位置：{finding.location}；依据：{finding.evidence}
                    </p>
                  </div>
                ))}
                {report.report.reader_resolutions?.map((resolution) => (
                  <p key={resolution.question_id}>
                    {resolution.resolved ? '已回应读者问题' : '读者问题仍未解决'}：
                    {resolution.explanation}
                  </p>
                ))}
              </article>
            ))}
        </div>
      ))}
    </section>
  );
}

export function ConversationLearning({ conversationId }: { conversationId: string }) {
  const { version, refresh } = useWorkspace();
  const rows = useResource<LearningTask[]>(
    `/api/learning-tasks?conversation_id=${encodeURIComponent(conversationId)}`,
    version,
  );
  const current = rows.data?.find((row) => row.selected);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  return (
    <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
      <ErrorBox message={error || rows.error} />
      {current ? (
        <>
          <div className="row flex items-center gap-3 wrap flex-wrap">
            <strong>{current.title}</strong>
            <span>
              {current.mode === 'interactive' ? '交互学习' : '委托整理'} ·{' '}
              {statuses[current.status] ?? current.status}
            </span>
            <Link to={`/learning/${current.id}`}>框架、进展与运行控制</Link>
            <Link to={`/learning-profiles?task_id=${current.id}`}>学习配置</Link>
          </div>
          {(rows.data?.length ?? 0) > 1 && (
            <label>
              当前学习任务
              <select
                value={current.id}
                disabled={busy}
                onChange={async (e) => {
                  setBusy(true);
                  setError('');
                  try {
                    await send(`/api/learning-tasks/${e.target.value}/select`);
                    refresh();
                  } catch (error) {
                    setError(messageOf(error));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {rows.data?.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.title}
                  </option>
                ))}
              </select>
            </label>
          )}
        </>
      ) : (
        <p>
          当前为普通对话。<Link to="/learning">建立学习任务或查看已有任务</Link>
        </p>
      )}
    </section>
  );
}

function TaskEditor({ task, changed }: { task: LearningTask; changed: () => void }) {
  const [controlTick, setControlTick] = useState(0);
  const latest = useResource<LearningTask>(`/api/learning-tasks/${task.id}`, controlTick);
  const controlTask = latest.data ?? task;
  useEffect(() => {
    const timer = window.setInterval(() => setControlTick((value) => value + 1), 3000);
    return () => window.clearInterval(timer);
  }, []);
  const [title, setTitle] = useState(task.title);
  const [goal, setGoal] = useState(task.goal);
  const [scope, setScope] = useState(task.scope);
  const [topic, setTopic] = useState(task.topic);
  const [mode, setMode] = useState(task.mode);
  const [units, setUnits] = useState(task.units);
  const [autoPublish, setAutoPublish] = useState(task.auto_publish);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const workspace = useWorkspace();
  async function action(path: string, body?: unknown, method = 'POST') {
    setBusy(true);
    setError('');
    try {
      await send(`/api/learning-tasks/${task.id}${path}`, body, method);
      changed();
      workspace.refresh();
    } catch (error) {
      setError(messageOf(error));
    } finally {
      setBusy(false);
    }
  }
  function editUnit(key: string, patch: Partial<Unit>) {
    setUnits((rows) => rows.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  }
  return (
    <>
      <p>
        {statuses[controlTask.status] ?? controlTask.status} · 要求修订 {task.requirements_revision}
      </p>
      {controlTask.stop_reason && (
        <p className="notice border-l-[3px] border-[#c0a76b] bg-[#f5f0e0] px-4 py-3 text-[13px] [overflow-wrap:anywhere]">
          {controlTask.stop_reason}
        </p>
      )}
      {task.conversation_available === false && (
        <p className="notice border-l-[3px] border-[#c0a76b] bg-[#f5f0e0] px-4 py-3 text-[13px] [overflow-wrap:anywhere]">
          原会话已删除。学习目标、成果与配置保留，可在新建任务后通过“学习配置与读者角色”复用。
        </p>
      )}
      <ErrorBox message={error} />
      <div className="row flex items-center gap-3 wrap flex-wrap">
        <Button
          disabled={busy || !task.conversation_id || task.conversation_available === false}
          onClick={async () => {
            setBusy(true);
            setError('');
            try {
              await send(`/api/learning-tasks/${task.id}/select`);
              workspace.refresh();
              navigate(`/chat/${task.conversation_id}`);
            } catch (error) {
              setError(messageOf(error));
            } finally {
              setBusy(false);
            }
          }}
        >
          切换到此任务并进入对话
        </Button>
        <Link to={`/learning-profiles?task_id=${task.id}`}>学习配置与读者角色</Link>
      </div>
      <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
        <h2>任务要求与初始框架</h2>
        <p>
          框架可以在聊天中逐步形成，也可以在此编辑。修改要求会撤回旧范围的自动保存许可，已有成果和累计进度保留。
        </p>
        <label>
          任务标题
          <input value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label>
          目标与用途
          <textarea value={goal} maxLength={10000} onChange={(e) => setGoal(e.target.value)} />
        </label>
        <label>
          内容范围与成果要求
          <textarea value={scope} maxLength={10000} onChange={(e) => setScope(e.target.value)} />
        </label>
        <label>
          记忆适用主题
          <input value={topic} maxLength={200} onChange={(e) => setTopic(e.target.value)} />
        </label>
        <label>
          学习模式
          <select value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
            <option value="interactive">交互学习：你决定方向，Agent引导</option>
            <option value="delegated">委托整理：按确认范围持续推进</option>
          </select>
        </label>
        {units.map((unit, index) => (
          <section
            className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5"
            key={unit.key}
          >
            <h3>
              知识单元 {index + 1} · {statuses[unit.status ?? 'pending'] ?? unit.status}
            </h3>
            <label>
              单元标题
              <input
                value={unit.title}
                maxLength={300}
                onChange={(e) => editUnit(unit.key, { title: e.target.value })}
              />
            </label>
            <label>
              要解决的问题
              <textarea
                value={unit.goal}
                maxLength={10000}
                onChange={(e) => editUnit(unit.key, { goal: e.target.value })}
              />
            </label>
            <label>
              前置单元（可多选）
              <select
                multiple
                value={unit.dependencies}
                onChange={(e) =>
                  editUnit(unit.key, {
                    dependencies: Array.from(e.target.selectedOptions).map(
                      (option) => option.value,
                    ),
                  })
                }
              >
                {units
                  .filter((other) => other.key !== unit.key)
                  .map((other) => (
                    <option key={other.key} value={other.key}>
                      {other.title || '未命名单元'}
                    </option>
                  ))}
              </select>
            </label>
            <Button
              className="quiet button-quiet"
              disabled={busy}
              onClick={() => setUnits((rows) => rows.filter((row) => row.key !== unit.key))}
            >
              移除此单元
            </Button>
            {unit.stop_reason && <p>{unit.stop_reason}</p>}
            {unit.draft_id && (
              <Link to={`/knowledge/reviews/${unit.draft_id}`}>查看草稿或保存结果</Link>
            )}
          </section>
        ))}
        <div className="row flex items-center gap-3 wrap flex-wrap">
          <Button
            className="secondary button-secondary"
            disabled={units.length >= 50}
            onClick={() =>
              setUnits((rows) => [
                ...rows,
                {
                  key: crypto.randomUUID(),
                  title: '',
                  goal: '',
                  dependencies: [],
                },
              ])
            }
          >
            添加知识单元
          </Button>
          <Button
            disabled={
              busy ||
              !title.trim() ||
              !goal.trim() ||
              units.some((u) => !u.title.trim() || !u.goal.trim())
            }
            onClick={() =>
              void action(
                '',
                {
                  expected_revision: task.revision,
                  title,
                  goal,
                  scope,
                  topic,
                  mode,
                  units: units.map(({ key, title, goal, dependencies }) => ({
                    key,
                    title,
                    goal,
                    dependencies,
                  })),
                },
                'PATCH',
              )
            }
          >
            保存要求和框架
          </Button>
        </div>
      </section>
      <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
        <h2>确认与运行控制</h2>
        <p>
          确认针对已保存的要求。委托整理需要有限的知识单元；允许自动保存后，仍须通过该版本草稿的独立审读。
        </p>
        <label className="check m-0 flex-row items-center gap-2 font-normal">
          <input
            type="checkbox"
            checked={autoPublish}
            disabled={!task.conversation_id || task.conversation_available === false}
            onChange={(e) => setAutoPublish(e.target.checked)}
          />
          允许在已确认范围内通过审读后自动保存
        </label>
        <div className="row flex items-center gap-3 wrap flex-wrap">
          <Button
            disabled={busy || !task.conversation_id || task.conversation_available === false}
            onClick={() =>
              void action('/confirm', {
                expected_revision: controlTask.revision,
                auto_publish: autoPublish,
              })
            }
          >
            确认已保存的范围
          </Button>
          <Button
            className="secondary button-secondary"
            disabled={busy || controlTask.status !== 'active'}
            onClick={() => void action('/pause', { expected_revision: controlTask.revision })}
          >
            暂停任务
          </Button>
          <Button
            className="secondary button-secondary"
            disabled={
              busy ||
              !task.conversation_id ||
              task.conversation_available === false ||
              !['paused', 'waiting', 'partial'].includes(controlTask.status)
            }
            onClick={() => void action('/resume', { expected_revision: controlTask.revision })}
          >
            继续任务
          </Button>
          <Button
            className="quiet button-quiet"
            disabled={busy || controlTask.status === 'cancelled'}
            onClick={() => void action('/cancel', { expected_revision: controlTask.revision })}
          >
            取消后续工作
          </Button>
        </div>
      </section>
    </>
  );
}

export function LearningPage() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const { version, refresh } = useWorkspace();
  const [tick, setTick] = useState(0);
  const tasks = useResource<LearningTask[]>(taskId ? null : '/api/learning-tasks', tick);
  const detail = useResource<LearningTask>(taskId ? `/api/learning-tasks/${taskId}` : null, tick);
  const conversations = useResource<Conversation[]>(taskId ? null : '/api/conversations', version);
  const [conversationId, setConversationId] = useState('');
  const [title, setTitle] = useState('');
  const [goal, setGoal] = useState('');
  const [mode, setMode] = useState('interactive');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const pendingCreate = useRef({ fingerprint: '', key: '', conversationId: '' });
  return (
    <>
      <PageTitle eyebrow="LEARNING" title={detail.data?.title ?? '学习与整理'} />
      <ErrorBox message={error || tasks.error || detail.error || conversations.error} />
      {(tasks.loading || detail.loading) && <Loading />}
      {taskId ? (
        detail.data && (
          <>
            <TaskEditor
              key={`${taskId}:${detail.data.revision}`}
              task={detail.data}
              changed={() => setTick((v) => v + 1)}
            />
            <CollaborationPanel
              key={`collaboration:${taskId}:${detail.data.revision}`}
              taskId={taskId}
            />
          </>
        )
      ) : (
        <>
          <p>
            推荐一个会话围绕一个学习任务；同一会话也可切换任务，聊天历史会保留。普通聊天不必创建任务。
          </p>
          <p>
            <Link to="/learning-profiles">编辑通用学习配置</Link>
          </p>
          <form
            className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5"
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              setError('');
              try {
                const fingerprint = JSON.stringify([conversationId, title, goal, mode]);
                if (pendingCreate.current.fingerprint !== fingerprint)
                  pendingCreate.current = { fingerprint, key: crypto.randomUUID(), conversationId };
                if (!pendingCreate.current.conversationId)
                  pendingCreate.current.conversationId = (
                    await send<Conversation>('/api/conversations', { title })
                  ).id;
                const task = await send<LearningTask>('/api/learning-tasks', {
                  conversation_id: pendingCreate.current.conversationId,
                  request_key: pendingCreate.current.key,
                  title,
                  goal,
                  mode,
                });
                refresh();
                navigate(`/learning/${task.id}`);
              } catch (error) {
                setError(messageOf(error));
              } finally {
                setBusy(false);
              }
            }}
          >
            <h2>新学习任务</h2>
            <label>
              会话
              <select value={conversationId} onChange={(e) => setConversationId(e.target.value)}>
                <option value="">新建独立会话</option>
                {conversations.data?.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.title}
                  </option>
                ))}
              </select>
            </label>
            <label>
              任务标题
              <input value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} />
            </label>
            <label>
              想了解什么、用于什么
              <textarea value={goal} maxLength={10000} onChange={(e) => setGoal(e.target.value)} />
            </label>
            <label>
              模式
              <select value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="interactive">交互学习</option>
                <option value="delegated">委托整理</option>
              </select>
            </label>
            <Button disabled={busy || !title.trim() || !goal.trim()}>建立任务</Button>
          </form>
          {tasks.data?.length === 0 && (
            <Empty title="还没有学习任务">可先创建任务，再在对话中梳理框架。</Empty>
          )}
          {tasks.data?.map((task) => (
            <section
              className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5"
              key={task.id}
            >
              <Link to={`/learning/${task.id}`}>
                <strong>{task.title}</strong>
              </Link>
              <p>
                {task.mode === 'interactive' ? '交互学习' : '委托整理'} ·{' '}
                {statuses[task.status] ?? task.status}
              </p>
              <p>{task.goal}</p>
            </section>
          ))}
        </>
      )}
    </>
  );
}
