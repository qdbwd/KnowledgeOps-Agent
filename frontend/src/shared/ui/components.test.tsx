// 关注用户能否安全阅读证据、审阅影响及保留冲突编辑，避免只测试组件实现细节。
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { Markdown, ChangeReview, KnowledgeEditor } from './components';
import type { ChangeSet, Knowledge } from '../types/types';

afterEach(cleanup);

describe('reading and review boundaries', () => {
  it('opens workspace artifacts in the full document reader', () => {
    render(
      <MemoryRouter>
        <Markdown text="[笔记](/api/workspace/download?path=outputs%2Fnote.md)" />
      </MemoryRouter>,
    );
    const link = screen.getByRole('link', { name: '笔记' });
    expect(link).not.toHaveAttribute('download');
    expect(link).toHaveAttribute('href', '/files?path=outputs%2Fnote.md');
  });
  it('opens evidence in the source view and does not render embedded raw HTML', () => {
    const { container } = render(
      <MemoryRouter>
        <Markdown text={'证据[原文](/api/evidence/abc)\n<script>window.bad=1</script>'} />
      </MemoryRouter>,
    );
    expect(screen.getByRole('link', { name: '原文' })).toHaveAttribute('href', '/evidence/abc');
    expect(container.querySelector('script')).toBeNull();
  });

  it('requires visible impact confirmation before publishing a structural change', async () => {
    const user = userEvent.setup();
    const draft: ChangeSet = {
      id: 'draft',
      root_id: 'run',
      status: 'draft',
      revision: 2,
      proactive: false,
      requires_impact_confirmation: true,
      impact_confirmed: false,
      before: {},
      operations: [
        { action: 'archive', document_id: 'old', title: '旧文', body: '' },
        { action: 'create', document_id: 'new', title: '新文', body: '合并后的正文' },
      ],
      differences: [],
    };
    render(
      <MemoryRouter>
        <ChangeReview
          draft={draft}
          busy={false}
          onPublish={async () => {}}
          onReject={async () => {}}
          onEdit={async () => {}}
        />
      </MemoryRouter>,
    );
    expect(screen.getByRole('button', { name: '确认并发布' })).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: /已核对/ }));
    expect(screen.getByRole('button', { name: '确认并发布' })).toBeEnabled();
    expect(screen.getByText('合并后的正文')).toBeVisible();
  });

  it('keeps the edited body visible when saving hits a version conflict', async () => {
    const user = userEvent.setup();
    const article: Knowledge = {
      id: 'doc',
      document_id: 'doc',
      version_id: 'v1',
      version: 1,
      title: '原文',
      body: '旧正文',
      tags: [],
      category: '',
      summary: '',
      archived: false,
      relations: [],
      citation_ids: [],
    };
    render(
      <MemoryRouter>
        <KnowledgeEditor
          article={article}
          onCancel={() => {}}
          onSave={async () => {
            throw new Error('version conflict');
          }}
        />
      </MemoryRouter>,
    );
    const editor = screen.getByLabelText('Markdown 正文');
    await user.clear(editor);
    await user.type(editor, '需要保留的修改');
    await user.click(screen.getByRole('button', { name: '保存新版本' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('version conflict');
    expect(editor).toHaveValue('需要保留的修改');
  });
});
