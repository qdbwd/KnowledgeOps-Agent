/** 浏览器草稿的事务边界。正文版本与请求回执分别维护，任何网络请求都在事务之外。 */
export type DraftData = {
  content: string;
  selected: string[];
  mode: 'ask' | 'preview' | 'direct';
  knowledgeOnly: boolean;
  delegation: boolean;
  auxiliary: boolean;
  reasoning: string;
  permission: string;
};

export const emptyDraft = (): DraftData => ({ content: '', selected: [], mode: 'ask', knowledgeOnly: false,
  delegation: true, auxiliary: false, reasoning: 'default', permission: 'ask' });

export type MessageBody = {
  content: string; source_ids: string[]; publication_mode: DraftData['mode']; knowledge_only: boolean;
  delegation: boolean; auxiliary: boolean; reasoning_effort: string; request_key: string;
};
export type PendingSubmission = {
  id: string;
  intent: 'send' | 'materials';
  revision: number;
  data: DraftData;
  creation: { title: string; request_key: string };
  body: MessageBody;
  needsSettings: boolean;
  stage: 'creation' | 'settings' | 'message';
  state: 'unknown' | 'not_accepted' | 'rejected';
  uncertain: boolean;
  error: string;
};
export type DraftRecord = {
  key: string; namespace: string; kind: 'draft'; scope: string; conversationId?: string;
  revision: number; change: number; data: DraftData;
  conflicts: { writer: string; data: DraftData; updatedAt: number }[];
  pending: PendingSubmission | null;
  lastAccepted?: { id: string; runId?: string; intent: PendingSubmission['intent'] };
  deleted: boolean; updatedAt: number;
};
type Pointer = { key: string; namespace: string; kind: 'pointer'; scope: string };
type Row = DraftRecord | Pointer;
export type DraftAddress = { conversationId?: string; newId?: string; legacy?: Partial<DraftData> };
export const sameDraft = (a: DraftData, b: DraftData) => JSON.stringify(a) === JSON.stringify(b);

/** 旧 sessionStorage 是迁入素材，不可信字段不能变成执行选项。 */
export function normalizeDraft(raw: unknown): DraftData {
  const value = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {};
  const result = emptyDraft();
  if (typeof value.content === 'string') result.content = value.content;
  if (Array.isArray(value.selected)) result.selected = value.selected.filter((id): id is string => typeof id === 'string');
  if (['ask', 'preview', 'direct'].includes(String(value.mode))) result.mode = value.mode as DraftData['mode'];
  for (const field of ['knowledgeOnly', 'delegation', 'auxiliary'] as const)
    if (typeof value[field] === 'boolean') result[field] = value[field];
  if (typeof value.reasoning === 'string' && value.reasoning.length <= 40) result.reasoning = value.reasoning;
  if (['ask', 'auto_review'].includes(String(value.permission))) result.permission = value.permission as string;
  return result;
}

function request<T>(value: IDBRequest<T>) {
  return new Promise<T>((resolve, reject) => { value.onsuccess = () => resolve(value.result); value.onerror = () => reject(value.error); });
}
let database: Promise<IDBDatabase> | undefined;
function connect() {
  if (!database) database = new Promise<IDBDatabase>((resolve, reject) => {
    const opening = indexedDB.open('knowledgeops-composer-v1', 1);
    opening.onupgradeneeded = () => {
      const rows = opening.result.createObjectStore('rows', { keyPath: 'key' });
      rows.createIndex('namespace', 'namespace');
    };
    opening.onerror = () => reject(opening.error);
    opening.onblocked = () => reject(new Error('浏览器草稿存储升级被其他页面阻塞，请关闭旧页面后重试。'));
    opening.onsuccess = () => {
      const db = opening.result;
      db.onversionchange = () => { db.close(); database = undefined; };
      resolve(db);
    };
  }).catch((error) => { database = undefined; throw error; });
  return database;
}

async function transaction<T>(mode: IDBTransactionMode, action: (rows: IDBObjectStore) => Promise<T>): Promise<T> {
  const db = await connect();
  const tx = db.transaction('rows', mode);
  const done = new Promise<void>((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onabort = () => reject(tx.error ?? new Error('草稿保存事务已中止。'));
    tx.onerror = () => reject(tx.error);
  });
  // 提前接管拒绝，避免 action 的请求失败与事务失败触发两次未处理异常。
  void done.catch(() => {});
  try {
    const result = await action(tx.objectStore('rows'));
    await done;
    return result;
  } catch (error) {
    try { tx.abort(); } catch { /* 事务可能已因配额或请求失败中止。 */ }
    await done.catch(() => {});
    throw error;
  }
}

