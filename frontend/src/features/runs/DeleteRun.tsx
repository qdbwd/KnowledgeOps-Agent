import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Trash2 } from 'lucide-react';
import type { Run } from '../../shared/types/types';
import { Button } from '../../shared/ui/Button';
import { AssetDeletionDialog, AssetDeletionHistory } from '../sources/AssetDeletion';

export function DeleteRunButton({ run, leave = false }: { run: Run; leave?: boolean }) {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  if (run.parent_id || run.user_deleted_at) return null;
  return (
    <>
      <Button
        className="button-quiet text-danger"
        aria-label={`删除运行：${run.task.slice(0, 90)}`}
        onClick={() => setOpen(true)}
      >
        <Trash2 size={15} />
        删除记录
      </Button>
      {open && (
        <AssetDeletionDialog
          kind="run"
          identity={run.id}
          title={run.task}
          onClose={() => setOpen(false)}
          onDeleted={leave ? () => navigate('/runs') : undefined}
        />
      )}
    </>
  );
}

export function RemovedRun({ run }: { run: Run }) {
  return (
    <section className="rounded-xl border border-line bg-white p-6">
      <h1 className="text-xl">运行记录已删除</h1>
      <p className="text-sm text-muted">
        执行详情已从日常记录中移除。聊天、已发布文章和独立交付文件仍可在各自页面查看。
      </p>
      <div className="flex flex-wrap gap-4 text-sm">
        <Link to="/runs">返回运行记录</Link>
        {run.conversation_id && <Link to={`/chat/${run.conversation_id}`}>回到对话</Link>}
        <Link to="/knowledge">查看知识库</Link>
        <Link to="/sources">查看材料与交付文件</Link>
      </div>
      <AssetDeletionHistory kind="run" />
    </section>
  );
}
