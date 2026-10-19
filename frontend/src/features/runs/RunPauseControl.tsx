import { Button } from '../../shared/ui/Button';
import { useState } from 'react';
import { Pause, Play } from 'lucide-react';
import { messageOf, send } from '../../shared/api/api';
import type { Run } from '../../shared/types/types';
import { ErrorBox } from '../../shared/ui/components';

/** 聊天和详情共用持久暂停入口；关闭页面不会改变运行状态。 */
export function RunPauseControl({ run, onChanged }: { run: Run; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  if (
    run.parent_id ||
    !['agent', 'memory_classification'].includes(run.kind) ||
    !['queued', 'running', 'paused', 'waiting_input', 'waiting_children', 'interrupted'].includes(
      run.status,
    )
  )
    return null;
  const paused = run.status === 'paused';
  const pending = !paused && run.pause_requested;
  return (
    <div>
      <Button
        className="quiet button-quiet small text-xs"
        disabled={busy || pending || run.cancel_requested}
        onClick={async () => {
          setBusy(true);
          setError('');
          try {
            await send(
              `/api/runs/${run.id}/${paused ? 'resume' : 'pause'}`,
              paused ? { budgets: {} } : {},
            );
            onChanged();
          } catch (error) {
            setError(messageOf(error));
          } finally {
            setBusy(false);
          }
        }}
      >
        {paused ? <Play size={12} /> : <Pause size={12} />}
        {pending ? '正在收尾暂停…' : paused ? '继续任务' : '暂停任务'}
      </Button>
      <ErrorBox message={error} />
    </div>
  );
}
