import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { ConversationsPage } from './ConversationsPage';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const row = (id: string) => ({ id, title: `会话${id}`, excerpt: `正文${id}`, activity_at: null, match_message_id: 'hit-' + id });

it('searches visible text and links the excerpt to its exact message', async () => {
  const fetcher = vi.fn(async (url: string) => Response.json({ items: url.includes('q=%E7%9B%AE%E6%A0%87') ? [row('match')] : [row('initial')], next_cursor: null }));
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><ConversationsPage /></MemoryRouter>);
  await screen.findByText('会话initial');
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: '目标' } });
  fireEvent.submit(screen.getByRole('searchbox').closest('form')!);
  await screen.findByText('会话match');
  expect(screen.getByRole('link', { name: '正文match' })).toHaveAttribute('href', '/chat/match?message=hit-match');
  expect(screen.queryByText('会话initial')).not.toBeInTheDocument();
});

it('restarts a changed directory instead of appending stale results', async () => {
  let reads = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.includes('cursor=')) return Response.json({ code: 'cursor_stale', detail: '活动顺序已变化' }, { status: 409 });
    reads += 1;
    return Response.json({ items: [row(reads === 1 ? 'old' : 'updated')], next_cursor: reads === 1 ? 'cursor' : null });
  }));
  render(<MemoryRouter><ConversationsPage /></MemoryRouter>);
  await screen.findByText('会话old');
  fireEvent.click(screen.getByText('加载更多会话'));
  await screen.findByText('会话updated');
  await waitFor(() => expect(screen.queryByText('会话old')).not.toBeInTheDocument());
  expect(screen.getByRole('status')).toHaveTextContent('重新加载');
});
