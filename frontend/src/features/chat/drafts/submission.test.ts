import 'fake-indexeddb/auto';
import { afterEach, expect, it, vi } from 'vitest';
import { ApiError } from '../../../shared/api/api';
import { DraftStore, emptyDraft } from './store';
import { dispatchSubmission, reconcileSubmission } from './submission';
import { ComposerSession } from './session';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function prepared(conversationId?: string) {
  const store = new DraftStore(crypto.randomUUID());
  let row = await store.open({ conversationId });
  row = await store.save(row.scope, row.revision, { ...emptyDraft(), content: '原请求', selected: ['source'] }, 'one');
  return { store, row: await store.prepare(row.scope, row.revision, 'send') };
}

it('reconciles a lost accepted response without posting again or clearing newer input', async () => {
  const { store, row } = await prepared('chat');
  const http = { get: vi.fn().mockResolvedValue({ state: 'accepted', run_id: 'original-run' }),
    post: vi.fn().mockRejectedValue(new TypeError('response lost')) };
  const uncertain = await dispatchSubmission(store, row.scope, true, http);
  expect(uncertain.pending?.state).toBe('unknown');
  await store.save(row.scope, row.revision, { ...row.data, content: '之后输入的消息' }, 'one');
  const receipt = await reconcileSubmission(store, row.scope, http);
  expect(http.post).toHaveBeenCalledTimes(1);
  expect(http.get.mock.calls[0][0]).toContain('submission-receipt');
  expect(receipt.data.content).toBe('之后输入的消息');
  expect(receipt.pending).toBeNull();
  expect(receipt.lastAccepted?.runId).toBe('original-run');
});

it('recovers creation identity after reload, and only explicit continuation sends the frozen message', async () => {
  const { store, row } = await prepared();
  const http = {
    get: vi.fn().mockImplementation(async (path: string) => path.includes('creation-receipt')
      ? { state: 'accepted', conversation_id: 'same-created-chat' }
      : path.includes('runtime-settings') ? { revision: 0, permission_mode: 'ask', reasoning_effort: 'default' }
        : { state: 'not_accepted' }),
    post: vi.fn().mockRejectedValueOnce(new TypeError('creation response lost')).mockResolvedValue({ id: 'run' }),
  };
  await dispatchSubmission(store, row.scope, true, http);
  const reopened = await reconcileSubmission(new DraftStore(store.namespace), row.scope, http);
  expect(reopened.conversationId).toBe('same-created-chat');
  expect(reopened.pending?.state).toBe('not_accepted');
  expect(http.post).toHaveBeenCalledTimes(1);
  await store.save(row.scope, row.revision, { ...row.data, content: '修改后的草稿', auxiliary: true }, 'one');
  const accepted = await dispatchSubmission(store, row.scope, false, http);
  expect(http.post.mock.calls[1][0]).toBe('/api/conversations/same-created-chat/messages');
  expect(http.post.mock.calls[1][1]).toEqual(row.pending?.body);
  expect(http.post).toHaveBeenCalledTimes(2);
  expect(accepted.data.content).toBe('修改后的草稿');
});

it('does not release uncertain requests on missing receipts or repeat POST during a failed check', async () => {
  const { store, row } = await prepared('chat');
  const http = { get: vi.fn().mockRejectedValue(new TypeError('offline')), post: vi.fn() };
  await reconcileSubmission(store, row.scope, http);
  await dispatchSubmission(store, row.scope, false, http);
  expect(http.post).not.toHaveBeenCalled();
  const pending = (await store.read(row.scope)).pending!;
  expect(pending.uncertain).toBe(true);
  await expect(store.releaseRejected(row.scope, pending.id)).rejects.toThrow('需继续核对');
});

it('returns a definite initial validation rejection to editing, but keeps uncertainty after an earlier lost response', async () => {
  const { store, row } = await prepared('chat');
  const http = { get: vi.fn().mockResolvedValue({ state: 'not_accepted' }),
    post: vi.fn().mockRejectedValue(new ApiError('材料不可用', 422)) };
  const rejected = await dispatchSubmission(store, row.scope, true, http);
  expect(rejected.pending?.state).toBe('rejected');
  const editable = await store.releaseRejected(row.scope, rejected.pending!.id);
  expect(editable.data.content).toBe('原请求');
  const next = await store.prepare(row.scope, editable.revision, 'send');
  expect(next.pending?.body.request_key).not.toBe(row.pending?.body.request_key);
  await store.updatePending(row.scope, next.pending!.id, { uncertain: true });
  const unknown = await dispatchSubmission(store, row.scope, false, http);
  expect(unknown.pending?.state).toBe('unknown');
});

