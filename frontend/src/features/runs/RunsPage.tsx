import { Button } from '../../shared/ui/Button';
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { Activity, ArrowLeft, Download, RotateCcw } from 'lucide-react';
import { messageOf, send, terminal, usePagedResource, useResource } from '../../shared/api/api';
import { RunPauseControl } from './RunPauseControl';
import { Badge, Empty, ErrorBox, Loading, Markdown, PageTitle } from '../../shared/ui/components';
import type { Budgets, ModelRecord, Run, RunEvent } from '../../shared/types/types';
import { useWorkspace } from '../../shared/state/workspace';
import { ExecutionApprovals } from '../permissions/ExecutionApprovals';
import { RunQuestions } from './RunQuestions';
import { RunChildren } from './RunChildren';
import { RuntimeResources } from './RuntimeResources';
import type { RuntimeManifest, Continuation } from './RuntimeResources';

export function RunsPage() {
  const { version } = useWorkspace();
  const result = usePagedResource<Run>('/api/runs/page', version);
  return (
    <>
      <PageTitle eyebrow="REVIEW & TRACE" title="运行记录">
        <Link className="button secondary button-secondary" to="/changes">
          查看知识变更 →
        </Link>
      </PageTitle>
      <p className="intro text-muted">查看每次任务的结果、工具活动、模型用量与停止原因。</p>
      <ErrorBox message={result.error} />
      {result.loading && !result.data.length ? (
        <Loading />
      ) : result.data?.length ? (
        <div className="list-cards grid gap-3">
          {result.data.map((run) => (
            <Link
              className="list-card flex items-center gap-[17px] rounded-[9px] border border-line bg-white px-[23px] py-4.5 text-[#5c7057] hover:border-[#aec2a5] hover:no-underline max-md:flex-wrap max-md:gap-3 max-md:p-4 [&>div]:min-w-0 [&>div]:flex-1 [&_h3]:mt-0 [&_h3]:mb-[5px] [&_h3]:text-[#344c37] [&_h3]:[overflow-wrap:anywhere] max-md:[&_h3]:text-[13px] [&_p]:m-0 [&_p]:text-[11px] [&_p]:text-[#85917e] [&_p]:[overflow-wrap:anywhere] [&>span:last-child]:text-[11px] [&>span:last-child]:whitespace-nowrap max-md:[&>span:last-child]:hidden"
              to={`/runs/${run.id}`}
              key={run.id}
            >
              <Activity size={20} />
              <div>
                <h3>{run.task.slice(0, 90)}</h3>
                <p>
                  {new Date(run.created_at).toLocaleString()} ·{' '}
                  {run.kind === 'material' ? '材料解析' : run.parent_id ? '研究子任务' : '主任务'}
                </p>
              </div>
              <Badge status={run.status} />
              <span>查看 →</span>
            </Link>
          ))}
        </div>
      ) : (
        <Empty title="暂无运行记录">通过对话提交第一个任务后，完整过程会保存在这里。</Empty>
      )}
      {result.next && (
        <Button
          className="secondary button-secondary"
          disabled={result.loading}
          onClick={result.loadMore}
        >
          {result.loading ? '正在加载…' : '加载更多运行'}
        </Button>
      )}
    </>
  );
}

