import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { NavLink, useMatch, useNavigate } from 'react-router-dom';
import { MoreHorizontal, Pencil, Trash2 } from 'lucide-react';
import { messageOf, send } from '../../shared/api/api';
import { useWorkspace } from '../../shared/state/workspace';
import type { Conversation } from '../../shared/types/types';
import { Button } from '../../shared/ui/Button';
import { Dialog } from '../../shared/ui/Dialog';
import { ErrorBox } from '../../shared/ui/components';
import { DeleteConversationDialog } from './DeleteConversationDialog';
import { ConversationActivity } from '../activity/ApplicationActivity';

/** 桌面右键、键盘菜单键与手机按钮共用重命名/删除菜单；删除沿用原预览和清理流程。 */
export function ConversationLink({ chat }: { chat: Conversation }) {
  const { refresh } = useWorkspace();
  const navigate = useNavigate();
  const currentChat = useMatch('/chat/:conversationId');
  const link = useRef<HTMLAnchorElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [title, setTitle] = useState(chat.title);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  function openMenu(x: number, y: number) {
    setMenu({
      x: Math.max(8, Math.min(x, window.innerWidth - 176)),
      y: Math.max(8, Math.min(y, window.innerHeight - 104)),
    });
  }
  function closeEditor() {
    setEditing(false);
    queueMicrotask(() => link.current?.focus());
  }
  function closeDelete() {
    setDeleting(false);
    queueMicrotask(() => link.current?.focus());
  }
  useEffect(() => {
    if (!menu) return;
    menuRef.current?.querySelector('button')?.focus({ preventScroll: true });
    const outside = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenu(null);
    };
    const close = () => setMenu(null);
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape' || event.key === 'Tab') {
        setMenu(null);
        link.current?.focus();
      } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        const items = Array.from(
          menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [],
        );
        const current = items.findIndex((item) => item === document.activeElement);
        const next =
          event.key === 'Home'
            ? 0
            : event.key === 'End'
              ? items.length - 1
              : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        items[next]?.focus();
      }
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', key);
    window.addEventListener('resize', close);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', key);
      window.removeEventListener('resize', close);
    };
  }, [menu]);
  return (
    <>
      <div className="group flex min-w-0 items-center rounded-md hover:bg-[#244237]">
        <NavLink
          ref={link}
          to={`/chat/${chat.id}`}
          title={chat.title}
          className="min-w-0 flex-1 truncate rounded-[5px] px-[7px] py-[9px] text-xs text-[#b2c4b7] hover:no-underline [&.active]:bg-[#27483b] [&.active]:text-[#f0f5e8]"
          onContextMenu={(event) => {
            event.preventDefault();
            openMenu(event.clientX, event.clientY);
          }}
          onKeyDown={(event) => {
            if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
              event.preventDefault();
              const rect = event.currentTarget.getBoundingClientRect();
              openMenu(rect.left, rect.bottom);
            }
          }}
        >
          {chat.title}
        </NavLink>
        <ConversationActivity id={chat.id} />
        <button
          type="button"
          className="shrink-0 rounded p-1 text-[#b2c4b7] opacity-0 hover:bg-[#28483b] focus:opacity-100 group-hover:opacity-100 max-md:opacity-100"
          aria-label={`会话“${chat.title}”的选项`}
          aria-haspopup="menu"
          aria-expanded={Boolean(menu)}
          onClick={(event) => {
            const rect = event.currentTarget.getBoundingClientRect();
            openMenu(rect.left, rect.bottom);
          }}
        >
          <MoreHorizontal size={16} />
        </button>
      </div>
      {menu &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            aria-label="会话选项"
            style={{ left: menu.x, top: menu.y }}
            className="fixed z-50 w-[168px] rounded-lg border border-line bg-white p-1 text-ink shadow-lg"
          >
            <button
              role="menuitem"
              type="button"
              className="flex w-full items-center gap-2 rounded px-3 py-2 text-sm hover:bg-canvas"
              onClick={() => {
                setMenu(null);
                setTitle(chat.title);
                setError('');
                setEditing(true);
              }}
            >
              <Pencil size={15} />
              重命名
            </button>
            <button
              role="menuitem"
              type="button"
              className="flex w-full items-center gap-2 rounded px-3 py-2 text-sm text-red-700 hover:bg-red-50"
              onClick={() => {
                setMenu(null);
                setDeleting(true);
              }}
            >
              <Trash2 size={15} />
              删除会话
            </button>
          </div>,
          document.body,
        )}
      {editing && (
        <Dialog title="重命名会话" onClose={closeEditor} busy={busy}>
          <form
            onSubmit={async (event) => {
              event.preventDefault();
              if (!title.trim() || busy) return;
              setBusy(true);
              setError('');
              try {
                await send(`/api/conversations/${chat.id}`, { title: title.trim() }, 'PATCH');
                refresh();
                closeEditor();
              } catch (failure) {
                setError(messageOf(failure));
              } finally {
                setBusy(false);
              }
            }}
          >
            <label>
              会话名称
              <input
                autoFocus
                maxLength={300}
                value={title}
                onChange={(event) => setTitle(event.target.value)}
              />
            </label>
            <ErrorBox message={error} />
            <div className="mt-4 flex justify-end gap-3">
              <Button
                type="button"
                className="secondary button-secondary"
                disabled={busy}
                onClick={closeEditor}
              >
                取消
              </Button>
              <Button disabled={busy || !title.trim()}>保存名称</Button>
            </div>
          </form>
        </Dialog>
      )}
      {deleting && (
        <DeleteConversationDialog
          conversationId={chat.id}
          onClose={closeDelete}
          onDeleted={() => {
            setDeleting(false);
            // 侧栏也能操作其他会话；只有删除当前会话才离开正在阅读的页面。
            if (currentChat?.params.conversationId === chat.id) navigate('/', { replace: true });
          }}
        />
      )}
    </>
  );
}
