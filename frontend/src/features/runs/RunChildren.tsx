import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { messageOf, send, useResource } from '../../shared/api/api';
import type { Run } from '../../shared/types/types';
import { Badge, ErrorBox, Markdown } from '../../shared/ui/components';

type ChildRun = {
  task_id: string; agent_name: string; task: string; status: string;
  answer: string; error: string | null; revision: number; last_event: number;
  cancel_requested: boolean; unsettled?: boolean;
};
type Instruction = { id: string; content: string; origin: string; status: string };
const openStates = ['queued', 'running', 'paused', 'waiting_input', 'waiting_children'];

function ChildCard({ child, run, changed }: { child: ChildRun; run: Run; changed: () => void }) {
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [showInstructions, setShowInstructions] = useState(false);
  // 响应丢失时，重试同一正文仍用原身份；编辑正文才创建另一次请求。
  const request = useRef<{ message: string; key: string } | null>(null);
  const path = `/api/runs/${run.root_id}/children/${child.task_id}`;
  const instructions = useResource<Instruction[]>(showInstructions ? `${path}/instructions` : null, child.revision);
  const editable = (child.unsettled ?? openStates.includes(child.status)) && openStates.includes(run.status)
    && !run.cancel_requested && !child.cancel_requested;

  async function act(operation: 'instructions' | 'cancel') {
    setBusy(true);
    setError('');
    try {
      if (operation === 'instructions') {
        if (request.current?.message !== message) request.current = { message, key: crypto.randomUUID() };
        await send(`${path}/instructions`, { message, request_key: request.current.key });
        setMessage('');
        request.current = null;
      } else {
        await send(`${path}/cancel`, {});
      }
      changed();
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }

  return <article className="child-run">
    <div className="row between wrap">
      <strong>{child.agent_name}</strong>
      <span className="row wrap"><Badge status={child.status} />
        <Link to={`/runs/${child.task_id}`}>查看详情</Link>
      </span>
    </div>
    <p className="child-task">{child.task}</p>
    {child.cancel_requested && openStates.includes(child.status) && <p className="muted">正在停止这个子任务，保留已执行结果。</p>}
    {child.error && <p className="muted small">{child.error}</p>}
    {child.answer && <Markdown text={child.answer} />}
    <ErrorBox message={error} />
    {editable && <form onSubmit={(event) => { event.preventDefault(); void act('instructions'); }}>
      <label>给这个子任务补充要求
        <textarea value={message} rows={2} maxLength={20000} disabled={busy}
          onChange={(event) => setMessage(event.target.value)} />
      </label>
      <p className="muted small">{run.status === 'paused'
        ? '补充会保存，整个任务仍保持暂停；继续后在安全点应用。'
        : '在下一安全点应用，不扩大工具权限或重置任务预算。'}</p>
      <div className="row wrap">
        <button className="secondary small" type="submit" disabled={busy || !message.trim()}>发送补充</button>
        <button className="quiet small" type="button" disabled={busy} onClick={() => void act('cancel')}>取消这个子任务</button>
      </div>
    </form>}
    <details onToggle={(event) => setShowInstructions(event.currentTarget.open)}>
      <summary>已发送的补充要求</summary>
      <ErrorBox message={instructions.error} />
      {instructions.loading && <p className="muted">正在读取…</p>}
      {instructions.data?.length === 0 && <p className="muted">还没有追加要求。</p>}
      {instructions.data?.map((entry) => <div key={entry.id} className="child-instruction">
        <span className="muted small">{entry.origin === 'human' ? '你' : '主 Agent'} · {entry.status === 'applied' ? '已应用' : '等待安全点应用'}</span>
        <p>{entry.content}</p>
      </div>)}
    </details>
  </article>;
}

/** 聊天与详情共用同一管理入口；结果、指令和状态都以服务端持久记录为准。 */
export function RunChildren({ run, refreshKey, onChanged }: {
  run: Run; refreshKey: string | number; onChanged: () => void;
}) {
  const [version, setVersion] = useState(0);
  const result = useResource<ChildRun[]>(!run.parent_id && run.child_count
    ? `/api/runs/${run.root_id}/children` : null, `${refreshKey}:${version}`);
  const progressing = result.data?.some((child) => ['queued', 'running'].includes(child.status)) ?? false;
  useEffect(() => {
    if (!progressing) return;
    const timer = window.setInterval(() => setVersion((n) => n + 1), 2000);
    return () => window.clearInterval(timer);
  }, [progressing]);
  function changed() { setVersion((n) => n + 1); onChanged(); }
  if (!result.data?.length) return <ErrorBox message={result.error} />;
  return <section className="card">
    <h2>子任务协作</h2>
    <p className="muted small">子 Agent 独立处理分工，结果交回主 Agent；你可以随时补充要求或取消其中一个。</p>
    <ErrorBox message={result.error} />
    {result.data.map((child) => <ChildCard key={child.task_id} child={child} run={run} changed={changed} />)}
  </section>;
}
