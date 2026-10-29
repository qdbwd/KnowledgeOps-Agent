/** 仅用于从旧版本 sessionStorage 迁入；新草稿统一由 drafts/store 的 IndexedDB 事务保存。 */
export type ComposerDraft = {
  content: string;
  selected: string[];
  mode: 'ask' | 'preview' | 'direct';
  knowledgeOnly: boolean;
  delegation: boolean;
  auxiliary: boolean;
};

const key = (id: string) => `knowledgeops:composer:${id}`;

export function readComposerDraft(id?: string): Partial<ComposerDraft> {
  try {
    return id ? (JSON.parse(sessionStorage.getItem(key(id)) ?? '{}') as Partial<ComposerDraft>) : {};
  } catch {
    return {};
  }
}

export function clearComposerDraft(id: string) {
  try {
    sessionStorage.removeItem(key(id));
  } catch {
    // 只在新存储已接管或会话已删除后调用；旧标签存储不可用不回滚已完成的事务。
  }
}
