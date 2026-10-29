import { useEffect, useState, useSyncExternalStore } from 'react';
import { ComposerSession } from './drafts/session';

export function useComposerDraft(conversationId?: string, newId?: string) {
  const [address, setAddress] = useState(() => ({ conversationId, newId, session: new ComposerSession(conversationId, newId) }));
  let current = address;
  if (address.conversationId !== conversationId || address.newId !== newId) {
    // 首页异步取得的新草稿身份写入 URL 时仍是同一次编辑，不能重建缓冲。
    // 否则旧实例刚开始保存的输入可能被新实例较早读到的空记录覆盖。
    const same = address.conversationId === conversationId && (Boolean(conversationId)
      || (Boolean(newId) && address.session.snapshot().record?.scope === `new:${newId}`));
    current = { conversationId, newId, session: same ? address.session : new ComposerSession(conversationId, newId) };
    setAddress(current);
  }
  const session = current.session;
  const state = useSyncExternalStore(session.subscribe, session.snapshot);
  useEffect(() => { void session.start(); return session.stop; }, [session]);
  return { state, session };
}
