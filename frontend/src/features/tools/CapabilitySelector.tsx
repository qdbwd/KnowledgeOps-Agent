import { useId, useState } from 'react';
import { Check, Info, Search } from 'lucide-react';
import { Button } from '../../shared/ui/Button';
import { Dialog } from '../../shared/ui/Dialog';
import { CapabilityDetails } from './CapabilityDetails';

import { capabilityName, type CapabilityChoice } from './capabilities';
export { capabilityName, type CapabilityChoice } from './capabilities';

/** 组成员与角色工具共用选择器；只维护草稿，实际权限由后端计算。 */
export function CapabilitySelector({
  title,
  options,
  selected,
  onChange,
  disabled = false,
  description,
  buttonLabel,
}: {
  title: string;
  options: CapabilityChoice[];
  selected: string[];
  onChange?: (value: string[]) => void;
  disabled?: boolean;
  description?: string;
  buttonLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [detail, setDetail] = useState<CapabilityChoice | null>(null);
  const inputPrefix = useId();
  const choices: CapabilityChoice[] = [
    ...options,
    ...selected
      .filter((id) => !options.some((row) => row.id === id))
      .map((id) => ({ id, name: id, unavailable_reason: '目录中已不存在，可取消选择。' })),
  ];
  const chosen = choices.filter((row) => selected.includes(row.id));
  const shown = (onChange ? choices : chosen).filter((row) =>
    `${row.name} ${row.display_name ?? ''} ${row.description ?? ''}`
      .toLocaleLowerCase()
      .includes(query.toLocaleLowerCase()),
  );
  return (
    <section aria-label={title} className="min-w-0 rounded-xl border border-line bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <strong>{title}</strong>
          <span className="ml-2 text-xs text-muted">
            {onChange ? '已选 ' : ''}
            {selected.length} 项
          </span>
        </div>
        <Button
          type="button"
          className="secondary button-secondary"
          disabled={disabled}
          onClick={() => {
            setQuery('');
            setOpen(true);
          }}
        >
          {buttonLabel ?? (onChange ? `选择${title}` : `查看${title}`)}
        </Button>
      </div>
      {description && <p className="mb-0 text-xs text-muted">{description}</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        {chosen.slice(0, 4).map((row) => (
          <span key={row.id} className="rounded-md bg-canvas px-2 py-1 text-xs">
            {capabilityName(row)}
          </span>
        ))}
        {chosen.length > 4 && (
          <span className="py-1 text-xs text-muted">另 {chosen.length - 4} 项</span>
        )}
        {!chosen.length && <span className="text-xs text-muted">尚未选择</span>}
      </div>
      {open && (
        <Dialog
          title={title}
          onClose={() => setOpen(false)}
          footer={
            <>
              <span className="mr-auto text-sm text-muted">
                {selected.length} 项{onChange ? '已选' : ''}
              </span>
              <Button type="button" onClick={() => setOpen(false)}>
                完成
              </Button>
            </>
          }
        >
          <label className="mb-4 flex flex-row items-center gap-2 rounded-lg border border-line px-3">
            <Search size={17} className="shrink-0 text-muted" />
            <input
              type="search"
              aria-label={`搜索${title}`}
              placeholder="搜索名称、用途或函数名"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              className="min-w-0 border-0! px-0! shadow-none!"
            />
          </label>
          <div className="grid grid-cols-2 gap-3 max-md:grid-cols-1">
            {shown.map((row) => (
              <article
                key={row.id}
                className={`flex min-w-0 items-start gap-3 rounded-xl border p-3 ${selected.includes(row.id) ? 'border-[#93b5a5] bg-[#f5f9f5]' : 'border-line'}`}
              >
                {onChange ? (
                  <input
                    id={`${inputPrefix}-${row.id}`}
                    className="mt-1 shrink-0"
                    type="checkbox"
                    aria-label={capabilityName(row)}
                    checked={selected.includes(row.id)}
                    disabled={disabled || row.disabled}
                    onChange={(event) =>
                      onChange(
                        event.target.checked
                          ? [...selected, row.id]
                          : selected.filter((id) => id !== row.id),
                      )
                    }
                  />
                ) : (
                  <Check size={16} className="mt-1 shrink-0 text-brand" />
                )}
                <div className="min-w-0 flex-1">
                  {onChange ? (
                    <label
                      htmlFor={`${inputPrefix}-${row.id}`}
                      className="m-0 block cursor-pointer text-sm font-medium [overflow-wrap:anywhere]"
                    >
                      {capabilityName(row)}
                    </label>
                  ) : (
                    <span className="block text-sm font-medium [overflow-wrap:anywhere]">
                      {capabilityName(row)}
                    </span>
                  )}
                  <p className="my-1 line-clamp-2 text-xs text-muted">
                    {row.summary || row.description}
                  </p>
                  {row.status && <span className="text-xs text-brand">{row.status}</span>}
                  {row.unavailable_reason && (
                    <p className="my-1 text-xs text-[#965b26]">{row.unavailable_reason}</p>
                  )}
                </div>
                <button
                  type="button"
                  aria-label={`查看${capabilityName(row)}详情`}
                  className="shrink-0 rounded p-1 text-muted hover:bg-white"
                  onClick={() => setDetail(row)}
                >
                  <Info size={16} />
                </button>
              </article>
            ))}
          </div>
          {!shown.length && <p className="text-muted">没有匹配的项目。</p>}
        </Dialog>
      )}
      {detail && <CapabilityDetails choice={detail} onClose={() => setDetail(null)} />}
    </section>
  );
}
