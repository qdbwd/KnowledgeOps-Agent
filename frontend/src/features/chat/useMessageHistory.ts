import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { RefObject } from 'react';
import { api, ApiError, messageOf } from '../../shared/api/api';
import type { Message } from '../../shared/types/types';

export type MessagePage = {
  items: Message[];
  revision: number;
  total: number;
  start: number;
  older_cursor: string | null;
  newer_cursor: string | null;
};
type View = { conversation: string | undefined; page: MessagePage | null };
type ReadingPosition = { id: string; offset: number };

/** 只保存可见消息锚点及其像素位置；DOM增长、慢请求和顺序变化都不能抢走阅读位置。 */
export function useMessageHistory(
  conversation: string | undefined,
  refresh: number,
  anchor: string | null,
  scroll: RefObject<HTMLDivElement | null>,
  following: RefObject<boolean>,
) {
  const [view, setView] = useState<View>({ conversation, page: null });
  const current = useRef(view);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const generation = useRef(0);
  const active = useRef<AbortController | null>(null);
  const position = useRef<ReadingPosition | null>(null);
  const center = useRef<string | null>(null);
  const location = useRef({ conversation, anchor });

  const capture = useCallback((): ReadingPosition | null => {
    const area = scroll.current;
    if (!area) return null;
    const top = area.getBoundingClientRect().top;
    const element = Array.from(area.querySelectorAll<HTMLElement>('[data-message-id]'))
      .find((node) => node.getBoundingClientRect().bottom > top);
    return element ? { id: element.dataset.messageId!, offset: element.getBoundingClientRect().top - top } : null;
  }, [scroll]);

  const request = useCallback(async (direction: 'replace' | 'older' | 'newer', cursor?: string | null, target?: string | null, latest = false, navigateToAnchor = false) => {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    const expected = ++generation.current;
    if (!conversation) {
      const empty = { conversation, page: null };
      current.current = empty;
      setView(empty);
      setLoading(false);
      return;
    }
    const old = current.current;
    const same = old.conversation === conversation;
    const saved = same && !latest && !navigateToAnchor ? capture() : null;
    if (direction !== 'replace' || target) following.current = false;
    if (latest) following.current = true;
    setLoading(true);
    setError('');
    const base = `/api/conversations/${conversation}/messages/page`;
    const params = new URLSearchParams();
    if (cursor) params.set('cursor', cursor);
    else if (target) params.set('anchor', target);
    try {
      let page: MessagePage;
      let replace = direction === 'replace';
      try {
        page = await api<MessagePage>(`${base}?${params}`, { signal: controller.signal });
      } catch (failure) {
        if (!(failure instanceof ApiError) || failure.code !== 'cursor_stale') throw failure;
        // 只能重新读取，不能重复任何提交。旧窗口的顺序不可再拿来拼接。
        const relocate = saved?.id ?? target;
        page = await api<MessagePage>(`${base}${relocate ? `?anchor=${encodeURIComponent(relocate)}` : ''}`, { signal: controller.signal });
        replace = true;
        if (expected === generation.current) setNotice('对话有更新，已保留当前阅读位置。');
      }
      if (controller.signal.aborted || expected !== generation.current) return;
      if (!replace && same && old.page) {
        if (page.revision !== old.page.revision) throw new Error('对话版本已变化，请重新定位消息。');
        const items = direction === 'older' ? [...page.items, ...old.page.items] : [...old.page.items, ...page.items];
        page = { ...page,
          items: Array.from(new Map(items.map((message) => [message.id, message])).values()),
          start: direction === 'older' ? page.start : old.page.start,
          older_cursor: direction === 'older' ? page.older_cursor : old.page.older_cursor,
          newer_cursor: direction === 'newer' ? page.newer_cursor : old.page.newer_cursor,
        };
      }
      position.current = saved;
      center.current = target && !saved ? target : null;
      const next = { conversation, page };
      current.current = next;
      setView(next);
    } catch (failure) {
      if (!controller.signal.aborted && expected === generation.current) setError(messageOf(failure));
    } finally {
      if (!controller.signal.aborted && expected === generation.current) setLoading(false);
    }
  }, [conversation, capture, following]);

  useEffect(() => {
    const same = current.current.conversation === conversation;
    const changed = location.current.conversation !== conversation || location.current.anchor !== anchor;
    location.current = { conversation, anchor };
    // 首次响应尚未到达时（包括 StrictMode 重放）仍使用路由锚点。
    // 用户明确返回最新后，普通状态刷新不能再次跳回 URL 中的历史命中。
    const target = changed || !same || !current.current.page
      ? anchor
      : (!following.current ? capture()?.id : null);
    void request('replace', null, target, false, changed && Boolean(anchor));
    return () => { active.current?.abort(); generation.current += 1; };
  }, [conversation, refresh, anchor, request, capture, following]);

  useLayoutEffect(() => {
    const area = scroll.current;
    if (!area || view.conversation !== conversation) return;
    const saved = position.current;
    const wanted = saved?.id ?? center.current;
    const element = wanted ? Array.from(area.querySelectorAll<HTMLElement>('[data-message-id]'))
      .find((node) => node.dataset.messageId === wanted) : null;
    if (element && !following.current) {
      area.scrollTop += element.getBoundingClientRect().top - area.getBoundingClientRect().top - (saved?.offset ?? area.clientHeight / 3);
    } else if (following.current) area.scrollTop = area.scrollHeight;
    position.current = null;
    center.current = null;
  }, [view, conversation, scroll, following]);

  const page = view.conversation === conversation ? view.page : null;
  return { data: page?.items ?? null, older: page?.older_cursor, newer: page?.newer_cursor, loading, error, notice,
    loadOlder: () => !loading && page?.older_cursor && request('older', page.older_cursor),
    loadNewer: () => !loading && page?.newer_cursor && request('newer', page.newer_cursor),
    latest: () => request('replace', null, null, true),
  };
}
