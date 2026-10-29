import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { api, messageOf, usePagedResource } from '../../shared/api/api';
import { Button } from '../../shared/ui/Button';
import { ErrorBox, Loading } from '../../shared/ui/components';

export type ActivityFlags = { attention: boolean; active: boolean; unread: boolean };
export type ManagementNotice = { id: string; kind: string; status: string; label: string; href: string; attention: boolean; target_id?: string };
export type ActivitySummary = {
  revision: number;
  counts: { all: number; attention: number; active: number; unread: number };
  conversations: Record<string, ActivityFlags>;
  management: { attention: number; items: ManagementNotice[]; truncated: boolean };
  checked_at: string;
};
type State = { data: ActivitySummary | null; error: string; change: number; refresh: () => void };
const ActivityContext = createContext<State>({ data: null, error: '', change: 0, refresh: () => {} });
export const useApplicationActivity = () => useContext(ActivityContext);
const foreground = () => document.visibilityState !== 'hidden';

/** 旧后端或异常响应可能缺少新增字段；外壳按空活动展示，不能整页崩溃。 */
function normalizeSummary(value: Partial<ActivitySummary> | null | undefined): ActivitySummary {
  return {
    revision: value?.revision ?? 0,
    counts: { all: 0, attention: 0, active: 0, unread: 0, ...(value?.counts ?? {}) },
    conversations: value?.conversations ?? {},
    management: { attention: 0, items: [], truncated: false, ...(value?.management ?? {}) },
    checked_at: value?.checked_at ?? '',
  };
}

/** Layout 只挂一次观察者。页面隐藏立即中止请求，重新聚焦只读核对，不触发任务。 */
export function ApplicationActivity({ conversationIds, version, children }: {
  conversationIds: string[]; version: number; children: ReactNode;
}) {
  const ids = [...new Set(conversationIds)].slice(0, 40).sort().join(',');
  const [tick, setTick] = useState(0);
  const [state, setState] = useState<Omit<State, 'refresh'>>({ data: null, error: '', change: 0 });
  const refresh = useCallback(() => setTick((value) => value + 1), []);
  useEffect(() => {
    let alive = true;
    let inFlight: AbortController | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      if (!alive || !foreground() || inFlight) return;
      clearTimeout(timer);
      const controller = new AbortController();
      inFlight = controller;
      // 请求本身有界，异常连接不能永远占用观察者。
      const timeout = setTimeout(() => controller.abort('timeout'), 6000);
      try {
        const data = normalizeSummary(await api<Partial<ActivitySummary>>(`/api/activity?conversation_ids=${encodeURIComponent(ids)}`, { signal: controller.signal }));
        if (alive && !controller.signal.aborted) setState((previous) => {
          const before = previous.data ? { ...previous.data, checked_at: '' } : null;
          const changed = JSON.stringify(before) !== JSON.stringify({ ...data, checked_at: '' });
          return { data, error: '', change: previous.change + (changed ? 1 : 0) };
        });
      } catch (failure) {
        if (alive && (!controller.signal.aborted || controller.signal.reason === 'timeout'))
          setState((previous) => ({ ...previous, error: controller.signal.reason === 'timeout'
            ? '任务状态检查超时，显示上次结果；恢复连接后会继续核对。'
            : `暂时无法更新任务状态：${messageOf(failure)}` }));
      } finally {
        clearTimeout(timeout);
        if (inFlight === controller) {
          inFlight = null;
          if (alive && foreground()) timer = setTimeout(() => void poll(), 5000);
        }
      }
    };
    const visibility = () => {
      clearTimeout(timer);
      if (document.visibilityState === 'hidden') {
        const pending = inFlight; inFlight = null; pending?.abort(); return;
      }
      void poll();
    };
    void poll();
    document.addEventListener('visibilitychange', visibility);
    window.addEventListener('focus', visibility);
    window.addEventListener('online', visibility);
    return () => {
      alive = false; clearTimeout(timer); inFlight?.abort();
      document.removeEventListener('visibilitychange', visibility);
      window.removeEventListener('focus', visibility); window.removeEventListener('online', visibility);
    };
  }, [ids, version, tick]);
  const value = useMemo(() => ({ ...state, refresh }), [state, refresh]);
  return <ActivityContext.Provider value={value}>{children}</ActivityContext.Provider>;
}

