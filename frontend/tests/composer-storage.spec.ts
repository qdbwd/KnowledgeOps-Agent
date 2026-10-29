import { expect, test } from '@playwright/test';

// 直接在真实浏览器的 IndexedDB 上验证事务，不能用内存 Map 证明跨标签保护。
test('draft transactions retain both edits, freeze submissions, and retire only the sent revision', async ({ page }) => {
  await page.goto('/');
  const result = await page.evaluate(async () => {
    const path = '/src/features/chat/drafts/store.ts';
    const { DraftStore, emptyDraft } = await import(/* @vite-ignore */ path);
    const store = new DraftStore(`storage-test-${crypto.randomUUID()}`);
    const initial = await store.open({ conversationId: 'test-chat' });
    const first = await store.save(initial.scope, initial.revision, { ...emptyDraft(), content: '甲' }, 'tab-a');
    const second = await store.save(initial.scope, initial.revision, { ...emptyDraft(), content: '乙' }, 'tab-b');
    let conflictBlocked = false;
    try { await store.prepare(initial.scope, second.revision, 'send'); } catch { conflictBlocked = true; }
    const resolved = await store.resolve(initial.scope, second.change, 'tab-b');
    const prepared = await store.prepare(initial.scope, resolved.revision, 'send');
    const pending = prepared.pending;
    const newer = await store.save(initial.scope, prepared.revision, { ...resolved.data, content: '下一条消息' }, 'tab-b');
    const completed = await store.complete(initial.scope, pending.id, 'run-one');
    const reopened = await new DraftStore(store.namespace).open({ conversationId: 'test-chat' });
    return { first: first.data.content, canonical: second.data.content, alternatives: second.conflicts.map((item: { data: { content: string } }) => item.data.content),
      conflictBlocked, sent: pending.body.content, later: newer.data.content, completed: completed.data.content,
      pending: reopened.pending, reopened: reopened.data.content };
  });
  expect(result).toEqual({ first: '甲', canonical: '甲', alternatives: ['乙'], conflictBlocked: true,
    sent: '乙', later: '下一条消息', completed: '下一条消息', pending: null, reopened: '下一条消息' });
});

test('new drafts keep their identity, legacy data never overwrites saved content, and deleted drafts cannot return', async ({ page }) => {
  await page.goto('/');
  const result = await page.evaluate(async () => {
    const path = '/src/features/chat/drafts/store.ts';
    const { DraftStore, emptyDraft } = await import(/* @vite-ignore */ path);
    const store = new DraftStore(`identity-test-${crypto.randomUUID()}`);
    const original = await store.open({});
    const same = await new DraftStore(store.namespace).open({});
    const independent = await store.open({ newId: crypto.randomUUID() });
    const legacy = await store.open({ conversationId: 'legacy', legacy: { ...emptyDraft(), content: '旧输入', selected: ['material'] } });
    const kept = await store.open({ conversationId: 'legacy', legacy: { ...emptyDraft(), content: '不可覆盖' } });
    const prepared = await store.prepare(original.scope, original.revision, 'materials');
    await store.bind(original.scope, prepared.pending.id, 'created-chat');
    const alias = await store.open({ conversationId: 'created-chat' });
    await store.removeConversation('created-chat');
    let staleSaveBlocked = false;
    try { await store.save(original.scope, original.revision, { ...emptyDraft(), content: '过期页面' }, 'stale'); }
    catch { staleSaveBlocked = true; }
    const deleted = await store.open({ conversationId: 'created-chat' });
    const otherNamespace = await new DraftStore(`${store.namespace}-other`).open({ conversationId: 'created-chat' });
    return { same: original.scope === same.scope, independent: independent.scope !== original.scope,
      legacy: legacy.data, kept: kept.data.content, alias: alias.scope === original.scope,
      deleted: deleted.deleted, content: deleted.data.content, pending: deleted.pending, staleSaveBlocked,
      isolated: !otherNamespace.deleted };
  });
  expect(result.same).toBe(true);
  expect(result.independent).toBe(true);
  expect(result.legacy).toMatchObject({ content: '旧输入', selected: ['material'] });
  expect(result.kept).toBe('旧输入');
  expect(result).toMatchObject({ alias: true, deleted: true, content: '', pending: null, staleSaveBlocked: true, isolated: true });
});
