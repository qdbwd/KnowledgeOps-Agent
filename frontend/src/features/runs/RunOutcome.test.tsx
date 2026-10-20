import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { RunOutcome } from './RunOutcome';
import type { Run } from '../../shared/types/types';
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
const run = { id: 'run', outcome: { kind: 'draft_ready' } } as Run;
it('uses current published status instead of historical draft_ready', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      Response.json({ drafts: [{ id: 'draft', status: 'published' }], drafts_truncated: false }),
    ),
  );
  render(
    <MemoryRouter>
      <RunOutcome run={run} refreshKey={0} />
    </MemoryRouter>,
  );
  await screen.findByText('已发布');
  expect(screen.queryByText('草稿待审核')).not.toBeInTheDocument();
});
it('links an actual pending draft to knowledge review', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      Response.json({ drafts: [{ id: 'draft', status: 'draft' }], drafts_truncated: false }),
    ),
  );
  render(
    <MemoryRouter>
      <RunOutcome run={run} refreshKey={0} />
    </MemoryRouter>,
  );
  expect(await screen.findByRole('link', { name: '草稿待审核 →' })).toHaveAttribute(
    'href',
    '/knowledge/reviews/draft',
  );
});