function ResumeControls({ run, onChanged }: { run: Run; onChanged: () => void }) {
  const [limits, setLimits] = useState<Budgets>(run.budgets);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const labels: Record<keyof Budgets, string> = {
    model_requests: '模型请求总上限',
    tool_calls: '工具尝试总上限',
    searches: '搜索总上限',
    active_seconds: '活跃时间总上限（秒）',
  };
  return (
    <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
      <h2>从已保存的进展继续</h2>
      <p className="muted text-muted">
        {run.kind === 'material'
          ? '继续下载或解析材料，此操作不调用大模型。若材料为 PDF，已完成的页面会保留。'
          : '继续使用同一次运行和检查点，已消耗的次数不会清零。已提交的知识变更会保留。'}
      </p>
      <ErrorBox message={error} />
      {run.kind !== 'material' && (
        <div className="budget-inputs grid grid-cols-4 gap-3 max-md:grid-cols-2 [&_label]:text-[11px]">
          {(Object.keys(labels) as (keyof Budgets)[]).map((key) => (
            <label key={key}>
              {labels[key]}
              <input
                type="number"
                min={key === 'searches' ? 0 : 1}
                value={limits[key]}
                onChange={(e) => setLimits({ ...limits, [key]: Number(e.target.value) })}
              />
            </label>
          ))}
        </div>
      )}
      <Button
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError('');
          try {
            await send(`/api/runs/${run.id}/resume`, { budgets: limits });
            onChanged();
          } catch (error) {
            setError(messageOf(error));
          } finally {
            setBusy(false);
          }
        }}
      >
        <RotateCcw size={16} />
        确认恢复任务
      </Button>
    </section>
  );
}

