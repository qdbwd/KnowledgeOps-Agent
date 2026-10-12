// 从历史证据进入来源页面时，必须继续查看被引用版本和片段，而非最新版本。
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import { EvidencePage, SourcePage } from './SourcesPage';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it.each(['evidence', 'source-version'])(
  'does not show stale %s after navigation to a missing identity',
  async (kind) => {
    const first = kind === 'evidence' ? '/evidence/old' : '/sources/source?version=old';
    const next = kind === 'evidence' ? '/evidence/missing' : '/sources/source?version=missing';
    vi.stubGlobal('fetch', async (url: string) => {
      if (url.includes('missing'))
        return Response.json({ detail: '目标记录不存在' }, { status: 404 });
      return Response.json({
        id: 'old',
        title: '只能属于旧地址的内容',
        text: '旧证据',
        kind: 'text',
        version: 1,
        version_id: 'old',
        segments: [],
      });
    });
    render(
      <MemoryRouter initialEntries={[first]}>
        <Link to={next}>打开另一记录</Link>
        <Routes>
          <Route path="/evidence/:evidenceId" element={<EvidencePage />} />
          <Route path="/sources/:sourceId" element={<SourcePage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText('只能属于旧地址的内容')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('link', { name: '打开另一记录' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('目标记录不存在');
    expect(screen.queryByText('只能属于旧地址的内容')).not.toBeInTheDocument();
  },
);

it('keeps the cited source version and location through navigation and refresh', async () => {
  const user = userEvent.setup();
  vi.stubGlobal('fetch', async (input: string) => {
    const data: Record<string, unknown> = {
      '/api/evidence/evidence-old': {
        id: 'evidence-old',
        title: '历史证据',
        text: '旧片段',
        start: 4,
        end: 7,
        source_id: 'source',
        source_version_id: 'version-old',
        segment_id: 'segment-old',
      },
      '/api/sources/source?version_id=version-old': {
        id: 'source',
        title: '固定旧版本',
        kind: 'text',
        status: 'ready',
        version_id: 'version-old',
        version: 1,
        segments: [{ id: 'segment-old', position: 0, status: 'ready', start: 0, end: 8 }],
      },
      '/api/source-segments/segment-old': {
        id: 'segment-old',
        text: '仍然显示旧版本原文',
        start: 0,
        end: 8,
        status: 'ready',
      },
      '/api/sources/source': {
        id: 'source',
        title: '错误的最新版本',
        version_id: 'version-new',
        version: 2,
        segments: [],
      },
    };
    return new Response(JSON.stringify(data[input] ?? { detail: 'unexpected URL' }), {
      status: input in data ? 200 : 404,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  const routes = (
    <Routes>
      <Route path="/evidence/:evidenceId" element={<EvidencePage />} />
      <Route path="/sources/:sourceId" element={<SourcePage />} />
    </Routes>
  );
  const mounted = render(
    <MemoryRouter initialEntries={['/evidence/evidence-old']}>{routes}</MemoryRouter>,
  );
  const link = await screen.findByRole('link', { name: '查看来源材料' });
  expect(link).toHaveAttribute('href', '/sources/source?version=version-old&segment=segment-old');
  await user.click(link);
  expect(await screen.findByText('仍然显示旧版本原文')).toBeVisible();
  expect(screen.queryByText('错误的最新版本')).toBeNull();
  mounted.unmount();
  render(
    <MemoryRouter initialEntries={['/sources/source?version=version-old&segment=segment-old']}>
      {routes}
    </MemoryRouter>,
  );
  expect(await screen.findByText('仍然显示旧版本原文')).toBeVisible();
  expect(screen.getByRole('link', { name: '查看原件' })).toHaveAttribute(
    'href',
    '/api/source-versions/version-old/original',
  );
});
