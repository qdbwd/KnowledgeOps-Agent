import { createContext, useContext, useEffect, useMemo } from 'react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useApplicationActivity } from '../activity/ApplicationActivity';

const DeletionContext = createContext<{ track: (id: string) => void }>({ track: () => {} });
export const useBackgroundDeletions = () => useContext(DeletionContext);

/** PG 的清理作业是唯一事实来源；共用 Layout 观察者，不再逐会话启动轮询。 */
export function BackgroundDeletions({ children }: { children: ReactNode }) {
  const { data, refresh, error } = useApplicationActivity();
  const context = useMemo(() => ({ track: refresh }), [refresh]);
  useEffect(() => {
    if (data) {
      try { localStorage.removeItem('knowledgeops:pending-conversation-deletions'); }
      catch { /* 旧浏览器索引不影响服务端作业和新的查询入口。 */ }
    }
  }, [data]);
  const failures = data?.management.items.filter((item) => item.kind === 'deletion' && item.target_id) ?? [];
  return <DeletionContext.Provider value={context}>
    {children}
    {failures.length > 0 && <div role="alert"
      className="fixed right-4 bottom-4 z-40 w-[min(380px,calc(100vw_-_32px))] rounded-xl border border-line bg-white p-4 shadow-lg">
      <p className="m-0 mb-2 text-sm">有会话的后台清理未完成。</p>
      {error && <p className="text-xs text-muted">当前显示上次检查结果，恢复连接后会继续核对。</p>}
      <Link className="text-sm" to={failures[0].href}>查看并处理 →</Link>
    </div>}
  </DeletionContext.Provider>;
}
