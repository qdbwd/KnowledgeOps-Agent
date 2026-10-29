import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { AssetDeletionHistory } from '../sources/AssetDeletion';
import { MCPCleanup, MCPPreparations } from '../mcp/MCPManagementPanels';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('opens a selected asset failure even when it is outside the recent list, without retrying automatically', async () => {
  const requests = vi.fn<(url: string, options?: RequestInit) => Promise<Response>>(async (url) => Response.json(url === '/api/asset-deletions/old'
    ? { id: 'old', target_kind: 'source', status: 'failed', summary: { title: '旧材料清理' } } : []));
  vi.stubGlobal('fetch', requests);
  render(<MemoryRouter initialEntries={['/sources?deletion=old']}><AssetDeletionHistory /></MemoryRouter>);
  expect(await screen.findByText('旧材料清理')).toBeVisible();
  expect(within(screen.getByLabelText('选中的删除记录')).getByRole('button', { name: '重试清理' })).toBeVisible();
  expect(requests.mock.calls.every(([, options]) => !options?.method)).toBe(true);
});

it('opens the original MCP preparation and cleanup controls from stable operation links', async () => {
  const requests = vi.fn(async (url: string) => {
    if (url === '/api/mcp/operations/old') return Response.json({ id: 'old', status: 'failed', attempts: 1,
      parameters: { name: '旧准备操作', installation: { manager: 'npm', packages: ['sample'], entrypoint: null } }, result: {} });
    if (url === '/api/mcp/cleanup/clean') return Response.json({ id: 'clean', name: '旧清理操作', status: 'failed', result: {} });
    return Response.json([]);
  });
  vi.stubGlobal('fetch', requests);
  render(<MemoryRouter initialEntries={['/mcp?operation=old&cleanup=clean']}>
    <MCPPreparations version={0} changed={() => {}} /><MCPCleanup version={0} />
  </MemoryRouter>);
  expect(await screen.findByText('旧准备操作')).toBeVisible();
  expect(await screen.findByText('旧清理操作')).toBeVisible();
  expect(screen.getByRole('button', { name: '重新准备依赖' })).toBeVisible();
  expect(screen.getByRole('button', { name: '重新检查并清理' })).toBeVisible();
});
