import { api, ApiError, messageOf } from '../../../shared/api/api';
import { clearComposerDraft, readComposerDraft } from '../composerDraft';
import { dispatchSubmission, reconcileSubmission } from './submission';
import { DraftStore, emptyDraft, sameDraft } from './store';
import type { DraftData, DraftRecord, PendingSubmission } from './store';

type Change = { namespace: string; scope?: string; conversationId?: string; deleted?: boolean; writer?: string };
const eventName = 'knowledgeops:draft-change';
const channelName = 'knowledgeops-composer-v1';
export function broadcastDraftChange(change: Change) {
  window.dispatchEvent(new CustomEvent(eventName, { detail: change }));
  if (typeof BroadcastChannel !== 'undefined') {
    const channel = new BroadcastChannel(channelName);
    channel.postMessage(change);
    channel.close();
  }
  try { localStorage.setItem(eventName, JSON.stringify({ ...change, stamp: crypto.randomUUID() })); }
  catch { /* 通知只携带身份；正文及回执的可靠性由 IndexedDB 事务负责。 */ }
}

export async function removeConversationDraft(conversationId: string) {
  clearComposerDraft(conversationId);
  const value = await api<{ namespace: string; contract_version: number }>('/api/client-state');
  if (value.contract_version !== 1) throw new Error('浏览器草稿协议已变化，请刷新页面。');
  try { await new DraftStore(value.namespace).removeConversation(conversationId); }
  finally { broadcastDraftChange({ namespace: value.namespace, conversationId, deleted: true }); }
}

export type ComposerState = {
  data: DraftData; record: DraftRecord | null; ready: boolean; saving: number; busy: boolean;
  persisted: boolean; error: string; storageError: string;
  completed: { id: string; conversationId: string; intent: PendingSubmission['intent'] } | null;
};