export class DraftStore {
  constructor(public readonly namespace: string) {
    if (!namespace) throw new Error('未取得当前应用的数据身份。');
  }
  private key(scope: string) { return JSON.stringify([this.namespace, 'draft', scope]); }
  private pointer(name: string) { return JSON.stringify([this.namespace, 'pointer', name]); }
  private async load(rows: IDBObjectStore, scope: string): Promise<DraftRecord> {
    const row: Row | undefined = await request(rows.get(this.key(scope)));
    if (!row || row.kind !== 'draft') throw new Error('草稿已不存在，请重新打开页面。');
    return row;
  }
  private writable(row: DraftRecord) {
    if (row.deleted) throw new Error('会话已删除，不能恢复旧草稿或提交。');
  }
  private put(rows: IDBObjectStore, row: DraftRecord) {
    row.change += 1;
    row.updatedAt = Date.now();
    rows.put(row);
    return row;
  }

  async open(address: DraftAddress): Promise<DraftRecord> {
    return transaction('readwrite', async (rows) => {
      const name = address.conversationId ? `conversation:${address.conversationId}` : 'current-new';
      const pointer: Row | undefined = await request(rows.get(this.pointer(name)));
      let scope = address.conversationId ? `conversation:${address.conversationId}`
        : address.newId ? `new:${address.newId}` : pointer?.scope;
      if (address.conversationId && pointer) scope = pointer.scope;
      if (!scope) scope = `new:${crypto.randomUUID()}`;
      let existing: Row | undefined = await request(rows.get(this.key(scope)));
      // 普通首页恢复未绑定的新草稿；明确打开旧 URL 则仍找到其创建回执。
      if (!address.conversationId && !address.newId && existing?.kind === 'draft' && (existing.conversationId || existing.deleted)) {
        scope = `new:${crypto.randomUUID()}`;
        existing = undefined;
      }
      if (existing?.kind === 'draft') return existing;
      const row: DraftRecord = { key: this.key(scope), namespace: this.namespace, kind: 'draft', scope,
        conversationId: address.conversationId, revision: 0, change: 0, data: normalizeDraft(address.legacy),
        conflicts: [], pending: null, deleted: false, updatedAt: Date.now() };
      rows.put({ key: this.pointer(name), namespace: this.namespace, kind: 'pointer', scope } satisfies Pointer);
      rows.put(row);
      return row;
    });
  }

  read(scope: string) { return transaction('readonly', (rows) => this.load(rows, scope)); }

  save(scope: string, expected: number, data: DraftData, writer: string) {
    const snapshot = structuredClone(data);
    return transaction('readwrite', async (rows) => {
      const row = await this.load(rows, scope);
      this.writable(row);
      const own = row.conflicts.find((item) => item.writer === writer);
      if (own || (row.revision !== expected && !sameDraft(row.data, snapshot))) {
        // 冲突状态只能由明确选择解除；随后输入继续更新本标签副本。
        row.conflicts = row.conflicts.filter((item) => item.writer !== writer);
        row.conflicts.push({ writer, data: snapshot, updatedAt: Date.now() });
      } else if (!sameDraft(row.data, snapshot)) {
        row.data = snapshot;
        row.revision += 1;
      } else return row;
      return this.put(rows, row);
    });
  }

  resolve(scope: string, expectedChange: number, writer: string | null) {
    return transaction('readwrite', async (rows) => {
      const row = await this.load(rows, scope);
      this.writable(row);
      if (row.change !== expectedChange) throw new Error('草稿又有变化，请查看最新版本后选择。');
      const choice = writer ? row.conflicts.find((item) => item.writer === writer)?.data : row.data;
      if (!choice) throw new Error('所选草稿版本已变化。');
      row.data = choice;
      row.conflicts = [];
      row.revision += 1;
      return this.put(rows, row);
    });
  }

