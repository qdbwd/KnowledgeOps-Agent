// 即使底层忽略 AbortSignal，旧地址的慢响应也不能覆盖新地址的数据。
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { usePagedResource, useResource } from './api';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('discards an old resource response that resolves after navigation', async () => {
  let resolveOld!: (response: Response) => void;
  vi.stubGlobal('fetch', (url: string) =>
    url === '/old'
      ? new Promise<Response>((resolve) => {
          resolveOld = resolve;
        })
      : Promise.resolve(Response.json({ title: '新资料' })),
  );
  const { result, rerender } = renderHook(({ path }) => useResource<{ title: string }>(path), {
    initialProps: { path: '/old' },
  });
  rerender({ path: '/new' });
  await waitFor(() => expect(result.current.data?.title).toBe('新资料'));
  await act(async () => {
    resolveOld(Response.json({ title: '过期资料' }));
  });
  expect(result.current.data?.title).toBe('新资料');
});

it('retains run card identity during same-address refresh but clears it on navigation', async () => {
  let finish!: (response: Response) => void;
  let count = 0;
  vi.stubGlobal('fetch', () => ++count === 1
    ? Promise.resolve(Response.json({ items: [{ id: 'first' }], next_cursor: null }))
    : new Promise<Response>((resolve) => { finish = resolve; }));
  const { result, rerender } = renderHook(({ path, revision }) => usePagedResource(path, revision, true), {
    initialProps: { path: '/first', revision: 0 },
  });
  await waitFor(() => expect(result.current.data).toEqual([{ id: 'first' }]));
  rerender({ path: '/first', revision: 1 });
  expect(result.current.data).toEqual([{ id: 'first' }]);
  const old = finish;
  rerender({ path: '/other', revision: 1 });
  expect(result.current.data).toEqual([]);
  await act(async () => { old(Response.json({ items: [{ id: 'late' }], next_cursor: null })); });
  expect(result.current.data).toEqual([]);
  await act(async () => { finish(Response.json({ items: [{ id: 'other' }], next_cursor: null })); });
  expect(result.current.data).toEqual([{ id: 'other' }]);
});