/** 每个页面独立编辑缓冲，持久层负责跨标签 CAS。网络期间仍可立即保存后续输入。 */
export class ComposerSession {
  private state: ComposerState = { data: emptyDraft(), record: null, ready: false, saving: 0,
    busy: false, persisted: false, error: '', storageError: '', completed: null };
  private listeners = new Set<() => void>();
  private queue: Promise<unknown> = Promise.resolve();
  private store: DraftStore | null = null;
  private writer = crypto.randomUUID();
  private generation = 0;
  private editEpoch = 0;
  // 编辑缓冲所基于的正文版本。后台读到新记录不代表用户已看过并基于它编辑。
  private editingRevision = 0;
  private detach = () => {};
  constructor(private conversationId?: string, private newId?: string) {
    if (newId === 'new') this.newId = crypto.randomUUID();
  }
  snapshot = () => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };
  private publish(patch: Partial<ComposerState>) {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((listener) => listener());
  }
  private serial<T>(action: () => Promise<T>): Promise<T> {
    const work = this.queue.then(action);
    this.queue = work.catch(() => {});
    return work;
  }
  private apply(record: DraftRecord, preserveEditor = false) {
    const own = record.conflicts.find((item) => item.writer === this.writer);
    if (!preserveEditor) this.editingRevision = record.revision;
    this.publish({ record,
      data: record.deleted ? emptyDraft() : preserveEditor ? this.state.data : own?.data ?? record.data,
      persisted: !preserveEditor || this.state.persisted });
  }
  private notify() {
    if (this.store && this.state.record) broadcastDraftChange({ namespace: this.store.namespace,
      scope: this.state.record.scope, conversationId: this.state.record.conversationId, writer: this.writer });
  }
  private storageFailure(error: unknown) {
    this.publish({ persisted: false, storageError: `草稿尚未保存，当前输入仍在本页。发送前需恢复保存：${messageOf(error)}` });
  }

  start = async () => {
    const generation = ++this.generation;
    this.detach();
    try {
      const client = await api<{ namespace: string; contract_version: number }>('/api/client-state');
      if (client.contract_version !== 1) throw new Error('浏览器草稿协议不兼容，请刷新应用。');
      const store = new DraftStore(client.namespace);
      const record = await store.open({ conversationId: this.conversationId, newId: this.newId,
        legacy: readComposerDraft(this.conversationId) });
      if (generation !== this.generation) return;
      this.store = store;
      if (this.conversationId) clearComposerDraft(this.conversationId);
      const preserve = this.editEpoch > 0 && !this.state.persisted;
      this.apply(record, preserve);
      this.publish({ ready: true, storageError: '', persisted: !preserve });
      if (preserve) {
        const saved = await store.save(record.scope, sameDraft(record.data, emptyDraft()) ? record.revision : -1, this.state.data, this.writer);
        this.editingRevision = saved.revision;
        this.apply(saved, true);
        this.publish({ persisted: true, storageError: '' });
      }
      const onChange = (change: Change) => {
        if (change.namespace !== store.namespace || change.writer === this.writer) return;
        if (change.scope !== this.state.record?.scope && (!change.conversationId || change.conversationId !== this.state.record?.conversationId)) return;
        if (change.deleted) {
          this.publish({ data: emptyDraft(), record: this.state.record ? { ...this.state.record,
            deleted: true, pending: null, conflicts: [], data: emptyDraft() } : null });
          if (change.conversationId) void store.removeConversation(change.conversationId)
            .then(() => this.refresh()).catch((failure) => this.storageFailure(failure));
          return;
        }
        void this.refresh();
      };
      const channel = typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel(channelName);
      if (channel) channel.onmessage = (event: MessageEvent<Change>) => onChange(event.data);
      const local = (event: Event) => onChange((event as CustomEvent<Change>).detail);
      const storage = (event: StorageEvent) => {
        if (event.key !== eventName || !event.newValue) return;
        try { onChange(JSON.parse(event.newValue) as Change); } catch { /* 无效通知不读取或提交正文。 */ }
      };
      const focus = () => { void this.refresh(true); };
      const unload = (event: BeforeUnloadEvent) => {
        if (this.state.saving || (!this.state.persisted && this.state.data.content)) {
          event.preventDefault(); event.returnValue = '';
        }
      };
      window.addEventListener(eventName, local);
      window.addEventListener('storage', storage);
      window.addEventListener('focus', focus);
      window.addEventListener('online', focus);
      window.addEventListener('beforeunload', unload);
      this.detach = () => {
        channel?.close();
        window.removeEventListener(eventName, local); window.removeEventListener('storage', storage);
        window.removeEventListener('focus', focus); window.removeEventListener('online', focus);
        window.removeEventListener('beforeunload', unload);
      };
      if (record.pending) await this.check();
      else if (record.conversationId) await this.refresh(true);
    } catch (failure) {
      if (generation === this.generation) { this.publish({ ready: true }); this.storageFailure(failure); }
    }
  };
  stop = () => { this.generation += 1; this.detach(); };

  edit = (patch: Partial<DraftData>) => {
    const data = { ...this.state.data, ...patch };
    const epoch = ++this.editEpoch;
    this.publish({ data, saving: this.state.saving + 1, persisted: false });
    void this.serial(async () => {
      if (!this.store || !this.state.record) throw new Error('浏览器存储尚未就绪。');
      const record = await this.store.save(this.state.record.scope, this.editingRevision, data, this.writer);
      this.editingRevision = record.revision;
      this.apply(record, true);
      if (this.editEpoch === epoch) this.publish({ persisted: true, storageError: '' });
      this.notify();
    }).catch((failure) => this.storageFailure(failure))
      .finally(() => this.publish({ saving: Math.max(0, this.state.saving - 1) }));
  };

  refresh = async (checkServer = false) => {
    try {
      await this.serial(async () => {
        if (!this.store || !this.state.record) return;
        const record = await this.store.read(this.state.record.scope);
        this.apply(record, !this.state.persisted);
      });
      if (checkServer && this.store && this.state.record?.conversationId && !this.state.record.deleted) {
        const client = await api<{ namespace: string }>('/api/client-state');
        if (client.namespace !== this.store.namespace) {
          this.store = null;
          this.storageFailure(new Error('应用数据身份已变化，请重新打开页面。'));
          return;
        }
        try { await api(`/api/conversations/${this.state.record.conversationId}/runtime-settings`); }
        catch (failure) {
          if (!(failure instanceof ApiError) || failure.status !== 404) throw failure;
          await this.store.removeConversation(this.state.record.conversationId);
          this.apply(await this.store.read(this.state.record.scope));
          this.notify();
        }
      }
      if (checkServer && this.state.record?.pending) await this.check();
    } catch (failure) { this.publish({ error: messageOf(failure) }); }
  };

  private async finish(record: DraftRecord, before: DraftRecord, epoch: number) {
    await this.serial(async () => {
      if (!this.store) return;
      const latest = await this.store.read(record.scope);
      this.apply(latest, this.editEpoch !== epoch && !sameDraft(this.state.data, latest.data));
      if (!latest.pending && latest.lastAccepted && before.pending && latest.lastAccepted.id === before.pending.id && latest.conversationId) {
        this.publish({ completed: { id: latest.lastAccepted.id, conversationId: latest.conversationId,
          intent: latest.lastAccepted.intent }, error: '' });
      }
      this.notify();
    });
  }

  submit = async (intent: PendingSubmission['intent'] = 'send') => {
    if (this.state.busy) return;
    this.publish({ busy: true, error: '' });
    const epoch = this.editEpoch;
    try {
      const prepared = await this.serial(async () => {
        if (!this.store || !this.state.record || !this.state.persisted) throw new Error('请先保存草稿，再提交。');
        const value = await this.store.prepare(this.state.record.scope, this.state.record.revision, intent);
        this.apply(value, true);
        this.notify();
        return value;
      });
      // 身份和参数已提交 IndexedDB；接下来即使页面卸载，仍可以只读核对。
      const result = await dispatchSubmission(this.store!, prepared.scope, true);
      await this.finish(result, prepared, epoch);
    } catch (failure) { this.publish({ error: messageOf(failure) }); }
    finally { this.publish({ busy: false }); }
  };

  private recover = async (retry: boolean) => {
    if (this.state.busy || !this.store || !this.state.record?.pending) return;
    this.publish({ busy: true, error: '' });
    const epoch = this.editEpoch;
    const before = this.state.record;
    try {
      const result = retry ? await dispatchSubmission(this.store, before.scope)
        : await reconcileSubmission(this.store, before.scope);
      await this.finish(result, before, epoch);
    } catch (failure) { this.publish({ error: messageOf(failure) }); }
    finally { this.publish({ busy: false }); }
  };
  check = () => this.recover(false);
  retry = () => this.recover(true);

  flush = async () => {
    await this.queue;
    if (!this.store || !this.state.record || !this.state.persisted) throw new Error('草稿尚未保存，请先重试保存。');
    if (this.state.record.deleted) throw new Error('会话已删除。');
  };

  resolve = async (writer: string | null, expectedChange: number) => {
    try {
      await this.serial(async () => {
        if (!this.store || !this.state.record) return;
        const row = await this.store.resolve(this.state.record.scope, expectedChange, writer);
        this.apply(row); this.publish({ error: '', persisted: true }); this.notify();
      });
    } catch (failure) { this.publish({ error: messageOf(failure) }); await this.refresh(); }
  };
  discard = async (expectedChange: number) => {
    try {
      await this.serial(async () => {
        if (!this.store || !this.state.record) return;
        const row = await this.store.abandon(this.state.record.scope, expectedChange);
        this.apply(row); this.publish({ error: '', persisted: true }); this.notify();
      });
    } catch (failure) { this.publish({ error: messageOf(failure) }); }
  };
  editRejected = async () => {
    try {
      if (!this.store || !this.state.record?.pending) return;
      this.apply(await this.store.releaseRejected(this.state.record.scope, this.state.record.pending.id), true);
      this.notify();
    } catch (failure) { this.publish({ error: messageOf(failure) }); }
  };
}
