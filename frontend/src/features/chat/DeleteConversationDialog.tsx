import { useState } from 'react';
import { Link } from 'react-router-dom';
import { messageOf, send, useResource } from '../../shared/api/api';
import { useWorkspace } from '../../shared/state/workspace';
import { Button } from '../../shared/ui/Button';
import { Dialog } from '../../shared/ui/Dialog';
import { ErrorBox, Loading } from '../../shared/ui/components';
import { useBackgroundDeletions } from './BackgroundDeletions';
import { removeConversationDraft } from './drafts/session';

type Preview = {
  title: string;
  state: string;
  version: string;
  eligible: boolean;
  blockers: string[];
  counts: { messages: number; runs: number };
  preserved: string[];
  assets?: {
    versions_delete: number;
    versions_keep: number;
    drafts_delete: number;
    drafts_keep: number;
    ready: boolean;
  };
};
function blockerText(reason: string) {
  if (reason === 'asset_reference_index_pending')
    return '正在核对已有内容的引用关系，请稍后重新预览。';
  if (reason.includes('unknown') || reason.includes('ledger_unavailable'))
    return '有执行结果尚未核实，需先完成核对。';
  if (reason.includes('run_cleanup_in_progress'))
    return '该会话的运行详情正在清理，请稍后重新预览。';
  if (reason.includes('unconsumed')) return '有子任务结果尚未交接完成。';
  if (reason.includes('snapshot_used')) return '有其他会话仍引用这里的恢复数据，暂时不能删除。';
  return '仍有未结束、暂停、待回答或待审批的执行，请先处理。';
}

/** 保留删除预览和并发校验；接受后由入口处理导航，清理由已有持久作业完成。 */
export function DeleteConversationDialog({
  conversationId,
  onClose,
  onDeleted,
}: {
  conversationId: string;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const { refresh } = useWorkspace();
  const { track } = useBackgroundDeletions();
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const base = `/api/conversations/${conversationId}`;
  const result = useResource<Preview>(base + '/deletion-preview', tick);
  const preview = result.data;
  return (
    <Dialog
      title="删除会话"
      busy={busy}
      onClose={onClose}
      footer={
        <>
          <Button className="secondary button-secondary" disabled={busy} onClick={onClose}>
            取消
          </Button>
          <Button
            disabled={busy || result.loading || !preview?.eligible || preview.state !== 'active'}
            onClick={async () => {
              if (!preview || busy) return;
              setBusy(true);
              setError('');
              try {
                const job = await send<{ status: string }>(
                  base,
                  { expected_version: preview.version },
                  'DELETE',
                );
                if (job.status !== 'completed') track(conversationId);
                // 删除已被接受；本地存储故障不能冒充服务端删除失败。
                // 仍打开的会话会在聚焦/再次读取时核对 404 并清除本地旧输入。
                await removeConversationDraft(conversationId).catch(() => {});
                refresh();
                onDeleted();
              } catch (failure) {
                setError(messageOf(failure));
              } finally {
                setBusy(false);
              }
            }}
          >
            确认删除会话
          </Button>
        </>
      }
    >
      <ErrorBox message={error || result.error} />
      {result.loading && <Loading />}
      {preview && (
        <>
          <p className="font-semibold [overflow-wrap:anywhere]">{preview.title}</p>
          <p>
            将删除 {preview.counts.messages} 条聊天消息及其上下文和 {preview.counts.runs}{' '}
            次运行的执行详情。删除后不能恢复这段聊天。
          </p>
          <p>
            以下内容会保留：{preview.preserved.join('、')}。学习任务将解除会话关联并停止自动推进。
          </p>
          {preview.assets?.ready && (
            <div className="my-4 rounded-lg bg-canvas p-4 text-sm">
              <p>
                将回收：{preview.assets.versions_delete} 个材料版本、{preview.assets.drafts_delete}{' '}
                份临时草稿。
              </p>
              <p>
                继续保留：{preview.assets.versions_keep} 个材料版本、{preview.assets.drafts_keep}{' '}
                份草稿。它们有独立保存意图、仍被引用，或属于归属待核对的旧数据。
              </p>
              <Link to={`/sources?conversation_id=${conversationId}`} onClick={onClose}>
                查看会话材料并选择独立保存
              </Link>
            </div>
          )}
          {!preview.eligible && (
            <div role="status">
              <strong>当前不能删除</strong>
              <ul>
                {[...new Set(preview.blockers.map(blockerText))].map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
      {(error || result.error || (preview && !preview.eligible)) && (
        <Button
          className="quiet button-quiet"
          disabled={busy}
          onClick={() => {
            setError('');
            setTick((n) => n + 1);
          }}
        >
          重新预览
        </Button>
      )}
    </Dialog>
  );
}
