/** 进入材料管理页时暂存当前标签页的未发送输入，不生成聊天记录或运行。 */
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

export function saveComposerDraft(id: string, draft: ComposerDraft) {
  sessionStorage.setItem(key(id), JSON.stringify(draft));
}

export function clearComposerDraft(id: string) {
  try {
    sessionStorage.removeItem(key(id));
  } catch {
    // 消息已由服务确认，浏览器存储失效不能把成功提交误报为失败。
  }
}
