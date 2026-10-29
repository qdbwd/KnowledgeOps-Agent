import { api, ApiError, messageOf, send } from '../../../shared/api/api';
import type { DraftRecord, DraftStore } from './store';

export type SubmissionStore = Pick<DraftStore, 'read' | 'bind' | 'updatePending' | 'complete' | 'removeConversation'>;
type Transport = {
  get<T>(path: string): Promise<T>;
  post<T>(path: string, body: unknown, method?: string): Promise<T>;
};
const transport: Transport = { get: api, post: send };
type CreationReceipt = { state: 'accepted' | 'not_accepted' | 'deleted'; conversation_id?: string };
type Receipt = { state: 'accepted' | 'not_accepted'; run_id?: string };
type Settings = { permission_mode: string; reasoning_effort: string; revision: number };

/** 刷新/重新打开时只读服务器事实；结果未知绝不能以重新 POST 作为查询方式。 */
export async function reconcileSubmission(store: SubmissionStore, scope: string, http = transport): Promise<DraftRecord> {
  let row = await store.read(scope);
  const pending = row.pending;
  if (!pending || row.deleted) return row;
  if (pending.state === 'unknown') row = await store.updatePending(scope, pending.id, { uncertain: true });
  try {
    if (!row.conversationId) {
      const receipt = await http.get<CreationReceipt>(`/api/conversations/creation-receipt?request_key=${encodeURIComponent(pending.creation.request_key)}`);
      if (receipt.state === 'not_accepted')
        return store.updatePending(scope, pending.id, { stage: 'creation', state: 'not_accepted', error: '' });
      if (!['accepted', 'deleted'].includes(receipt.state)) throw new Error('暂时无法确认创建结果，请重新核对。');
      if (!receipt.conversation_id) throw new Error('创建回执不完整，请稍后重新核对。');
      row = await store.bind(scope, pending.id, receipt.conversation_id);
      if (receipt.state === 'deleted') {
        await store.removeConversation(receipt.conversation_id);
        return store.read(scope);
      }
    }
    if (pending.intent === 'send') {
      const receipt = await http.get<Receipt>(`/api/conversations/${row.conversationId}/submission-receipt?request_key=${encodeURIComponent(pending.body.request_key)}`);
      if (receipt.state === 'accepted') {
        if (!receipt.run_id) throw new Error('提交回执不完整，请稍后重新核对。');
        return store.complete(scope, pending.id, receipt.run_id);
      }
      if (receipt.state !== 'not_accepted') throw new Error('暂时无法确认是否受理，请重新核对。');
    }
    if (row.pending?.needsSettings) {
      const settings = await http.get<Settings>(`/api/conversations/${row.conversationId}/runtime-settings`);
      if (settings.permission_mode === pending.data.permission && settings.reasoning_effort === pending.data.reasoning) {
        row = await store.updatePending(scope, pending.id, { needsSettings: false, stage: 'message' });
      } else if (settings.revision !== 0) {
        return store.updatePending(scope, pending.id, { state: 'not_accepted', error: '会话执行设置已变化，请在该会话中核对后继续。' });
      }
    }
    if (pending.intent === 'materials' && !row.pending?.needsSettings) return store.complete(scope, pending.id);
    return store.updatePending(scope, pending.id, { state: 'not_accepted', error: '' });
  } catch (failure) {
    if (failure instanceof ApiError && failure.status === 404 && row.conversationId) {
      // 回执/设置的 404 表示会话不可读，不通过创建新身份绕过删除。
      await store.removeConversation(row.conversationId);
      return store.read(scope);
    }
    return store.updatePending(scope, pending.id, { state: 'unknown', uncertain: true,
      error: `暂时无法核对提交结果：${messageOf(failure)}` });
  }
}

/** 只由明确的发送/继续按钮调用。参数从已提交的本地事务读取，不从当前输入框重建。 */
export async function dispatchSubmission(store: SubmissionStore, scope: string, fresh = false, http = transport): Promise<DraftRecord> {
  let row = fresh ? await store.read(scope) : await reconcileSubmission(store, scope, http);
  let pending = row.pending;
  if (!pending || row.deleted || (!fresh && pending.state !== 'not_accepted')) return row;
  const id = pending.id;
  try {
    if (!row.conversationId) {
      row = await store.updatePending(scope, id, { stage: 'creation', state: 'unknown', error: '' });
      if (row.pending?.id !== id) return row;
      const created = await http.post<{ id: string }>('/api/conversations', pending.creation);
      if (!created.id) throw new Error('创建响应缺少会话身份。');
      row = await store.bind(scope, id, created.id);
    }
    pending = row.pending;
    if (!pending || pending.id !== id) return row;
    if (pending.needsSettings) {
      const path = `/api/conversations/${row.conversationId}/runtime-settings`;
      const current = await http.get<Settings>(path);
      if (current.permission_mode !== pending.data.permission || current.reasoning_effort !== pending.data.reasoning) {
        if (current.revision !== 0) throw new Error('会话执行设置已变化，请先核对。');
        row = await store.updatePending(scope, id, { stage: 'settings', state: 'unknown' });
        if (row.pending?.id !== id) return row;
        await http.post(path, { expected_revision: 0, permission_mode: pending.data.permission,
          reasoning_effort: pending.data.reasoning }, 'PATCH');
      }
      row = await store.updatePending(scope, id, { needsSettings: false, stage: 'message' });
    }
    if (row.pending?.id !== id) return row;
    if (pending.intent === 'materials') return store.complete(scope, id);
    row = await store.updatePending(scope, id, { stage: 'message', state: 'unknown', error: '' });
    if (row.pending?.id !== id) return row;
    const accepted = await http.post<{ id: string }>(`/api/conversations/${row.conversationId}/messages`, pending.body);
    if (!accepted.id) throw new Error('发送响应缺少任务身份。');
    return store.complete(scope, id, accepted.id);
  } catch (failure) {
    // 首次请求的明确参数/权限拒绝可回到编辑；曾有丢失响应时继续保护原身份。
    const rejected = !pending?.uncertain && failure instanceof ApiError
      && [400, 403, 404, 422].includes(failure.status);
    return store.updatePending(scope, id, { state: rejected ? 'rejected' : 'unknown', uncertain: !rejected,
      error: rejected ? messageOf(failure) : `提交结果尚未确认，请核对原请求：${messageOf(failure)}` });
  }
}
