import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { Activity, ArrowLeft, Download, RotateCcw } from 'lucide-react';
import { messageOf, send, terminal, usePagedResource, useResource } from './api';
import { Badge, Empty, ErrorBox, Loading, Markdown, PageTitle } from './components';
import type { Budgets, ModelRecord, Run, RunEvent } from './types';
import { useWorkspace } from './workspace';
import { MCPApprovals } from './MCPPage';
import { RuntimeResources } from './RuntimeResources';
import type { RuntimeManifest, Continuation } from './RuntimeResources';

export function RunsPage() {
  const { version } = useWorkspace();
  const result = usePagedResource<Run>('/api/runs/page', version);
  return (
    <>
      <PageTitle eyebrow="REVIEW & TRACE" title="运行记录">
        <Link className="button secondary" to="/changes">
          查看知识变更 →
        </Link>
      </PageTitle>
      <p className="intro">查看每次任务的结果、工具活动、模型用量与停止原因。</p>
      <ErrorBox message={result.error} />
      {result.loading && !result.data.length ? (
        <Loading />
      ) : result.data?.length ? (
        <div className="list-cards">
          {result.data.map((run) => (
            <Link className="list-card" to={`/runs/${run.id}`} key={run.id}>
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
        <button className="secondary" disabled={result.loading} onClick={result.loadMore}>
          {result.loading ? '正在加载…' : '加载更多运行'}
        </button>
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
    <section className="card">
      <h2>从已保存的进展继续</h2>
      <p className="muted">
        {run.kind === 'material'
          ? '继续解析材料，PDF 已完成的页面会保留。此操作不调用大模型。'
          : '继续使用同一次运行和检查点，已消耗的次数不会清零。已提交的知识变更会保留。'}
      </p>
      <ErrorBox message={error} />
      {run.kind !== 'material' && (
        <div className="budget-inputs">
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
      <button
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
      </button>
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
  const records = useResource<{
    models: ModelRecord[];
    events: RunEvent[];
    event_page: { after: number; next_after: number | null; total: number };
    artifacts: { id: string; kind: string; length: number }[];
    resources?: RuntimeManifest | null;
    continuation?: Continuation | null;
  }>(`/api/runs/${runId}/records?after=${eventAfter}`, key);
  const children = useResource<Run[]>(
    result.data ? `/api/runs?root_id=${result.data.root_id}` : null,
    key,
  );
  const [error, setError] = useState('');
  const status = result.data?.status;
  useEffect(() => {
    if (!status || terminal(status)) return;
    const timer = setInterval(() => setTick((n) => n + 1), 2000);
    return () => clearInterval(timer);
  }, [status, runId]);
  if (result.loading && result.data?.id !== runId) return <Loading />;
  if (!result.data) return <ErrorBox message={result.error} />;
  const run = result.data;
  return (
    <>
      <Link className="back-link" to="/runs">
        <ArrowLeft size={15} />
        全部运行
      </Link>
      <PageTitle eyebrow="RUN / OBSERVABILITY" title="运行详情">
        <div className="row">
          <button className="secondary" onClick={refresh}>
            刷新
          </button>
          <a className="button secondary" href={`/api/runs/${run.id}/export`}>
            <Download size={15} />
            导出记录
          </a>
        </div>
      </PageTitle>
      <ErrorBox message={error || records.error} />
      <section className="card">
        <div className="row wrap">
          <Badge status={run.status} />
          {run.outcome.kind && <Badge status={run.outcome.kind} />}
          {run.conversation_id && <Link to={`/chat/${run.conversation_id}`}>回到对话 →</Link>}
          {run.outcome.source_id && (
            <Link to={`/sources/${run.outcome.source_id}`}>查看来源材料 →</Link>
          )}
        </div>
        <h2 className="run-task">{run.task}</h2>
        {run.kind === 'material' ? (
          <p className="muted">
            材料下载与解析不调用大模型 · 已运行 {(run.active_ms / 1000).toFixed(1)} 秒
          </p>
        ) : (
          <div className="metrics">
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
        {run.stop_reason && <p className="notice">停止原因：{run.stop_reason}</p>}
        {!terminal(run.status) && (
          <button
            className="secondary"
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
          </button>
        )}
        <details>
          <summary>运行标识与检查点</summary>
          <dl className="metadata">
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
      <MCPApprovals rootId={run.root_id} refreshKey={key} onChanged={refresh} />
      <RuntimeResources
        resources={records.data?.resources}
        continuation={records.data?.continuation}
      />
      {terminal(run.status) && run.status !== 'completed' && !run.parent_id && (
        <ResumeControls key={`${run.id}:${run.generation}`} run={run} onChanged={refresh} />
      )}
      {run.outcome.answer && (
        <section className="card">
          <h2>本次结果</h2>
          <Markdown text={run.outcome.answer} />
        </section>
      )}
      {(run.outcome.draft_ids ?? []).map((id) => (
        <Link key={id} className="draft-link" to={`/changes/${id}`}>
          打开待审核草稿 →
        </Link>
      ))}
      {Boolean(children.data?.some((child) => child.parent_id)) && (
        <section className="card">
          <h2>研究子任务</h2>
          {children.data
            ?.filter((child) => child.parent_id)
            .map((child) => (
              <Link className="list-card" to={`/runs/${child.id}`} key={child.id}>
                <span>{child.task}</span>
                <Badge status={child.status} />
              </Link>
            ))}
        </section>
      )}
      <section className="card">
        <h2>
          模型调用 <span className="muted">{records.data?.models.length ?? 0} 次</span>
        </h2>
        {records.data?.models.map((record, index) => (
          <details className="trace-entry" key={record.id}>
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
            <p className="muted">
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
      <section className="card">
        <h2>保留的运行材料</h2>
        <p className="muted">这里保存压缩前历史、工作摘要和长工具结果；它们不属于已发布知识。</p>
        {records.data?.artifacts?.map((artifact) => (
          <Link
            className="list-card"
            key={artifact.id}
            to={`/runs/${run.id}/artifacts/${artifact.id}`}
          >
            <span>{artifact.kind}</span>
            <span>{artifact.length} 字符 · 分页查看 →</span>
          </Link>
        ))}
        {!records.data?.artifacts?.length && <p className="muted">此运行暂未产生独立材料。</p>}
      </section>
      <section className="card">
        <h2>事件时间线</h2>
        <p className="muted">
          事件 {records.data?.events[0]?.sequence ?? 0}–{records.data?.events.at(-1)?.sequence ?? 0}{' '}
          / {records.data?.event_page.total ?? 0}
        </p>
        <div className="row">
          <button
            className="secondary"
            disabled={records.loading || eventAfter === 0}
            onClick={() => setEventPosition({ runId, after: Math.max(0, eventAfter - 200) })}
          >
            上一页事件
          </button>
          <button
            className="secondary"
            disabled={records.loading || records.data?.event_page.next_after == null}
            onClick={() => setEventPosition({ runId, after: records.data!.event_page.next_after! })}
          >
            下一页事件
          </button>
        </div>
        {records.data?.events.map((event) => (
          <details className="trace-entry" key={event.sequence}>
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
          <section className="card">
            <p>
              {result.data.kind} · 字符 {result.data.start}–{result.data.end} /{' '}
              {result.data.total_length}
            </p>
            <pre className="source-text">{result.data.text}</pre>
            <div className="row">
              <button
                className="secondary"
                disabled={start === 0}
                onClick={() => setStart(Math.max(0, start - 6000))}
              >
                上一段
              </button>
              <button
                disabled={result.data.next_start === null}
                onClick={() => setStart(result.data?.next_start ?? start)}
              >
                下一段
              </button>
            </div>
          </section>
        )
      )}
    </>
  );
}
