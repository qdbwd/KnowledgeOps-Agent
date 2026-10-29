import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { messageOf, send, usePagedResource } from '../../shared/api/api';
import { useWorkspace } from '../../shared/state/workspace';
import { Button } from '../../shared/ui/Button';
import { Badge, Empty, ErrorBox, Loading, PageTitle } from '../../shared/ui/components';
import { DeleteRunButton } from '../runs/DeleteRun';
import { AssetDeletionHistory } from '../sources/AssetDeletion';
import { ManagementActivity, useApplicationActivity } from './ApplicationActivity';

type View = 'attention' | 'active' | 'unread' | 'all';
type ActivityRun = {
  id: string; parent_id: null; conversation_id: string | null; kind: string; task: string; status: string;
  user_deleted_at?: string | null; created_at: string; answer: string; unread: boolean; attention: boolean;
  result_version: number; attention_reasons: string[]; child_count: number; href: string;
  children: { id: string; task: string; status: string; answer: string | null }[];
};
const views: [View, string][] = [['attention', '待处理'], ['active', '进行中'], ['unread', '新结果'], ['all', '全部']];
const reasons: Record<string, string> = { approval: '等待批准', input: '等待回答', unknown: '执行结果待核对', paused: '已暂停', recovery: '需要核对或恢复' };
const kinds: Record<string, string> = { agent: '智能体任务', material: '材料解析', context_compaction: '上下文压缩', memory_classification: '记忆分类' };

function RunList({ view }: { view: View }) {
  const activity = useApplicationActivity();
  const { version } = useWorkspace();
  const [refresh, setRefresh] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const [seen, setSeen] = useState({ activity: activity.change, workspace: version });
  const [error, setError] = useState('');
  const [reading, setReading] = useState<string | null>(null);
  const changed = seen.activity !== activity.change || seen.workspace !== version;
  useEffect(() => {
    if (!expanded && changed) {
      setSeen({ activity: activity.change, workspace: version });
      setRefresh((value) => value + 1);
    }
  }, [expanded, changed, activity.change, version]);
  const rows = usePagedResource<ActivityRun>(`/api/activity/runs?view=${view}`, refresh, true);
  function reload() {
    setExpanded(false); setSeen({ activity: activity.change, workspace: version }); setRefresh((value) => value + 1);
    activity.refresh();
  }
  return <>
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
      <p className="text-sm text-muted">{expanded && changed ? '任务状态有更新，刷新后显示最新列表。' : '查看结果与批准、回答、恢复任务相互独立。'}</p>
      <Button className="button-secondary" onClick={reload}>刷新任务列表</Button>
    </div>
    <ErrorBox message={error || rows.error || activity.error} />
    {view === 'attention' && <ManagementActivity />}
    <div className="grid gap-3">
      {rows.data.map((run) => <article key={run.id} className="min-w-0 rounded-xl border border-line bg-white p-5">
        <div className="flex flex-wrap items-center gap-2">
          <Link className="min-w-0 flex-1 break-words font-semibold" to={run.href}>{run.task}</Link>
          <Badge status={run.status} />
          {run.unread && <span className="rounded bg-emerald-50 px-2 py-1 text-xs text-emerald-800">新结果</span>}
        </div>
        <p className="my-2 text-xs text-muted">{kinds[run.kind] ?? run.kind} · {new Date(run.created_at).toLocaleString()}</p>
        {run.attention && <p className="my-3 text-sm text-amber-800">{run.attention_reasons.map((reason) => reasons[reason] ?? reason).join(' · ')}</p>}
        {run.answer && <p className="my-3 whitespace-pre-wrap break-words text-sm">{run.answer}</p>}
        {run.children.map((child) => <div key={child.id} className="my-2 border-l-2 border-line pl-3 text-sm">
          <Link to={`/runs/${child.id}`}>{child.task}</Link> <Badge status={child.status} />
          {child.answer && <p className="my-1 whitespace-pre-wrap break-words text-muted">{child.answer}</p>}
        </div>)}
        {run.child_count > run.children.length && <Link className="text-xs" to={run.href}>查看全部 {run.child_count} 个子任务 →</Link>}
        <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
          <Link to={run.href}>{run.attention ? '查看并处理' : '查看任务详情'} →</Link>
          {run.conversation_id && <Link to={`/chat/${run.conversation_id}`}>回到会话 →</Link>}
          {run.unread && <Button className="button-secondary text-xs" disabled={reading === run.id} onClick={async () => {
            setReading(run.id); setError('');
            try { await send(`/api/activity/runs/${run.id}/read`, { version: run.result_version }); reload(); }
            catch (failure) { setError(messageOf(failure)); }
            finally { setReading(null); }
          }}>标为已读</Button>}
          <DeleteRunButton run={run} />
        </div>
      </article>)}
    </div>
    {rows.loading && !rows.data.length && <Loading />}
    {!rows.loading && !rows.error && !rows.data.length && <Empty title={`暂无${views.find(([id]) => id === view)?.[1]}任务`}>
      可以通过上方分类查看其他任务。{view === 'attention' && '已读不会隐藏仍需处理的事项。'}
    </Empty>}
    {rows.next && <Button className="button-secondary mt-5" disabled={rows.loading} onClick={() => { setExpanded(true); void rows.loadMore(); }}>加载更多运行</Button>}
  </>;
}

export function ActivityRunsPage() {
  const [params, setParams] = useSearchParams();
  const raw = params.get('view');
  const view: View = views.some(([id]) => id === raw) ? raw as View : 'attention';
  const { data } = useApplicationActivity();
  return <>
    <PageTitle eyebrow="TASKS" title="运行记录"><Link to="/knowledge/reviews">查看知识变更 →</Link></PageTitle>
    <nav className="mb-4 flex flex-wrap gap-2" aria-label="任务分类">
      {views.map(([id, label]) => <Button key={id} className={id === view ? '' : 'button-secondary'} aria-pressed={id === view}
        onClick={() => setParams({ view: id })}>{label}{data ? ` ${data.counts[id]}` : ''}</Button>)}
    </nav>
    <RunList key={view} view={view} />
    <AssetDeletionHistory kind="run" />
  </>;
}
