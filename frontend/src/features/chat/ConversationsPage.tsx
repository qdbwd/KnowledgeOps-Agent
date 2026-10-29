import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Search } from 'lucide-react';
import { usePagedResource } from '../../shared/api/api';
import { useWorkspace } from '../../shared/state/workspace';
import { Button } from '../../shared/ui/Button';
import { Empty, ErrorBox, Loading, PageTitle } from '../../shared/ui/components';
import type { Conversation } from '../../shared/types/types';
import { ConversationDeletionHistory } from './ConversationDeletionHistory';

type ConversationResult = Conversation & { activity_at: string | null; excerpt: string; match_message_id: string | null };

export function ConversationsPage() {
  const { version, refresh } = useWorkspace();
  const [input, setInput] = useState('');
  const [query, setQuery] = useState('');
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(input.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [input]);
  const rows = usePagedResource<ConversationResult>(`/api/conversations/page?q=${encodeURIComponent(query)}`, version);
  return (
    <div>
      <PageTitle eyebrow="CONVERSATIONS" title="全部会话">
        <Link className="button button-secondary" to="/">开始新对话</Link>
      </PageTitle>
      <ConversationDeletionHistory />
      <form className="mb-6 flex items-center gap-3 rounded-xl border border-line bg-white px-4 py-2" onSubmit={(event) => { event.preventDefault(); setQuery(input.trim()); }}>
        <Search size={18} className="shrink-0 text-muted" />
        <input aria-label="搜索会话标题和正文" type="search" value={input} maxLength={200}
          placeholder="搜索标题、你的消息和 AI 回答" onChange={(event) => setInput(event.target.value)}
          className="min-w-0 flex-1 !border-0 !shadow-none" />
        <Button type="submit" className="button-quiet">搜索</Button>
      </form>
      <ErrorBox message={rows.error} />
      {rows.error && <Button className="button-secondary" onClick={refresh}>重新加载</Button>}
      {rows.notice && <p role="status" className="text-sm text-muted">{rows.notice}</p>}
      <div className="grid gap-3">
        {rows.data.map((row) => (
          <article key={row.id} className="min-w-0 rounded-xl border border-line bg-white p-5">
            <Link className="block break-words text-base font-semibold" to={`/chat/${row.id}`}>{row.title}</Link>
            {row.excerpt && <Link className="mt-2 block line-clamp-3 whitespace-pre-wrap break-words text-sm text-muted hover:text-ink"
              to={`/chat/${row.id}${row.match_message_id ? `?message=${encodeURIComponent(row.match_message_id)}` : ''}`}>
              {row.excerpt}
            </Link>}
            {row.activity_at && <time className="mt-3 block text-xs text-muted" dateTime={row.activity_at}>{new Date(row.activity_at).toLocaleString()}</time>}
          </article>
        ))}
      </div>
      {rows.loading && <Loading />}
      {!rows.loading && !rows.error && rows.data.length === 0 && <Empty title={query ? '没有找到匹配的会话' : '还没有会话'}>
        {query ? '可以换一个标题或正文中的词。' : '发送第一条消息后，会话会显示在这里。'}
      </Empty>}
      {rows.next && <Button className="button-secondary mt-5" disabled={rows.loading} onClick={() => void rows.loadMore()}>加载更多会话</Button>}
    </div>
  );
}
