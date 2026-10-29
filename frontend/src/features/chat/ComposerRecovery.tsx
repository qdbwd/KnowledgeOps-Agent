import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Button } from '../../shared/ui/Button';
import { Dialog } from '../../shared/ui/Dialog';
import { ErrorBox } from '../../shared/ui/components';
import type { ComposerSession, ComposerState } from './drafts/session';

/** 普通保存用轻量状态；有冲突或未知提交时才展开恢复操作，不把请求键暴露给用户。 */
export function ComposerRecovery({ state, session }: { state: ComposerState; session: ComposerSession }) {
  const [discarding, setDiscarding] = useState<number | null>(null);
  const row = state.record;
  const pending = row?.pending;
  const occupied = Boolean(state.data.content || state.data.selected.length);
  return <div className="mb-2 max-h-[35dvh] overflow-y-auto text-xs" aria-label="草稿与提交状态">
    <ErrorBox message={state.storageError || state.error} />
    {!state.ready && <p role="status">正在恢复草稿…</p>}
    {state.storageError && <Button className="button-quiet text-xs" onClick={() => void session.start()}>重试保存草稿</Button>}
    {row?.deleted ? <p role="alert">会话已删除，旧输入和待提交记录已清除。<Link to="/">开始新对话 →</Link></p> : <>
      {pending && <section className="mb-2 rounded-lg border border-line bg-canvas p-3" aria-label="待核对的提交">
        <strong>{state.busy ? '正在处理提交…' : pending.state === 'rejected' ? '上次提交未被接受' : pending.state === 'not_accepted' ? '尚未查到受理记录' : '上次提交结果待核对'}</strong>
        <p className="my-2">{pending.error || (pending.state === 'not_accepted' ? '可以继续原提交；将使用之前保存的内容和选项。' : '重新打开页面只会核对结果，不会自动再次发送。')}</p>
        <details><summary>查看该次提交内容</summary><p className="my-2 max-h-32 overflow-auto whitespace-pre-wrap [overflow-wrap:anywhere]">{pending.data.content || '准备会话材料'}</p></details>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Button className="button-secondary text-xs" disabled={state.busy} onClick={() => void session.check()}>核对提交结果</Button>
          {pending.state === 'not_accepted' && <Button disabled={state.busy} onClick={() => void session.retry()}>继续原提交</Button>}
          {pending.state === 'rejected' && <Button disabled={state.busy} onClick={() => void session.editRejected()}>返回编辑</Button>}
          {row.conversationId && <Link to={`/chat/${row.conversationId}`}>打开对应会话 →</Link>}
        </div>
      </section>}
      {Boolean(row?.conflicts.length) && <details open className="mb-2 rounded-lg border border-line bg-canvas p-3">
        <summary>草稿存在不同版本，已分别保留</summary>
        <p className="my-2">选择一份继续编辑，其他版本将在确认选择后舍弃。未核对的提交内容不会改变。</p>
        {[{ writer: null, data: row!.data }, ...row!.conflicts].map((item, index) => <div key={item.writer ?? 'current'} className="my-2 rounded border border-line bg-white p-2">
          <strong>版本 {index + 1} · {item.data.selected.length} 份材料</strong>
          <p className="my-2 max-h-28 overflow-auto whitespace-pre-wrap [overflow-wrap:anywhere]">{item.data.content || '空白输入'}</p>
          <Button className="button-secondary text-xs" disabled={state.busy || Boolean(state.saving)}
            onClick={() => void session.resolve(item.writer, row!.change)}>使用版本 {index + 1}，舍弃其他版本</Button>
        </div>)}
      </details>}
      {state.ready && occupied && <div className="flex items-center justify-between gap-2 text-muted">
        <span role="status">{state.saving ? '正在保存草稿…' : state.persisted ? '草稿已保存到本浏览器' : '草稿暂未保存'}</span>
        {!pending && <Button className="button-quiet text-xs" disabled={state.busy || Boolean(state.saving) || !row} onClick={() => setDiscarding(row!.change)}>放弃草稿</Button>}
      </div>}
    </>}
    {discarding !== null && <Dialog title="放弃草稿" onClose={() => setDiscarding(null)} footer={<>
      <Button className="button-secondary" onClick={() => setDiscarding(null)}>保留草稿</Button>
      <Button onClick={() => { setDiscarding(null); void session.discard(discarding); }}>确认放弃草稿</Button>
    </>}><p>将移除这份尚未发送的输入、材料选择和冲突副本。已保存的会话材料与聊天不会删除。</p></Dialog>}
  </div>;
}
