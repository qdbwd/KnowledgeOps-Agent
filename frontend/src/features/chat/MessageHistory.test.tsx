import '@testing-library/jest-dom/vitest';
import { StrictMode, useRef } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useMessageHistory } from './useMessageHistory';
import type { MessagePage } from './useMessageHistory';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function page(ids: string[], options: Partial<MessagePage> = {}): MessagePage {
  return { items: ids.map((id) => ({ id, run_id: id, role: 'user', content: id, created_at: '2026-01-01T00:00:00Z' })),
    revision: 1, total: 3, start: 0, older_cursor: null, newer_cursor: null, ...options };
}

function Harness({ conversation = 'first', refresh = 0, anchor = null }: { conversation?: string; refresh?: number; anchor?: string | null }) {
  const scroll = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const history = useMessageHistory(conversation, refresh, anchor, scroll, following);
  return <div ref={scroll}>
    {history.data?.map((message) => <article key={message.id} data-message-id={message.id}>{message.content}</article>)}
    <p role="status">{history.notice}</p><p role="alert">{history.error}</p>
    <button disabled={history.loading} onClick={() => void history.loadOlder()}>older</button>
    <button disabled={history.loading} onClick={() => void history.loadNewer()}>newer</button>
    <button onClick={() => void history.latest()}>latest</button>
  </div>;
}

it('prepends and appends chronological pages without losing the other boundary', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.includes('cursor=older')) return Response.json(page(['one'], { newer_cursor: 'middle' }));
    if (url.includes('cursor=newer')) return Response.json(page(['three'], { older_cursor: 'middle' }));
    return Response.json(page(['two'], { older_cursor: 'older', newer_cursor: 'newer' }));
  }));
  render(<Harness />);
  await screen.findByText('two');
  fireEvent.click(screen.getByText('older'));
  await screen.findByText('one');
  fireEvent.click(screen.getByText('newer'));
  await screen.findByText('three');
  expect(screen.getAllByRole('article').map((node) => node.textContent)).toEqual(['one', 'two', 'three']);
});

it('ignores a late response belonging to another conversation', async () => {
  let finishFirst!: (value: Response) => void;
  vi.stubGlobal('fetch', vi.fn((url: string) => url.includes('/first/')
    ? new Promise<Response>((resolve) => { finishFirst = resolve; })
    : Promise.resolve(Response.json(page(['new conversation'])))));
  const rendered = render(<Harness />);
  rendered.rerender(<Harness conversation="second" />);
  await screen.findByText('new conversation');
  await act(async () => finishFirst(Response.json(page(['old conversation']))));
  expect(screen.queryByText('old conversation')).not.toBeInTheDocument();
});

it('relocates a stale page around the currently read message rather than merging different revisions', async () => {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    return { top: 0, bottom: 20, left: 0, right: 10, width: 10, height: 20, x: 0, y: 0, toJSON() {} };
  });
  const fetcher = vi.fn(async (url: string) => {
    if (url.includes('cursor=')) return Response.json({ code: 'cursor_stale', detail: '已变化' }, { status: 409 });
    if (url.includes('anchor=reading')) return Response.json(page(['moved', 'reading'], { revision: 2 }));
    return Response.json(page(['reading', 'old-order'], { older_cursor: 'stale' }));
  });
  vi.stubGlobal('fetch', fetcher);
  render(<Harness />);
  await screen.findByText('reading');
  fireEvent.click(screen.getByText('older'));
  await screen.findByText('moved');
  expect(screen.queryByText('old-order')).not.toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('保留当前阅读位置');
  expect(fetcher.mock.calls.some(([url]) => url.endsWith('?anchor=reading'))).toBe(true);
});

it('uses a new search anchor even when the same conversation is already open', async () => {
  const fetcher = vi.fn(async (url: string) => Response.json(page([url.includes('anchor=hit') ? 'hit' : 'original'])));
  vi.stubGlobal('fetch', fetcher);
  const rendered = render(<Harness />);
  await screen.findByText('original');
  rendered.rerender(<Harness anchor="hit" />);
  await screen.findByText('hit');
  expect(screen.queryByText('original')).not.toBeInTheDocument();
  fireEvent.click(screen.getByText('latest'));
  await screen.findByText('original');
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3));
});

it('keeps the initial search anchor across effect replay and does not revisit it after returning to latest', async () => {
  const fetcher = vi.fn(async (url: string) => Response.json(page([url.includes('anchor=hit') ? 'hit' : 'latest message'])));
  vi.stubGlobal('fetch', fetcher);
  const rendered = render(<StrictMode><Harness anchor="hit" /></StrictMode>);
  await screen.findByText('hit');
  fireEvent.click(screen.getByText('latest'));
  await screen.findByText('latest message');
  rendered.rerender(<StrictMode><Harness anchor="hit" refresh={1} /></StrictMode>);
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(4));
  expect(screen.queryByText('hit')).not.toBeInTheDocument();
  expect(screen.getByText('latest message')).toBeInTheDocument();
});
