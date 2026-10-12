// 即使底层忽略 AbortSignal，旧地址的慢响应也不能覆盖新地址的数据。
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useResource } from './api';

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