export function RunPage() {
  const { runId } = useParams();
  const { version, refresh } = useWorkspace();
  const [tick, setTick] = useState(0);
  const [eventPosition, setEventPosition] = useState({ runId, after: 0 });
  const eventAfter = eventPosition.runId === runId ? eventPosition.after : 0;
  const key = `${version}:${tick}`;
  const result = useResource<Run>(`/api/runs/${runId}`, key);
  // 状态和记录是独立请求。状态先进入终态时，再读取对应事件版本，
  // 避免轮询停止后仍保留执行前的空回执；普通轮询只刷新发生变化的记录。
  const recordsKey = `${version}:${result.data?.status ?? ''}:${result.data?.last_event ?? ''}:${result.data?.details_available ?? ''}`;
  const records = useResource<{
    models: ModelRecord[];
    events: RunEvent[];
    event_page: { after: number; next_after: number | null; total: number };
    artifacts: { id: string; kind: string; length: number; available?: boolean }[];
    resources?: RuntimeManifest | null;
    continuation?: Continuation | null;
  }>(`/api/runs/${runId}/records?after=${eventAfter}`, recordsKey);
  const [error, setError] = useState('');
  const status = result.data?.status;
  useEffect(() => {
    if (
      !status ||
      terminal(status) ||
      status === 'paused' ||
      result.data?.details_available === false
    )
      return;
    const timer = setInterval(() => setTick((n) => n + 1), 2000);
    return () => clearInterval(timer);
  }, [status, runId, result.data?.details_available]);
  if (result.loading && result.data?.id !== runId) return <Loading />;
  if (!result.data) return <ErrorBox message={result.error} />;
  const run = result.data;
  if (run.details_available === false)
    return (
      <>
        <Link className="back-link mb-[15px] inline-flex items-center gap-1.5 text-xs" to="/runs">
          ← 全部运行
        </Link>
        <PageTitle eyebrow="RUN" title="运行结果摘要" />
        <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
          <h2>{run.task}</h2>
          <Badge status={run.status} />
          <p className="notice border-l-[3px] border-[#c0a76b] bg-[#f5f0e0] px-4 py-3 text-[13px] [overflow-wrap:anywhere]">
            完整执行详情已过期或正在清理，无法原地恢复或导出完整轨迹。下方保留简短结果，聊天和独立文档仍可继续使用。
          </p>
          {run.details_expires_at && (
            <p className="muted text-muted">
              详情保留至：{new Date(run.details_expires_at).toLocaleString()}
            </p>
          )}
          {run.outcome.answer && <Markdown text={run.outcome.answer} />}
          {run.conversation_id && <Link to={`/chat/${run.conversation_id}`}>回到对话 →</Link>}
          {(run.outcome.draft_ids ?? []).map((id) => (
            <p key={id}>
              <Link to={`/changes/${id}`}>查看保留的草稿 →</Link>
            </p>
          ))}
        </section>
        <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
          <h2>内部材料</h2>
          <ErrorBox message={records.error} />
          {records.loading && !records.data && <Loading />}
          {records.data?.artifacts?.map((artifact) => (
            <p key={artifact.id}>
              {artifact.available === false ? (
                <span>{artifact.kind}：正文已过期</span>
              ) : (
                <Link to={`/runs/${run.id}/artifacts/${artifact.id}`}>
                  {artifact.kind}：查看保留内容 →
                </Link>
              )}
            </p>
          ))}
        </section>
      </>
    );
  return (
    <>
      <Link className="back-link mb-[15px] inline-flex items-center gap-1.5 text-xs" to="/runs">
        <ArrowLeft size={15} />
        全部运行
      </Link>
      <PageTitle eyebrow="RUN / OBSERVABILITY" title="运行详情">
        <div className="row flex items-center gap-3">
          <Button className="secondary button-secondary" onClick={refresh}>
            刷新
          </Button>
          <a className="button secondary button-secondary" href={`/api/runs/${run.id}/export`}>
            <Download size={15} />
            导出记录
          </a>
        </div>
      </PageTitle>
      <ErrorBox message={error || records.error} />
      <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
        <div className="row flex items-center gap-3 wrap flex-wrap">
          <Badge status={run.status} />
          {run.outcome.kind && <Badge status={run.outcome.kind} />}
          {run.conversation_id && <Link to={`/chat/${run.conversation_id}`}>回到对话 →</Link>}
          {run.outcome.source_id && (
            <Link to={`/sources/${run.outcome.source_id}`}>查看来源材料 →</Link>
          )}
        </div>
        <h2 className="run-task mt-5.5 text-[17px] whitespace-pre-wrap">{run.task}</h2>
        {run.kind === 'material' ? (
          <p className="muted text-muted">
            材料下载与解析不调用大模型 · 已运行 {(run.active_ms / 1000).toFixed(1)} 秒
          </p>
        ) : (
          <div className="metrics my-[25px] grid grid-cols-4 gap-4.5 max-md:grid-cols-2 [&>div]:border-l-2 [&>div]:border-[#d7e4cd] [&>div]:pl-3.5 [&_strong]:block [&_strong]:text-[26px] [&_strong]:font-semibold [&_strong]:text-[#345332] [&_strong_small]:text-[11px] [&_strong_small]:font-normal [&_strong_small]:text-[#8c9e83] [&_span]:text-[11px] [&_span]:text-[#8a9b81]">
            <div>
              <strong>
                {run.model_usage?.root_used ?? '—'}
                <small> / {run.budgets.model_requests}</small>
              </strong>
              <span>模型请求</span>
            </div>
            <div>
              <strong>
                {run.tool_calls}
                <small> / {run.budgets.tool_calls}</small>
              </strong>
              <span>工具尝试</span>
            </div>
            <div>
              <strong>
                {run.searches}
                <small> / {run.budgets.searches}</small>
              </strong>
              <span>搜索调用</span>
            </div>
            <div>
              <strong>
                {(run.active_ms / 1000).toFixed(1)}
                <small> 秒</small>
              </strong>
              <span>活跃执行时间</span>
            </div>
          </div>
        )}
        {run.stop_reason && (
          <p className="notice border-l-[3px] border-[#c0a76b] bg-[#f5f0e0] px-4 py-3 text-[13px] [overflow-wrap:anywhere]">
            停止原因：{run.stop_reason}
          </p>
        )}
        <RunPauseControl run={run} onChanged={refresh} />
        {!terminal(run.status) && (
          <Button
            className="secondary button-secondary"
            onClick={async () => {
              try {
                await send(`/api/runs/${run.id}/cancel`);
                refresh();
              } catch (error) {
                setError(messageOf(error));
              }
            }}
          >
            停止本次任务
          </Button>
        )}
        <details className="mt-5">
          <summary>运行标识与检查点</summary>
          <dl className="metadata my-5 grid gap-2.5 text-xs [&>div]:flex [&>div]:gap-5 max-md:[&>div]:gap-2.5 [&_dt]:min-w-[90px] [&_dt]:text-[#87937e] max-md:[&_dt]:min-w-[60px] [&_dd]:m-0 [&_dd]:[overflow-wrap:anywhere]">
            <div>
              <dt>运行 ID</dt>
              <dd>{run.id}</dd>
            </div>
            <div>
              <dt>根任务 ID</dt>
              <dd>{run.root_id}</dd>
            </div>
            <div>
              <dt>检查点</dt>
              <dd>{run.checkpoint_id}</dd>
            </div>
            <div>
              <dt>调度代次</dt>
              <dd>{run.generation}</dd>
            </div>
          </dl>
        </details>
      </section>
      <ExecutionApprovals run={run} refreshKey={key} onChanged={refresh} />
      <RunQuestions run={run} refreshKey={key} onChanged={refresh} />
      <RunChildren run={run} refreshKey={key} onChanged={refresh} />
      {records.data ? (
        <RuntimeResources
          resources={records.data.resources}
          continuation={records.data.continuation}
        />
      ) : (
        <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
          <h2>本次运行依据</h2>
          <p className="muted text-muted">
            {records.error ? '运行依据暂不可用。' : '正在加载运行依据…'}
          </p>
        </section>
      )}
      {terminal(run.status) &&
        run.status !== 'completed' &&
        !run.parent_id &&
        !['waiting_approval', 'execution_unknown'].includes(run.outcome.kind ?? '') && (
          <ResumeControls key={`${run.id}:${run.generation}`} run={run} onChanged={refresh} />
        )}
      {run.outcome.answer && (
        <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
          <h2>本次结果</h2>
          <Markdown text={run.outcome.answer} />
        </section>
      )}
      {(run.outcome.draft_ids ?? []).map((id) => (
        <Link
          key={id}
          className="draft-link my-3.5 flex items-center gap-[15px] rounded-lg border border-[#d8e1d0] bg-[#f8faf1] px-5 py-4.5 text-[#52673f] [&>span:nth-child(2)]:flex-1 [&_strong]:block [&_small]:mt-1 [&_small]:block [&_small]:text-[11px] [&_small]:text-[#879375] [&>span:last-child]:text-xs"
          to={`/changes/${id}`}
        >
          打开待审核草稿 →
        </Link>
      ))}
      <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
        <h2>
          模型调用{' '}
          <span className="muted text-muted">
            {records.loading
              ? records.data
                ? '更新中…'
                : '加载中…'
              : records.data
                ? `${records.data.models.length} 次`
                : '记录暂不可用'}
          </span>
        </h2>
        {records.data?.models.map((record, index) => (
          <details
            className="trace-entry mt-[13px] border-t border-[#e5eddd] pt-[13px] [&>summary]:flex [&>summary]:flex-wrap [&>summary]:justify-between [&>summary]:gap-2.5 max-md:[&>summary]:text-[11px] [&_pre]:max-h-[420px] [&_pre]:overflow-auto"
            key={record.id}
          >
            <summary>
              <span>
                #{index + 1} · {record.purpose}
              </span>
              <span>
                {record.usage?.total_tokens ?? '未知'} tokens ·{' '}
                {record.duration_ms == null
                  ? '耗时未知'
                  : `${(record.duration_ms / 1000).toFixed(1)}s`}{' '}
                · {record.status}
              </span>
            </summary>
            <p className="muted text-muted">
              首响应：
              {record.first_token_ms == null ? '未观测到' : `${record.first_token_ms.toFixed(0)}ms`}
            </p>
            {record.error && <ErrorBox message={record.error} />}
            <h3>实际输入</h3>
            <pre>{JSON.stringify(record.request, null, 2)}</pre>
            <h3>实际输出</h3>
            <pre>{JSON.stringify(record.response, null, 2)}</pre>
          </details>
        ))}
      </section>
      <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
        <h2>保留的运行材料</h2>
        <p className="muted text-muted">
          这里保存压缩前历史、工作摘要和长工具结果；它们不属于已发布知识。
        </p>
        {records.data?.artifacts?.map((artifact) => (
          <Link
            className="list-card flex items-center gap-[17px] rounded-[9px] border border-line bg-white px-[23px] py-4.5 text-[#5c7057] hover:border-[#aec2a5] hover:no-underline max-md:flex-wrap max-md:gap-3 max-md:p-4 [&>div]:min-w-0 [&>div]:flex-1 [&_h3]:mt-0 [&_h3]:mb-[5px] [&_h3]:text-[#344c37] [&_h3]:[overflow-wrap:anywhere] max-md:[&_h3]:text-[13px] [&_p]:m-0 [&_p]:text-[11px] [&_p]:text-[#85917e] [&_p]:[overflow-wrap:anywhere] [&>span:last-child]:text-[11px] [&>span:last-child]:whitespace-nowrap max-md:[&>span:last-child]:hidden"
            key={artifact.id}
            to={`/runs/${run.id}/artifacts/${artifact.id}`}
          >
            <span>{artifact.kind}</span>
            <span>{artifact.length} 字符 · 分页查看 →</span>
          </Link>
        ))}
        {records.data && !records.data.artifacts?.length && (
          <p className="muted text-muted">此运行暂未产生独立材料。</p>
        )}
      </section>
      <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
        <h2>事件时间线</h2>
        <p className="muted text-muted">
          {records.data
            ? `事件 ${records.data.events[0]?.sequence ?? 0}–${records.data.events.at(-1)?.sequence ?? 0} / ${records.data.event_page.total}`
            : records.error
              ? '事件记录暂不可用。'
              : '正在加载事件记录…'}
        </p>
        <div className="row flex items-center gap-3">
          <Button
            className="secondary button-secondary"
            disabled={records.loading || eventAfter === 0}
            onClick={() => setEventPosition({ runId, after: Math.max(0, eventAfter - 200) })}
          >
            上一页事件
          </Button>
          <Button
            className="secondary button-secondary"
            disabled={records.loading || records.data?.event_page.next_after == null}
            onClick={() => setEventPosition({ runId, after: records.data!.event_page.next_after! })}
          >
            下一页事件
          </Button>
        </div>
        {records.data?.events.map((event) => (
          <details
            className="trace-entry mt-[13px] border-t border-[#e5eddd] pt-[13px] [&>summary]:flex [&>summary]:flex-wrap [&>summary]:justify-between [&>summary]:gap-2.5 max-md:[&>summary]:text-[11px] [&_pre]:max-h-[420px] [&_pre]:overflow-auto"
            key={event.sequence}
          >
            <summary>
              <span>
                #{event.sequence} · {event.kind}
              </span>
              <time>{new Date(event.created_at).toLocaleTimeString()}</time>
            </summary>
            <pre>{JSON.stringify(event.data, null, 2)}</pre>
          </details>
        ))}
      </section>
    </>
  );
}

