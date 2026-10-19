import { Button } from '../../shared/ui/Button';
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, messageOf, send } from '../../shared/api/api';
import { useWorkspace } from '../../shared/state/workspace';
import { ErrorBox, Loading, PageTitle } from '../../shared/ui/components';
import { clearComposerDraft } from './composerDraft';

type Preview = {
  conversation_id: string;
  title: string;
  state: string;
  version: string;
  eligible: boolean;
  blockers: string[];
  counts: { messages: number; runs: number; snapshots: number; history_records: number };
  preserved: string[];
  assets?: { versions_delete: number; versions_keep: number; drafts_delete: number; drafts_keep: number; ready: boolean };
};
type Deletion = {
  id: string | null;
  target_id: string;
  status: string;
  phase: string;
  last_error: string | null;
};

function blockerText(reason: string) {
  if (reason === 'asset_reference_index_pending') return '正在核对已有内容的引用关系，请稍后重新预览。';
  if (reason.includes('unknown') || reason.includes('ledger_unavailable'))
    return '有执行结果尚未核实，需先完成核对。';
  if (reason.includes('run_cleanup_in_progress'))
    return '该会话的运行详情正在清理，请稍后重新预览。';
  if (reason.includes('unconsumed')) return '有子任务结果尚未交接完成。';
  if (reason.includes('snapshot_used')) return '有其他会话仍引用这里的恢复数据，暂时不能删除。';
  return '仍有未结束、暂停、待回答或待审批的执行，请先处理。';
}

/** 删除是持久后台作业，刷新地址仍能查询；离开页面不取消清理。 */
export function DeleteConversationPage() {
  const { conversationId } = useParams();
  return <ConversationDeletion key={conversationId} conversationId={conversationId!} />;
}

function ConversationDeletion({ conversationId }: { conversationId: string }) {
  const { refresh } = useWorkspace();
  const [preview, setPreview] = useState<Preview | null>(null);
  const [job, setJob] = useState<Deletion | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const base = `/api/conversations/${conversationId}`;
  const status = job?.status;
  useEffect(() => {
    const controller = new AbortController();
    api<Preview>(base + '/deletion-preview', { signal: controller.signal })
      .then(async (value) => {
        const current =
          value.state !== 'active'
            ? await api<Deletion>(base + '/deletion', { signal: controller.signal })
            : null;
        if (!controller.signal.aborted) {
          setPreview(value);
          setJob(current);
          setError('');
        }
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(messageOf(failure));
      });
    return () => controller.abort();
  }, [base, tick]);
  useEffect(() => {
    if (!status || ['completed', 'failed'].includes(status)) return;
    const controller = new AbortController();
    const timer = setInterval(() => {
      api<Deletion>(base + '/deletion', { signal: controller.signal })
        .then((value) => {
          if (controller.signal.aborted) return;
          setJob(value);
          setError('');
          if (value.status === 'completed') refresh();
        })
        .catch((failure) => {
          if (!controller.signal.aborted) setError(messageOf(failure));
        });
    }, 1500);
    return () => {
      clearInterval(timer);
      controller.abort();
    };
  }, [base, status, refresh]);
  async function act(retry = false) {
    if (!preview || busy) return;
    setBusy(true);
    setError('');
    try {
      setJob(
        await send<Deletion>(
          base + (retry ? '/deletion/retry' : ''),
          retry ? undefined : { expected_version: preview.version },
          retry ? 'POST' : 'DELETE',
        ),
      );
      clearComposerDraft(conversationId);
      refresh();
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <PageTitle
        eyebrow="CONVERSATION"
        title={status === 'completed' ? '会话已删除' : '删除会话'}
      />
      <ErrorBox message={error} />
      {!preview && !error && <Loading />}
      {preview && (
        <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
          {job ? (
            <div role="status">
              <h2>
                {status === 'completed'
                  ? '聊天及其上下文已清理'
                  : status === 'failed'
                    ? '清理未完成，可重试'
                    : '正在清理会话'}
              </h2>
              <p>
                {status === 'completed'
                  ? '无依赖的会话材料和临时草稿已清理；独立资产、仍被使用的内容及学习配置已保留。'
                  : '会话已停止读写。后台分步处理，关闭此页面后仍会继续；隐藏会话不代表清理已经完成。'}
              </p>
              {status === 'failed' && (
                <Button disabled={busy} onClick={() => void act(true)}>
                  重试清理
                </Button>
              )}
            </div>
          ) : (
            <>
              <h2>{preview.title}</h2>
              <p>
                将删除 {preview.counts.messages} 条聊天消息及其上下文、历史检索副本和{' '}
                {preview.counts.runs} 次运行的执行详情。删除后不能恢复这段聊天。
              </p>
              <p>
                以下内容会保留：{preview.preserved.join('、')}
                。学习任务将解除会话关联并停止自动推进。
              </p>
              {preview.assets?.ready && <div className="my-4 rounded-lg bg-[#f6f8f3] p-4 text-sm">
                <p>将回收：{preview.assets.versions_delete} 个材料版本、{preview.assets.drafts_delete} 份临时草稿。</p>
                <p>继续保留：{preview.assets.versions_keep} 个材料版本、{preview.assets.drafts_keep} 份草稿。它们有独立保存意图、被其他内容使用，或属于归属待核对的旧数据。</p>
                <Link to={`/sources?conversation_id=${conversationId}`}>查看会话材料并选择独立保存</Link>
              </div>}
              {!preview.eligible && (
                <div
                  className="notice border-l-[3px] border-[#c0a76b] bg-[#f5f0e0] px-4 py-3 text-[13px] [overflow-wrap:anywhere]"
                  role="status"
                >
                  <strong>当前不能删除</strong>
                  <ul>
                    {[...new Set(preview.blockers.map(blockerText))].map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                </div>
              )}
              <div className="row flex items-center gap-3 wrap flex-wrap">
                <Button disabled={busy || !preview.eligible} onClick={() => void act()}>
                  确认删除会话
                </Button>
                <Link className="button secondary button-secondary" to={`/chat/${conversationId}`}>
                  返回对话
                </Link>
              </div>
            </>
          )}
          <div className="row flex items-center gap-3 wrap flex-wrap">
            <Button
              className="quiet button-quiet"
              disabled={busy}
              onClick={() => setTick((n) => n + 1)}
            >
              刷新状态
            </Button>
            {job && <Link to="/">开始新对话 →</Link>}
            {job && <Link to="/learning">查看保留的学习任务 →</Link>}
          </div>
        </section>
      )}
    </>
  );
}