export function ActivityIndicator() {
  const { data, error } = useApplicationActivity();
  if (error) return <span className="text-xs text-amber-200" title={error} aria-label="任务状态暂未更新">!</span>;
  if (!data) return null;
  const attention = data.counts.attention + data.management.attention;
  return <span className="ml-auto flex gap-1 text-[10px]" aria-label={`${attention} 项待处理，${data.counts.unread} 项新结果`}>
    {attention > 0 && <span className="rounded bg-amber-100 px-1.5 text-amber-900">{attention}</span>}
    {data.counts.unread > 0 && <span className="rounded bg-[#d9ebe1] px-1.5 text-[#214e3c]">{data.counts.unread}</span>}
  </span>;
}

export function ConversationActivity({ id }: { id: string }) {
  const { data } = useApplicationActivity();
  const item = data?.conversations[id];
  if (!item) return null;
  const label = item.attention ? '待处理' : item.active ? '进行中' : item.unread ? '有新结果' : '';
  return label ? <span className="shrink-0 text-[10px] text-[#c2d5c9]" title={label}>{label}{item.unread && label !== '有新结果' ? ' · 新' : ''}</span> : null;
}

function ManagementList({ kind }: { kind: 'mcp' | 'deletion' }) {
  const [tick, setTick] = useState(0);
  const rows = usePagedResource<ManagementNotice>(`/api/activity/management?kind=${kind}`, tick);
  return <div className="mt-4 border-t border-line pt-3">
    <div className="flex items-center justify-between gap-2"><h3 className="text-sm">{kind === 'mcp' ? '工具服务操作' : '未完成的删除'}</h3>
      <Button className="button-quiet text-xs" onClick={() => setTick((value) => value + 1)}>刷新{kind === 'mcp' ? '工具服务操作' : '删除记录'}</Button></div>
    <ErrorBox message={rows.error} />
    {rows.data.map((item) => <Notice key={item.id} item={item} />)}
    {rows.loading && <Loading />}
    {!rows.loading && !rows.error && !rows.data.length && <p className="text-xs text-muted">暂无记录</p>}
    {rows.next && <Button className="button-secondary text-xs" disabled={rows.loading} onClick={() => void rows.loadMore()}>加载更多{kind === 'mcp' ? '工具服务操作' : '删除记录'}</Button>}
  </div>;
}

function Notice({ item }: { item: ManagementNotice }) {
  return <div className="my-2 flex flex-wrap items-center gap-2 text-sm">
    <span className="break-words">{item.label}</span><span className="text-muted">{item.attention ? '需要处理' : item.status === 'waiting' ? '等待依赖解除' : '进行中'}</span>
    <Link to={item.href}>查看并处理 →</Link>
  </div>;
}

export function ManagementActivity() {
  const { data } = useApplicationActivity();
  const [expanded, setExpanded] = useState(false);
  if (!data?.management.items.length && !expanded) return null;
  return <section className="mb-5 rounded-xl border border-line bg-white p-4" aria-label="其他后台操作">
    <h2 className="mb-3 text-sm">其他后台操作</h2>
    {expanded ? <><ManagementList kind="mcp" /><ManagementList kind="deletion" /></>
      : data?.management.items.map((item) => <Notice key={item.id} item={item} />)}
    {(data?.management.truncated || expanded) && <Button className="button-quiet text-xs" onClick={() => setExpanded(!expanded)}>
      {expanded ? '收起后台操作' : '查看全部后台操作'}</Button>}
  </section>;
}