  prepare(scope: string, expected: number, intent: PendingSubmission['intent']) {
    return transaction('readwrite', async (rows) => {
      const row = await this.load(rows, scope);
      this.writable(row);
      if (row.pending) throw new Error('请先核对上一次提交，不能另建请求。');
      if (row.revision !== expected || row.conflicts.length) throw new Error('请先处理草稿的不同版本。');
      const data = structuredClone(row.data);
      if (intent === 'send' && !data.content.trim()) throw new Error('请先填写消息。');
      row.pending = { id: crypto.randomUUID(), intent, revision: row.revision, data,
        creation: { title: intent === 'send' ? data.content.trim().slice(0, 30) : '新对话', request_key: crypto.randomUUID() },
        body: { content: data.content, source_ids: data.selected, publication_mode: data.mode,
          knowledge_only: data.knowledgeOnly, delegation: data.delegation, auxiliary: data.auxiliary,
          reasoning_effort: data.reasoning, request_key: crypto.randomUUID() },
        needsSettings: !row.conversationId, stage: row.conversationId ? 'message' : 'creation',
        state: 'unknown', uncertain: false, error: '' };
      return this.put(rows, row);
    });
  }

  bind(scope: string, pendingId: string, conversationId: string) {
    return transaction('readwrite', async (rows) => {
      const row = await this.load(rows, scope);
      this.writable(row);
      if (row.pending?.id !== pendingId) return row;
      if (row.conversationId && row.conversationId !== conversationId) throw new Error('创建回执的会话身份不一致。');
      const alias: Row | undefined = await request(rows.get(this.pointer(`conversation:${conversationId}`)));
      if (alias && alias.scope !== scope) throw new Error('该会话已有另一份草稿，需先核对。');
      row.conversationId = conversationId;
      row.pending.stage = row.pending.needsSettings ? 'settings' : 'message';
      rows.put({ key: this.pointer(`conversation:${conversationId}`), namespace: this.namespace, kind: 'pointer', scope } satisfies Pointer);
      return this.put(rows, row);
    });
  }

  updatePending(scope: string, id: string, patch: Partial<Pick<PendingSubmission, 'stage' | 'state' | 'error' | 'needsSettings' | 'uncertain'>>) {
    return transaction('readwrite', async (rows) => {
      const row = await this.load(rows, scope);
      this.writable(row);
      if (row.pending?.id !== id) return row;
      Object.assign(row.pending, patch);
      return this.put(rows, row);
    });
  }

  complete(scope: string, id: string, runId?: string) {
    return transaction('readwrite', async (rows) => {
      const row = await this.load(rows, scope);
      if (row.deleted || row.pending?.id !== id) return row;
      const sent = row.pending;
      // 只退休实际发送的正文版本；之后输入以及其他标签的冲突副本不受影响。
      if (sent.intent === 'send' && row.revision === sent.revision && sameDraft(row.data, sent.data)) {
        row.data = { ...row.data, content: '', selected: [] };
        row.revision += 1;
      }
      row.lastAccepted = { id, runId, intent: sent.intent };
      row.pending = null;
      return this.put(rows, row);
    });
  }

  releaseRejected(scope: string, id: string) {
    return transaction('readwrite', async (rows) => {
      const row = await this.load(rows, scope);
      this.writable(row);
      if (row.pending?.id !== id || row.pending.state !== 'rejected' || row.pending.uncertain)
        throw new Error('尚未明确拒绝，需继续核对原请求。');
      row.pending = null;
      return this.put(rows, row);
    });
  }

  abandon(scope: string, expectedChange: number) {
    return transaction('readwrite', async (rows) => {
      const row = await this.load(rows, scope);
      this.writable(row);
      if (row.change !== expectedChange) throw new Error('草稿已变化，请重新核对。');
      if (row.pending && row.pending.state !== 'rejected') throw new Error('提交结果尚未确定，不能丢弃请求身份。');
      row.pending = null;
      row.data = emptyDraft();
      row.conflicts = [];
      row.revision += 1;
      return this.put(rows, row);
    });
  }

  removeConversation(conversationId: string) {
    return transaction('readwrite', async (rows) => {
      const all: Row[] = await request(rows.index('namespace').getAll(this.namespace));
      const targets = all.filter((row): row is DraftRecord => row.kind === 'draft' && row.conversationId === conversationId);
      if (!targets.length) {
        const scope = `conversation:${conversationId}`;
        targets.push({ key: this.key(scope), namespace: this.namespace, kind: 'draft', scope, conversationId,
          revision: 0, change: 0, data: emptyDraft(), conflicts: [], pending: null, deleted: true, updatedAt: Date.now() });
      }
      for (const row of targets) {
        row.deleted = true; row.data = emptyDraft(); row.conflicts = []; row.pending = null;
        delete row.lastAccepted;
        row.revision += 1;
        this.put(rows, row);
      }
      // 保留最小墓碑，旧标签页迟到的保存不能复活已删除的正文/提交。
    });
  }
}
