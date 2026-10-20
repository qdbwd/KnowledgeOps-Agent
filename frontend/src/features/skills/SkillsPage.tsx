import { useState } from 'react';
import { Link } from 'react-router-dom';
import { BookOpen, Download, Search, Upload } from 'lucide-react';
import { messageOf, send, useResource } from '../../shared/api/api';
import { useWorkspace } from '../../shared/state/workspace';
import { Button } from '../../shared/ui/Button';
import { Dialog } from '../../shared/ui/Dialog';
import { Empty, ErrorBox, Loading, PageTitle } from '../../shared/ui/components';
import { CapabilityDetails } from '../tools/CapabilityDetails';
import { SkillImportDialog } from './SkillImportDialog';
import type { SkillEntry } from './types';

export function SkillsPage() {
  const { version, refresh } = useWorkspace();
  const [local, setLocal] = useState(0);
  const catalog = useResource<SkillEntry[]>('/api/skills', `${version}:${local}`);
  const [query, setQuery] = useState('');
  const [editor, setEditor] = useState<{ skill: SkillEntry | null } | null>(null);
  const [detail, setDetail] = useState<SkillEntry | null>(null);
  const [confirm, setConfirm] = useState<{
    skill: SkillEntry;
    action: 'remove' | 'disable';
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const reload = () => {
    setLocal((value) => value + 1);
    refresh();
  };
  async function toggle(skill: SkillEntry, enabled: boolean) {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await send(
        '/api/tools/policy',
        { expected_revision: skill.policy_revision, skill_enabled: { [skill.name]: enabled } },
        'PATCH',
      );
      setNotice(enabled ? '技能已启用，新任务可以按需读取。' : '技能已停用。');
      setConfirm(null);
      reload();
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }
  async function remove(skill: SkillEntry) {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await send(
        `/api/skills/${encodeURIComponent(skill.name)}`,
        { expected_revision: skill.revision },
        'DELETE',
      );
      setNotice('技能已移除。');
      setConfirm(null);
      reload();
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }
  async function download(skill: SkillEntry) {
    setBusy(true);
    setError('');
    try {
      const response = await fetch(`/api/skills/${encodeURIComponent(skill.name)}/export`);
      if (!response.ok) {
        const result = await response.json();
        throw new Error(result.detail || '导出失败');
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = `${skill.name}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }
  const shown = (catalog.data ?? []).filter((skill) =>
    `${skill.name} ${skill.display_name} ${skill.description}`
      .toLocaleLowerCase()
      .includes(query.toLocaleLowerCase()),
  );
  return (
    <>
      <PageTitle eyebrow="任务方法" title="Skills" />
      <p className="text-muted">管理可按需读取的任务方法、脚本和配套资料。</p>
      <nav aria-label="能力管理入口" className="mb-5 flex flex-wrap gap-4 text-sm">
        <Link to="/tools">工具与能力</Link>
        <Link to="/mcp">MCP 服务</Link>
      </nav>
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <label className="flex min-w-0 items-center gap-2 rounded-lg border border-line bg-white px-3 text-muted">
          <Search size={17} />
          <input
            aria-label="搜索技能"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="min-w-0 border-0 bg-transparent"
            placeholder="搜索名称或用途"
          />
        </label>
        <Button
          disabled={busy}
          onClick={() => {
            setEditor({ skill: null });
            setError('');
          }}
        >
          <Upload size={16} />
          导入技能
        </Button>
      </div>
      {!confirm && <ErrorBox message={error} />}
      <ErrorBox message={catalog.error} />
      {(busy || notice) && (
        <p role="status" className="rounded-lg bg-[#e7f1e8] p-3 text-sm">
          {busy ? '正在处理…' : notice}
        </p>
      )}
      {catalog.loading && !catalog.data && <Loading />}
      {catalog.error && (
        <Button className="secondary button-secondary" onClick={reload}>
          重新加载
        </Button>
      )}
      {catalog.data && !shown.length && (
        <Empty title={query ? '没有匹配的技能' : '还没有技能'}>
          导入 SKILL.md 或一个完整文件包即可开始。
        </Empty>
      )}
      <div className="grid gap-4 lg:grid-cols-2">
        {shown.map((skill) => (
          <article
            key={skill.name}
            aria-label={skill.display_name}
            className="flex min-w-0 flex-col rounded-xl border border-line bg-white p-5"
          >
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <h2 className="m-0 text-base font-semibold [overflow-wrap:anywhere]">
                  {skill.display_name}
                </h2>
                <p className="mb-0 mt-1 text-xs text-muted [overflow-wrap:anywhere]">
                  {skill.name} · 修订 {skill.revision}
                </p>
              </div>
              <label className="flex shrink-0 items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  aria-label={`启用技能：${skill.display_name}`}
                  checked={skill.enabled}
                  disabled={busy || catalog.loading || !skill.available}
                  onChange={() => {
                    setError('');
                    if (skill.enabled) setConfirm({ skill, action: 'disable' });
                    else void toggle(skill, true);
                  }}
                />
                {skill.enabled ? '已启用' : '停用'}
              </label>
            </div>
            <p className="mb-4 flex-1 text-sm text-muted [overflow-wrap:anywhere]">
              {skill.summary || skill.description}
            </p>
            {skill.unavailable_reason && (
              <p className="rounded-lg bg-[#fbf4e9] p-3 text-xs">{skill.unavailable_reason}</p>
            )}
            <div className="flex flex-wrap items-center gap-3 border-t border-line pt-3 text-sm">
              <button
                type="button"
                className="inline-flex items-center gap-1 text-brand"
                onClick={() => setDetail(skill)}
              >
                <BookOpen size={15} />
                查看详情
              </button>
              <button
                type="button"
                className="text-brand disabled:opacity-40"
                disabled={busy || !skill.managed}
                onClick={() => {
                  setError('');
                  setEditor({ skill });
                }}
              >
                更新
              </button>
              <button
                type="button"
                className="inline-flex items-center gap-1 text-brand disabled:opacity-40"
                disabled={busy || !skill.managed}
                onClick={() => void download(skill)}
              >
                <Download size={15} />
                导出
              </button>
              <button
                type="button"
                className="ml-auto text-[#a44b40] disabled:opacity-40"
                disabled={busy || !skill.managed}
                onClick={() => {
                  setError('');
                  setConfirm({ skill, action: 'remove' });
                }}
              >
                移除
              </button>
            </div>
            {!skill.managed && (
              <p className="mb-0 text-xs text-muted">部署接管尚未完成，包更新与移除暂不可用。</p>
            )}
          </article>
        ))}
      </div>
      {editor && (
        <SkillImportDialog
          skill={editor.skill}
          onClose={() => setEditor(null)}
          onSaved={(text) => {
            setNotice(text);
            setEditor(null);
            reload();
          }}
        />
      )}
      {detail && (
        <CapabilityDetails
          choice={{
            ...detail,
            id: detail.name,
            detail_url: `/api/skills/${encodeURIComponent(detail.name)}`,
          }}
          onClose={() => setDetail(null)}
        />
      )}
      {confirm && (
        <Dialog
          title={confirm.action === 'remove' ? '移除技能' : '停用技能'}
          busy={busy}
          onClose={() => setConfirm(null)}
          footer={
            <>
              <Button
                className="secondary button-secondary"
                disabled={busy}
                onClick={() => setConfirm(null)}
              >
                取消
              </Button>
              <Button
                disabled={busy}
                onClick={() =>
                  void (confirm.action === 'remove'
                    ? remove(confirm.skill)
                    : toggle(confirm.skill, false))
                }
              >
                {confirm.action === 'remove' ? '确认移除技能' : '确认停用技能'}
              </Button>
            </>
          }
        >
          <ErrorBox message={error} />
          <p>
            确认{confirm.action === 'remove' ? '移除' : '停用'}“{confirm.skill.display_name}”？
          </p>
          <p className="text-sm">
            新任务将不再加载它；已经读取它的任务会在后续执行前停止，历史记录和固定快照继续保留。
          </p>
          {confirm.action === 'remove' && (
            <p className="text-sm">
              当前技能包的文件会移除。如需保存副本，请先导出。重新导入不会恢复旧任务的许可。
            </p>
          )}
        </Dialog>
      )}
    </>
  );
}