export function RunArtifactPage() {
  const { runId, artifactId } = useParams();
  const [start, setStart] = useState(0);
  const result = useResource<{
    text: string;
    kind: string;
    start: number;
    end: number;
    total_length: number;
    next_start: number | null;
  }>(`/api/runs/${runId}/artifacts/${artifactId}?start=${start}`);
  return (
    <>
      <Link to={`/runs/${runId}`}>← 返回运行详情</Link>
      <PageTitle eyebrow="PRESERVED CONTEXT" title="运行材料原文" />
      <ErrorBox message={result.error} />
      {result.loading ? (
        <Loading />
      ) : (
        result.data && (
          <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
            <p>
              {result.data.kind} · 字符 {result.data.start}–{result.data.end} /{' '}
              {result.data.total_length}
            </p>
            <pre className="source-text">{result.data.text}</pre>
            <div className="row flex items-center gap-3">
              <Button
                className="secondary button-secondary"
                disabled={start === 0}
                onClick={() => setStart(Math.max(0, start - 6000))}
              >
                上一段
              </Button>
              <Button
                disabled={result.data.next_start === null}
                onClick={() => setStart(result.data?.next_start ?? start)}
              >
                下一段
              </Button>
            </div>
          </section>
        )
      )}
    </>
  );
}
