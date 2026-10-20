import { useState } from 'react';
import { api, messageOf, send } from '../../shared/api/api';
import { Button } from '../../shared/ui/Button';
import { Dialog } from '../../shared/ui/Dialog';
import { ErrorBox } from '../../shared/ui/components';
import type { SkillEntry, SkillPreview } from './types';

type Trial = { path: string; content: string; next_offset: number | null; total_lines: number };

export function SkillImportDialog({
  skill,
  onClose,
  onSaved,
}: {
  skill: SkillEntry | null;
  onClose: () => void;
  onSaved: (notice: string) => void;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [name, setName] = useState(skill?.name ?? '');
  const [description, setDescription] = useState('');
  const [preview, setPreview] = useState<SkillPreview | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [trial, setTrial] = useState<Trial | null>(null);
  const [resource, setResource] = useState('SKILL.md');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const title = skill ? `更新技能：${skill.display_name}` : '导入技能';
  function invalidate() {
    setPreview(null);
    setTrial(null);
  }
  function choose(values: FileList | null) {
    setFiles(Array.from(values ?? []));
    invalidate();
    setError('');
  }
  async function discard() {
    if (pendingId) {
      await send(`/api/skills/imports/${pendingId}`, undefined, 'DELETE');
      setPendingId(null);
    }
  }
  async function close() {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      if (pendingId) {
        const status = await api<{ status: string }>(`/api/skills/imports/${pendingId}`);
        if (status.status === 'succeeded') {
          setPendingId(null);
          onSaved('技能已保存。');
          return;
        }
      }
      await discard();
      onClose();
    } catch (failure) {
      setError(`取消暂存失败：${messageOf(failure)}。可重试；未提交的上传也会自动到期。`);
    } finally {
      setBusy(false);
    }
  }
  async function inspect() {
    if (busy || !files.length) return;
    setBusy(true);
    setError('');
    invalidate();
    try {
      await discard();
      const data = new FormData();
      files.forEach((file) => data.append('files', file, file.webkitRelativePath || file.name));
      if (name.trim()) data.append('name', name.trim());
      if (description.trim()) data.append('description', description.trim());
      data.append('expected_revision', String(skill?.revision ?? 0));
      const result = await api<SkillPreview>('/api/skills/imports', { method: 'POST', body: data });
      setPreview(result);
      setPendingId(result.id);
      setName(result.preview.name);
      setDescription(result.preview.description);
      setResource('SKILL.md');
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }
  async function read(offset = 0) {
    if (!preview || busy) return;
    setBusy(true);
    setError('');
    try {
      const result = await send<Trial>(`/api/skills/imports/${preview.id}/try-read`, {
        path: resource,
        offset,
      });
      setTrial(result);
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }
  async function commit() {
    if (!preview || busy || preview.preview.missing_metadata.length) return;
    setBusy(true);
    setError('');
    try {
      await send(`/api/skills/imports/${preview.id}/commit`);
      setPendingId(null);
      onSaved(
        skill
          ? preview.preview.compatible
            ? '技能已更新。'
            : '技能已更新，因兼容性问题保持停用。'
          : '技能已导入，确认使用条件后可以启用。',
      );
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      title={title}
      busy={busy}
      onClose={() => void close()}
      footer={
        <>
          <Button
            className="secondary button-secondary"
            disabled={busy}
            onClick={() => void close()}
          >
            取消
          </Button>
          <Button
            className="secondary button-secondary"
            disabled={busy || !files.length}
            onClick={() => void inspect()}
          >
            {busy ? '正在处理…' : preview ? '重新检查' : '检查并预览'}
          </Button>
          <Button
            disabled={busy || !preview || Boolean(preview.preview.missing_metadata.length)}
            onClick={() => void commit()}
          >
            {skill ? '确认更新' : '确认导入（停用）'}
          </Button>
        </>
      }
    >
      <p className="mt-0 text-sm text-muted">
        导入 SKILL.md 或包含脚本、参考资料和资源的完整包。导入不会安装依赖或执行脚本。
      </p>
      {skill && (
        <p className="rounded-lg bg-canvas p-3 text-sm">
          更新替换这个技能的完整文件包。已开始的任务继续使用固定版本；不兼容的更新会停用技能。
        </p>
      )}
      <ErrorBox message={error} />
      <fieldset disabled={busy} className="m-0 grid gap-4 border-0 p-0">
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="grid min-w-0 gap-2 text-sm">
            选择 SKILL.md 或 ZIP
            <input
              type="file"
              accept=".md,.zip"
              onChange={(event) => choose(event.target.files)}
              className="w-full min-w-0 rounded-lg border border-line p-2 text-xs"
            />
          </label>
          <label className="grid min-w-0 gap-2 text-sm">
            选择完整文件夹
            <input
              type="file"
              multiple
              ref={(node) => {
                node?.setAttribute('webkitdirectory', '');
              }}
              onChange={(event) => choose(event.target.files)}
              className="w-full min-w-0 rounded-lg border border-line p-2 text-xs"
            />
          </label>
        </div>
        <p className="my-0 text-xs text-muted" role="status">
          {files.length ? `已选择 ${files.length} 个文件` : '尚未选择文件'}
        </p>
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="grid gap-2 text-sm">
            技能名称
            <input
              aria-label="技能名称"
              value={name}
              readOnly={Boolean(skill)}
              placeholder="从文件读取，缺少时填写"
              maxLength={64}
              onChange={(event) => {
                setName(event.target.value);
                invalidate();
              }}
            />
            <span className="text-xs text-muted">小写英文、数字与连字符；更新时保持名称。</span>
          </label>
          <label className="grid gap-2 text-sm">
            用途说明
            <textarea
              rows={3}
              value={description}
              placeholder="从文件读取，缺少时填写"
              maxLength={1024}
              onChange={(event) => {
                setDescription(event.target.value);
                invalidate();
              }}
            />
          </label>
        </div>
      </fieldset>
      {preview && (
        <section aria-label="导入预览" className="mt-5 space-y-4 border-t border-line pt-4">
          <h3 className="m-0">{preview.preview.name || '待补填名称'}</h3>
          {preview.preview.local_changes && (
            <p className="rounded-lg bg-[#fbf4e9] p-3 text-sm" role="alert">
              当前技能文件曾在页面外修改。确认更新会替换整个包；如需保留这些改动，请先取消并导出当前文件。
            </p>
          )}
          {Boolean(preview.preview.missing_metadata.length) && (
            <p className="rounded-lg bg-[#fbf4e9] p-3 text-sm">请补填名称和用途，然后重新检查。</p>
          )}
          {preview.preview.compatibility_issues.map((issue) => (
            <p key={issue} className="rounded-lg bg-[#fbf4e9] p-3 text-sm">
              {issue}
            </p>
          ))}
          <p className="text-sm">
            {preview.preview.compatibility ||
              '运行依赖请核对技能正文。脚本尚未执行，依赖是否齐备尚未验证。'}
          </p>
          <details className="rounded-lg border border-line p-3">
            <summary className="cursor-pointer text-sm">
              完整文件清单 · {preview.preview.resources.length} 个文件 ·{' '}
              {preview.preview.total_bytes.toLocaleString()} 字节
            </summary>
            <ul className="max-h-52 overflow-y-auto pl-5 text-xs [overflow-wrap:anywhere]">
              {preview.preview.resources.map((item) => (
                <li key={item.path} className="my-2">
                  {item.path} · {item.size.toLocaleString()} 字节{item.binary ? ' · 二进制' : ''}
                </li>
              ))}
            </ul>
          </details>
          <details className="rounded-lg border border-line p-3">
            <summary className="cursor-pointer text-sm">查看将保存的 SKILL.md</summary>
            <pre className="max-h-80 overflow-auto whitespace-pre-wrap text-xs [overflow-wrap:anywhere]">
              {preview.preview.content}
            </pre>
          </details>
          <div className="flex flex-wrap items-end gap-3">
            <label className="grid min-w-0 flex-1 gap-2 text-sm">
              试读资源
              <select
                value={resource}
                disabled={busy}
                onChange={(event) => {
                  setResource(event.target.value);
                  setTrial(null);
                }}
              >
                {preview.preview.resources.map((item) => (
                  <option key={item.path} value={item.path} disabled={item.binary}>
                    {item.path}
                    {item.binary ? '（二进制）' : ''}
                  </option>
                ))}
              </select>
            </label>
            <Button
              className="secondary button-secondary"
              disabled={busy || Boolean(preview.preview.missing_metadata.length)}
              onClick={() => void read()}
            >
              试读
            </Button>
          </div>
          {trial && (
            <section aria-label="试读结果" className="rounded-lg bg-canvas p-3">
              <p className="mt-0 text-xs text-muted">
                已通过技能读取器读取 · {trial.total_lines} 行 · 配套脚本未执行
              </p>
              <pre className="max-h-72 overflow-auto whitespace-pre-wrap text-xs [overflow-wrap:anywhere]">
                {trial.content}
              </pre>
              {trial.next_offset !== null && (
                <Button
                  className="secondary button-secondary"
                  disabled={busy}
                  onClick={() => void read(trial.next_offset!)}
                >
                  读取下一段
                </Button>
              )}
            </section>
          )}
        </section>
      )}
    </Dialog>
  );
}
