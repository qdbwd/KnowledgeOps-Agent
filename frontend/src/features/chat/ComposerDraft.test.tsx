import '@testing-library/jest-dom/vitest';
import 'fake-indexeddb/auto';
import { StrictMode, useEffect } from 'react';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useComposerDraft } from './useComposerDraft';
import type { ComposerSession } from './drafts/session';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function Editor({ id, changed }: { id?: string; changed: (session: ComposerSession) => void }) {
  const { session, state } = useComposerDraft(undefined, id);
  useEffect(() => changed(session), [session, changed]);
  return <><p>{state.ready ? 'ready' : 'loading'}</p><textarea aria-label="draft" value={state.data.content}
    onChange={(event) => session.edit({ content: event.target.value })} /></>;
}

it('keeps the same editor when its allocated draft identity is written to the URL', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ namespace: crypto.randomUUID(), contract_version: 1 })));
  let editor!: ComposerSession;
  const changed = (session: ComposerSession) => { editor = session; };
  const result = render(<StrictMode><Editor changed={changed} /></StrictMode>);
  await screen.findByText('ready');
  const original = editor;
  const id = original.snapshot().record!.scope.slice(4);
  // 模拟身份归一化与第一笔输入同时发生，旧实现会换掉 session。
  act(() => {
    original.edit({ content: '地址变化期间的输入' });
    result.rerender(<StrictMode><Editor id={id} changed={changed} /></StrictMode>);
  });
  expect(editor).toBe(original);
  await waitFor(() => expect(editor.snapshot().persisted).toBe(true));
  expect(screen.getByRole('textbox')).toHaveValue('地址变化期间的输入');
});