it('prevents a delayed old acknowledgement from clearing a later submission', async () => {
  const { store, row } = await prepared('chat');
  const done = await store.complete(row.scope, row.pending!.id, 'first');
  const edited = await store.save(row.scope, done.revision, { ...row.data, content: '下一次' }, 'one');
  const next = await store.prepare(row.scope, edited.revision, 'send');
  const late = await store.complete(row.scope, row.pending!.id, 'first');
  expect(late.pending?.id).toBe(next.pending?.id);
  expect(late.data.content).toBe('下一次');
});

it('does not post when the durable pending identity cannot be saved', async () => {
  const namespace = crypto.randomUUID();
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => Response.json(options?.method === 'POST'
    ? { id: 'unexpected-post' } : url === '/api/client-state'
      ? { namespace, contract_version: 1 } : { permission_mode: 'ask', reasoning_effort: 'default', revision: 0 }));
  vi.stubGlobal('fetch', fetcher);
  const session = new ComposerSession('chat');
  await session.start();
  session.edit({ content: '不能丢失的输入' });
  await session.flush();
  vi.spyOn(DraftStore.prototype, 'prepare').mockRejectedValueOnce(new DOMException('quota', 'QuotaExceededError'));
  await session.submit();
  expect(fetcher.mock.calls.every(([, options]) => !['POST', 'PATCH'].includes(options?.method ?? 'GET'))).toBe(true);
  expect(session.snapshot().data.content).toBe('不能丢失的输入');
  expect(session.snapshot().error).toContain('quota');
  session.stop();
});

it('reconciles a lost settings response without repeating PATCH or sending on reload', async () => {
  const store = new DraftStore(crypto.randomUUID());
  let row = await store.open({});
  row = await store.save(row.scope, row.revision, { ...emptyDraft(), content: '原内容', permission: 'auto_review' }, 'one');
  row = await store.prepare(row.scope, row.revision, 'send');
  let settings = { permission_mode: 'ask', reasoning_effort: 'default', revision: 0 };
  const http = {
    get: vi.fn().mockImplementation(async (path: string) => path.includes('runtime-settings') ? settings : { state: 'not_accepted' }),
    post: vi.fn().mockImplementation(async (path: string) => {
      if (path === '/api/conversations') return { id: 'created' };
      if (path.endsWith('/runtime-settings')) {
        settings = { ...settings, permission_mode: 'auto_review', revision: 1 };
        throw new TypeError('settings response lost');
      }
      return { id: 'accepted-run' };
    }),
  };
  const interrupted = await dispatchSubmission(store, row.scope, true, http);
  expect(interrupted.pending).toMatchObject({ stage: 'settings', state: 'unknown' });
  const checked = await reconcileSubmission(new DraftStore(store.namespace), row.scope, http);
  expect(checked.pending).toMatchObject({ needsSettings: false, state: 'not_accepted' });
  expect(http.post).toHaveBeenCalledTimes(2);
  const accepted = await dispatchSubmission(store, row.scope, false, http);
  expect(accepted.lastAccepted?.runId).toBe('accepted-run');
  expect(http.post.mock.calls.map(([path]) => path)).toEqual([
    '/api/conversations', '/api/conversations/created/runtime-settings', '/api/conversations/created/messages',
  ]);
});

it('preserves both edits when a remote refresh finishes after local typing has started', async () => {
  const namespace = crypto.randomUUID();
  vi.stubGlobal('fetch', vi.fn(async (path: string) => Response.json(path === '/api/client-state'
    ? { namespace, contract_version: 1 } : { revision: 0 })));
  const store = new DraftStore(namespace);
  let initial = await store.open({ conversationId: 'chat' });
  initial = await store.save(initial.scope, initial.revision, { ...emptyDraft(), content: '共同起点' }, 'other');
  const session = new ComposerSession('chat');
  await session.start();
  await store.save(initial.scope, initial.revision, { ...initial.data, content: '远端修改' }, 'other');
  const refreshing = session.refresh();
  session.edit({ content: '基于共同起点的本地修改' });
  await refreshing;
  await session.flush();
  const kept = await store.read(initial.scope);
  expect(kept.data.content).toBe('远端修改');
  expect(kept.conflicts.map((copy) => copy.data.content)).toEqual(['基于共同起点的本地修改']);
  expect(session.snapshot().data.content).toBe('基于共同起点的本地修改');
  session.stop();
});

it('refuses a stale conflict choice or discard confirmation instead of deleting a newer edit', async () => {
  const { store, row } = await prepared('chat');
  await store.complete(row.scope, row.pending!.id, 'done');
  const current = await store.read(row.scope);
  const other = await store.save(row.scope, current.revision, { ...current.data, content: '另一标签的更新' }, 'other');
  await expect(store.abandon(row.scope, current.change)).rejects.toThrow('草稿已变化');
  await expect(store.resolve(row.scope, current.change, null)).rejects.toThrow('草稿又有变化');
  expect((await store.read(row.scope)).data.content).toBe(other.data.content);
});
