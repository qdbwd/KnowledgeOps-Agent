import { useEffect, useRef, useState } from 'react';

/** 所有页面共用错误处理；保留后端给出的冲突/权限原因，网络错误另行说明。 */
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    const detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    throw new Error(detail ?? `请求失败 (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export function send<T>(path: string, body?: unknown, method = 'POST'): Promise<T> {
  return api<T>(path, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

/** 页面切换时中止旧请求，防止慢响应覆盖新文章；key 用于操作后刷新。 */
export function useResource<T>(path: string | null, key: number | string = 0) {
  const [resource, setResource] = useState<{
    path: string | null;
    data: T | null;
    loading: boolean;
    error: string;
  }>({ path, data: null, loading: Boolean(path), error: '' });
  useEffect(() => {
    // 数据与资源地址绑定。相同地址刷新可保留原内容；换地址则从空内容开始，
    // 不能在404或网络失败后把上一个来源显示成当前引用的证据。
    setResource((previous) => ({
      path,
      data: previous.path === path ? previous.data : null,
      loading: Boolean(path),
      error: '',
    }));
    if (!path) {
      return;
    }
    const controller = new AbortController();
    api<T>(path, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setResource({ path, data, loading: false, error: '' });
      })
      .catch((error: Error) => {
        if (!controller.signal.aborted && error.name !== 'AbortError')
          setResource((previous) => ({ ...previous, error: error.message, loading: false }));
      });
    return () => controller.abort();
  }, [path, key]);
  // 路由刚变化但effect尚未执行的一次render也不得泄漏旧身份的内容。
  return resource.path === path
    ? { data: resource.data, loading: resource.loading, error: resource.error }
    : { data: null, loading: Boolean(path), error: '' };
}

export const messageOf = (error: unknown) =>
  error instanceof Error ? error.message : String(error);

export type CollectionPage<T> = { items: T[]; next_cursor: string | null };

/** 服务端游标分页；切换筛选/刷新时废弃旧请求，补页失败仍保留已加载内容。 */
export function usePagedResource<T extends { id: string }>(path: string | null, key = 0, retainOnRefresh = false) {
  const [resource, setResource] = useState<{ path: string | null; data: T[] }>({ path, data: [] });
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(Boolean(path));
  const [error, setError] = useState('');
  const generation = useRef(0);
  const controllers = useRef(new Set<AbortController>());
  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    const pendingControllers = controllers.current;
    pendingControllers.add(controller);
    // 运行列表中的补问卡片有尚未提交的输入，刷新同一地址时可保留组件身份。
    // 跨会话/筛选仍清空，慢响应不能把旧任务恢复到新地址。
    setResource((previous) => ({ path, data: retainOnRefresh && previous.path === path ? previous.data : [] }));
    setNext(null);
    setError('');
    setLoading(Boolean(path));
    if (path)
      api<CollectionPage<T>>(path, { signal: controller.signal })
        .then((page) => {
          if (generation.current === current && !controller.signal.aborted) {
            setResource({ path, data: page.items });
            setNext(page.next_cursor);
          }
        })
        .catch((failure: Error) => {
          if (failure.name !== 'AbortError' && generation.current === current)
            setError(failure.message);
        })
        .finally(() => {
          pendingControllers.delete(controller);
          if (generation.current === current && !controller.signal.aborted) setLoading(false);
        });
    return () => {
      for (const pending of pendingControllers) pending.abort();
      pendingControllers.clear();
    };
  }, [path, key, retainOnRefresh]);
  async function loadMore() {
    if (!path || !next || loading) return;
    const current = generation.current;
    const controller = new AbortController();
    controllers.current.add(controller);
    setLoading(true);
    setError('');
    try {
      const page = await api<CollectionPage<T>>(
        `${path}${path.includes('?') ? '&' : '?'}cursor=${encodeURIComponent(next)}`,
        { signal: controller.signal },
      );
      if (generation.current === current && !controller.signal.aborted) {
        setResource((previous) => ({ path,
          data: Array.from(new Map([...(previous.path === path ? previous.data : []), ...page.items]
            .map((item) => [item.id, item])).values()),
        }));
        setNext(page.next_cursor);
      }
    } catch (failure) {
      if (!controller.signal.aborted && generation.current === current)
        setError(messageOf(failure));
    } finally {
      controllers.current.delete(controller);
      if (generation.current === current && !controller.signal.aborted) setLoading(false);
    }
  }
  return { data: resource.path === path ? resource.data : [], next, loading, error, loadMore };
}
export const terminal = (status: string) =>
  ['completed', 'failed', 'cancelled', 'interrupted', 'budget_exhausted'].includes(status);
export const statusText: Record<string, string> = {
  queued: '排队中',
  running: '进行中',
  paused: '已暂停',
  waiting_input: '等待回答',
  waiting_children: '等待子任务',
  completed: '已结束',
  failed: '运行失败',
  cancelled: '已取消',
  interrupted: '等待恢复',
  budget_exhausted: '预算已用尽',
  draft: '待选择',
  approved: '已审核',
  published: '已发布',
  rejected: '已拒绝',
  ready: '可读取',
  pending: '等待解析',
  processing: '正在解析',
  material_ready: '材料已就绪',
  partial: '部分完成',
  answered: '已回答',
  draft_ready: '草稿待审核',
  needs_input: '需要补充信息',
  waiting_approval: '等待操作确认',
  execution_unknown: '需要核对执行结果',
};
