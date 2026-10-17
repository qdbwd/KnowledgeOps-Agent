import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, messageOf, send } from '../../shared/api/api';
import { useWorkspace } from '../../shared/state/workspace';
import { ErrorBox, Loading, PageTitle } from '../../shared/ui/components';

type Preview = {
  conversation_id: string; title: string; state: string; version: string; eligible: boolean;
  blockers: string[]; counts: { messages: number; runs: number; snapshots: number; history_records: number };
  preserved: string[];
};
type Deletion = { id: string | null; target_id: string; status: string; phase: string; last_error: string | null };

function blockerText(reason: string) {
  if (reason.includes('unknown') || reason.includes('ledger_unavailable')) return '有执行结果尚未核实，需先完成核对。';
  if (reason.includes('run_cleanup_in_progress')) return '该会话的运行详情正在清理，请稍后重新预览。';
  if (reason.includes('unconsumed')) return '有子任务结果尚未交接完成。';
  if (reason.includes('snapshot_used')) return '有其他会话仍引用这里的恢复数据，暂时不能删除。';
  return '仍有未结束、暂停、待回答或待审批的执行，请先处理。';
}

/** 删除是持久后台作业，刷新地址仍能查询；离开页面不取消清理。 */
export function DeleteConversationPage() {
  const { conversationId } = useParams();
  return <ConversationDeletion key={conversationId} conversationId={conversationId!} />;
}

function ConversationDeletion({ conversationId }: { conversationId: string }) {
  const { refresh } = useWorkspace();
  const [preview, setPreview] = useState<Preview | null>(null);
  const [job, setJob] = useState<Deletion | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const base = `/api/conversations/${conversationId}`;
  const status = job?.status;
  useEffect(() => {
    const controller = new AbortController();
    api<Preview>(base + '/deletion-preview', { signal: controller.signal }).then(async (value) => {
      const current = value.state !== 'active' ? await api<Deletion>(base + '/deletion', { signal: controller.signal }) : null;
      if (!controller.signal.aborted) { setPreview(value); setJob(current); setError(''); }
    }).catch((failure) => { if (!controller.signal.aborted) setError(messageOf(failure)); });
    return () => controller.abort();
  }, [base, tick]);
  useEffect(() => {
    if (!status || ['completed', 'failed'].includes(status)) return;
    const controller = new AbortController();
    const timer = setInterval(() => {
      api<Deletion>(base + '/deletion', { signal: controller.signal }).then((value) => {
        if (controller.signal.aborted) return;
        setJob(value); setError('');
        if (value.status === 'completed') refresh();
      }).catch((failure) => { if (!controller.signal.aborted) setError(messageOf(failure)); });
    }, 1500);
    return () => { clearInterval(timer); controller.abort(); };
  }, [base, status, refresh]);
  async function act(retry = false) {
    if (!preview || busy) return;
    setBusy(true); setError('');
    try {
      setJob(await send<Deletion>(base + (retry ? '/deletion/retry' : ''),
        retry ? undefined : { expected_version: preview.version }, retry ? 'POST' : 'DELETE'));
      refresh();
    } catch (failure) { setError(messageOf(failure)); }
    finally { setBusy(false); }
  }
  return <>
    <PageTitle eyebrow="CONVERSATION" title={status === 'completed' ? '会话已删除' : '删除会话'} />
    <ErrorBox message={error} />
    {!preview && !error && <Loading />}
    {preview && <section className="card">
      {job ? <div role="status">
        <h2>{status === 'completed' ? '聊天及其上下文已清理' : status === 'failed' ? '清理未完成，可重试' : '正在清理会话'}</h2>
        <p>{status === 'completed' ? '独立资料、知识、草稿、文件和学习配置已保留。' : '会话已停止读写。后台分步处理，关闭此页面后仍会继续；隐藏会话不代表清理已经完成。'}</p>
        {status === 'failed' && <button disabled={busy} onClick={() => void act(true)}>重试清理</button>}
      </div> : <>
        <h2>{preview.title}</h2>
        <p>将删除 {preview.counts.messages} 条聊天消息及其上下文、历史检索副本和 {preview.counts.runs} 次运行的执行详情。删除后不能恢复这段聊天。</p>
        <p>以下内容会保留：{preview.preserved.join('、')}。学习任务将解除会话关联并停止自动推进。</p>
        {!preview.eligible && <div className="notice" role="status">
          <strong>当前不能删除</strong>
          <ul>{[...new Set(preview.blockers.map(blockerText))].map((reason) => <li key={reason}>{reason}</li>)}</ul>
        </div>}
        <div className="row wrap">
          <button disabled={busy || !preview.eligible} onClick={() => void act()}>确认删除会话</button>
          <Link className="button secondary" to={`/chat/${conversationId}`}>返回对话</Link>
        </div>
      </>}
      <div className="row wrap">
        <button className="quiet" disabled={busy} onClick={() => setTick((n) => n + 1)}>刷新状态</button>
        {job && <Link to="/">开始新对话 →</Link>}
        {job && <Link to="/learning">查看保留的学习任务 →</Link>}
      </div>
    </section>}
  </>;
}
