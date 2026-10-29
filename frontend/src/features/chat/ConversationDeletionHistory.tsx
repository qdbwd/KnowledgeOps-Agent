import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { messageOf, send, usePagedResource, useResource } from '../../shared/api/api';
import { Button } from '../../shared/ui/Button';
import { ErrorBox, Loading } from '../../shared/ui/components';
import { useWorkspace } from '../../shared/state/workspace';
import { useApplicationActivity } from '../activity/ApplicationActivity';
import type { ManagementNotice } from '../activity/ApplicationActivity';

type Job = { status: string; summary: { title?: string } };

/** 只有用户主动查看时打开管理；失败重试仍走原会话删除接口。 */
export function ConversationDeletionHistory() {
  const [params, setParams] = useSearchParams();
  const selected = params.get('deletion');
  const [opened, setOpened] = useState(false);
  const show = opened || Boolean(selected);
  const activity = useApplicationActivity();
  const { refresh } = useWorkspace();
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const rows = usePagedResource<ManagementNotice>(show ? '/api/activity/management?kind=deletion&target_kind=conversation' : null, tick + activity.change);
  const detail = useResource<Job>(selected ? `/api/conversations/${encodeURIComponent(selected)}/deletion` : null, `${tick}:${activity.change}`);
  return <section className="my-5" aria-label="会话删除管理">
    <Button className="button-quiet" onClick={() => {
      setOpened(!show);
      if (selected) { const next = new URLSearchParams(params); next.delete('deletion'); setParams(next); }
    }}>{show ? '收起删除管理' : '查看删除管理'}</Button>
    {show && <div className="mt-3 rounded-xl border border-line bg-white p-5">
      <h2 className="text-base">会话删除管理</h2>
      <p className="text-sm text-muted">正常删除在后台完成。这里查看未完成的清理，重试会继续原作业。</p>
      <ErrorBox message={error || detail.error || rows.error} />
      {detail.loading && <Loading />}
      {selected && detail.data && <div className="my-4 rounded-lg bg-[#f5f7f1] p-4" aria-label="选中的会话删除">
        <strong>{detail.data.summary?.title || '会话清理'}</strong>
        <p className="text-sm">{detail.data.status === 'failed' ? '清理未完成，可继续重试。'
          : detail.data.status === 'completed' ? '清理已完成，仍有依赖的内容已保留。' : '后台清理已受理。'}</p>
        {detail.data.status === 'failed' && <Button className="button-secondary" disabled={busy} onClick={async () => {
          setBusy(true); setError('');
          try { await send(`/api/conversations/${encodeURIComponent(selected)}/deletion/retry`); setTick((value) => value + 1); activity.refresh(); refresh(); }
          catch (failure) { setError(messageOf(failure)); }
          finally { setBusy(false); }
        }}>重试后台清理</Button>}
        <Button className="button-quiet" onClick={() => { setTick((value) => value + 1); activity.refresh(); }}>刷新进度</Button>
      </div>}
      {rows.data.map((item) => <p key={item.id} className="text-sm"><Link to={item.href}>{item.label} →</Link></p>)}
      {rows.loading && <Loading />}
      {!rows.loading && !rows.error && rows.data.length === 0 && <p className="text-sm text-muted">没有需要处理的会话删除。</p>}
      {rows.next && <Button className="button-secondary" disabled={rows.loading} onClick={() => void rows.loadMore()}>加载更多删除记录</Button>}
    </div>}
  </section>;
}
