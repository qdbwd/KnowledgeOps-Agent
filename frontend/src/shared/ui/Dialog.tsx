import { useEffect, useId, useRef } from 'react';
import type { ReactNode } from 'react';
import { X } from 'lucide-react';

/** 原生模态层约束背景交互；React 卸载时显式返回触发按钮，避免焦点落到 body。 */
export function Dialog({
  title,
  children,
  footer,
  onClose,
  busy = false,
}: {
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  onClose: () => void;
  busy?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current!;
    const returnTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.showModal();
    return () => {
      dialog.close();
      document.body.style.overflow = previousOverflow;
      queueMicrotask(() => {
        if (!dialog.open && returnTo?.isConnected) returnTo.focus({ preventScroll: true });
      });
    };
  }, []);
  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onCancel={(event) => {
        // React 会把嵌套 dialog 的 cancel 冒泡给外层；Escape 每次只关闭当前层。
        event.stopPropagation();
        event.preventDefault();
        if (!busy) onClose();
      }}
      onKeyDown={(event) => {
        if ((event.target as Element).closest('dialog') !== event.currentTarget) return;
        // search 输入会先消费 Escape 清空查询，浏览器因此不触发 cancel。
        // 模态层统一处理关闭；阻止冒泡，使嵌套详情只关闭最上层。
        if (event.key === 'Escape') {
          event.stopPropagation();
          event.preventDefault();
          if (!busy) onClose();
          return;
        }
        // 浏览器允许 Tab 从原生 dialog 暂时移向浏览器栏；管理表单在首尾循环。
        // 嵌套详情只由最上层 dialog 处理，避免父框抢走焦点。
        if (event.key !== 'Tab') return;
        const focusable = Array.from(
          event.currentTarget.querySelectorAll<HTMLElement>(
            'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]',
          ),
        ).filter((element) => element.offsetParent !== null);
        const first = focusable[0];
        const last = focusable.at(-1);
        if (!first) {
          event.preventDefault();
          event.currentTarget.focus();
        } else if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }}
      className="fixed inset-0 m-auto max-h-[90dvh] w-[min(800px,calc(100%_-_32px))] max-w-none rounded-2xl border border-line bg-white p-0 text-ink shadow-xl backdrop:bg-[#102e27]/35 open:flex open:flex-col max-md:max-h-[94dvh] max-md:w-[calc(100%_-_20px)]"
    >
      <header className="flex shrink-0 items-center justify-between gap-4 border-b border-line px-6 py-4 max-md:px-4">
        <h2 id={titleId} className="m-0 text-lg font-semibold">
          {title}
        </h2>
        <button
          type="button"
          aria-label={`关闭${title}`}
          disabled={busy}
          onClick={onClose}
          className="rounded-lg p-2 text-muted hover:bg-canvas focus-visible:outline-2 focus-visible:outline-brand"
        >
          <X size={20} />
        </button>
      </header>
      <div className="min-h-0 overflow-y-auto overscroll-contain px-6 py-5 max-md:px-4">
        {children}
      </div>
      {footer && (
        <footer className="flex shrink-0 flex-wrap items-center justify-end gap-3 border-t border-line bg-white px-6 py-4 max-md:px-4">
          {footer}
        </footer>
      )}
    </dialog>
  );
}
