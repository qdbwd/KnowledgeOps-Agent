import { useState } from 'react';
import { send, messageOf } from '../../shared/api/api';
import { Button } from '../../shared/ui/Button';
import { Dialog } from '../../shared/ui/Dialog';
import { ErrorBox } from '../../shared/ui/components';

export type MemoryCategory = { id: string; name: string; revision: number; memory_count: number };

export function CategorySelect({
  value,
  onChange,
  categories,
  disabled = false,
  automatic = false,
}: {
  value: string;
  onChange: (value: string) => void;
  categories: MemoryCategory[];
  disabled?: boolean;
  automatic?: boolean;
}) {
  return (
    <label className="min-w-0">
      类别（可选）
      <select value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
        <option value="">{automatic ? '自动分类' : '未分类'}</option>
        {value && !categories.some((row) => row.id === value) && (
          <option value={value}>原类别已不可用</option>
        )}
        {categories.map((row) => (
          <option key={row.id} value={row.id}>
            {row.name}
          </option>
        ))}
      </select>
    </label>
  );
}

function CategoryRow({
  row,
  changed,
  remove,
}: {
  row: MemoryCategory;
  changed: () => void;
  remove: () => void;
}) {
  const [name, setName] = useState(row.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return (
    <form
      className="rounded-xl border border-line p-4"
      onSubmit={async (event) => {
        event.preventDefault();
        setBusy(true);
        setError('');
        try {
          await send(
            `/api/memory-categories/${row.id}`,
            { name, expected_revision: row.revision },
            'PATCH',
          );
          changed();
        } catch (failure) {
          setError(messageOf(failure));
        } finally {
          setBusy(false);
        }
      }}
    >
      <label>
        类别名称
        <input
          aria-label={`类别名称 ${row.name}`}
          value={name}
          maxLength={100}
          disabled={busy}
          onChange={(event) => setName(event.target.value)}
        />
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <span className="mr-auto text-xs text-muted">{row.memory_count} 条记忆</span>
        <Button
          type="submit"
          className="button-secondary"
          disabled={busy || !name.trim() || name === row.name}
        >
          保存名称
        </Button>
        <Button type="button" className="button-quiet text-danger" disabled={busy} onClick={remove}>
          删除类别
        </Button>
      </div>
      <ErrorBox message={error} />
    </form>
  );
}

/** 类别管理只改变展示归属；删除确认明确告诉用户正文会保留。 */
export function CategoryManager({
  categories,
  onChanged,
  onClose,
}: {
  categories: MemoryCategory[];
  onChanged: () => void;
  onClose: () => void;
}) {
  const [name, setName] = useState('');
  const [deleting, setDeleting] = useState<MemoryCategory | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return (
    <Dialog title="管理记忆类别" busy={busy} onClose={onClose}>
      <p className="text-sm text-muted">
        类别用来整理记忆。改名或删除类别不会改变记忆内容、生效状态或适用范围。
      </p>
      <form
        className="mb-5 rounded-xl bg-canvas p-4"
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError('');
          try {
            await send('/api/memory-categories', { name });
            setName('');
            onChanged();
          } catch (failure) {
            setError(messageOf(failure));
          } finally {
            setBusy(false);
          }
        }}
      >
        <label>
          新类别名称
          <input
            value={name}
            maxLength={100}
            disabled={busy}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <Button disabled={busy || !name.trim()}>新增类别</Button>
      </form>
      <ErrorBox message={error} />
      <div className="grid grid-cols-2 gap-3 max-md:grid-cols-1">
        {categories.map((row) => (
          <CategoryRow
            key={`${row.id}:${row.revision}`}
            row={row}
            changed={onChanged}
            remove={() => {
              setDeleting(row);
              setError('');
            }}
          />
        ))}
      </div>
      {deleting && (
        <Dialog
          title={`删除类别“${deleting.name}”？`}
          busy={busy}
          onClose={() => setDeleting(null)}
          footer={
            <>
              <Button
                type="button"
                className="button-secondary"
                disabled={busy}
                onClick={() => setDeleting(null)}
              >
                保留类别
              </Button>
              <Button
                type="button"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  setError('');
                  try {
                    await send(
                      `/api/memory-categories/${deleting.id}`,
                      { expected_revision: deleting.revision },
                      'DELETE',
                    );
                    setDeleting(null);
                    onChanged();
                  } catch (failure) {
                    setError(messageOf(failure));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                删除类别并保留记忆
              </Button>
            </>
          }
        >
          <p>该类别下的 {deleting.memory_count} 条记忆将移到“未分类”，记忆正文和历史修订保留。</p>
          <ErrorBox message={error} />
        </Dialog>
      )}
    </Dialog>
  );
}
