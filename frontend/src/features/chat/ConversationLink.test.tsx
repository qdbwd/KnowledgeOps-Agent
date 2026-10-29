import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { WorkspaceContext } from '../../shared/state/workspace';
import { BackgroundDeletions } from './BackgroundDeletions';
import { ConversationLink } from './ConversationLink';

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});

const chat = { id: 'chat', title: '待管理的会话' };
const preview = {
  title: chat.title,
  state: 'active',
  version: 'a'.repeat(64),
  eligible: true,
  blockers: [],
  counts: { messages: 4, runs: 2 },
  preserved: ['长期记忆正文'],
};

function Location() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

function show(path: string) {
  const refresh = vi.fn();
  const requests = vi.fn(async (url: string) =>
    Response.json(url.endsWith('/deletion-preview') ? preview : { status: 'pending' }),
  );
  vi.stubGlobal('fetch', requests);
  render(
    <MemoryRouter initialEntries={[path]}>
      <WorkspaceContext.Provider value={{ version: 0, refresh }}>
        <BackgroundDeletions>
          <ConversationLink chat={chat} />
          <Location />
        </BackgroundDeletions>
      </WorkspaceContext.Provider>
    </MemoryRouter>,
  );
  return { refresh, requests, user: userEvent.setup() };
}

it.each([
  { path: '/chat/chat', destination: '/', opener: 'contextmenu' },
  { path: '/chat/other', destination: '/chat/other', opener: 'button' },
])(
  'confirms sidebar deletion from $path and navigates only for the current chat',
  async ({ path, destination, opener }) => {
    const { refresh, requests, user } = show(path);
    if (opener === 'contextmenu')
      fireEvent.contextMenu(screen.getByRole('link', { name: chat.title }));
    else await user.click(screen.getByRole('button', { name: `会话“${chat.title}”的选项` }));
    expect(screen.getByRole('menuitem', { name: '重命名' })).toBeInTheDocument();
    await user.click(screen.getByRole('menuitem', { name: '删除会话' }));
    await screen.findByText(/以下内容会保留/);
    expect(requests).not.toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ method: 'DELETE' }),
    );
    await user.click(screen.getByRole('button', { name: '确认删除会话' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(requests).toHaveBeenCalledWith(
      '/api/conversations/chat',
      expect.objectContaining({
        method: 'DELETE',
        body: JSON.stringify({ expected_version: preview.version }),
      }),
    );
    expect(refresh).toHaveBeenCalledOnce();
    expect(screen.getByTestId('location').textContent).toBe(destination);
    expect(localStorage.getItem('knowledgeops:pending-conversation-deletions')).toBeNull();
  },
);

it('opens deletion with the keyboard and cancels without deleting or leaving the conversation', async () => {
  const { refresh, requests, user } = show('/chat/chat');
  const link = screen.getByRole('link', { name: chat.title });
  link.focus();
  await user.keyboard('{Shift>}{F10}{/Shift}');
  expect(screen.getByRole('menuitem', { name: '重命名' })).toHaveFocus();
  await user.keyboard('{ArrowDown}{Enter}');
  await screen.findByText(/以下内容会保留/);
  await user.click(screen.getByRole('button', { name: '取消' }));
  await waitFor(() => expect(link).toHaveFocus());
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByTestId('location')).toHaveTextContent('/chat/chat');
  expect(requests).not.toHaveBeenCalledWith(
    expect.anything(),
    expect.objectContaining({ method: 'DELETE' }),
  );
  expect(refresh).not.toHaveBeenCalled();
});
