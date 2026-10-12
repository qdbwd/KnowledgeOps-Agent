import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { ArrowUp, FileText, Sparkles, Square, Activity } from 'lucide-react';
import { api, messageOf, send, terminal, usePagedResource, useResource } from './api';
import type { CollectionPage } from './api';
import { Badge, Empty, ErrorBox, Loading, Markdown, PageTitle } from './components';
import type { ChangeSet, Conversation, Guidance, Message, Run, RunEvent, Source } from './types';
import { useWorkspace } from './workspace';
import { MCPApprovals } from './MCPPage';
import { ContextUsage } from './ContextUsage';
import { ConversationLearning } from './LearningPage';

/** 一个任务的流式观察，不承担运行生命周期；组件卸载只关闭连接，不取消 Agent。 */
function LiveRun({ initial, onChanged }: { initial: Run; onChanged: () => void }) {
  const [run, setRun] = useState(initial);
  const [activity, setActivity] = useState('等待执行');
  const [partial, setPartial] = useState('');
  const [connection, setConnection] = useState('');
  const [error, setError] = useState('');
  const [guidance, setGuidance] = useState<Guidance[]>([]);
  useEffect(() => {
    const source = new EventSource(`/api/runs/${initial.id}/events`);
    source.addEventListener('progress', (message: MessageEvent) => {
      const event: RunEvent = JSON.parse(message.data);
      setConnection('');
      if (event.kind === 'model_started') {
        setActivity('正在阅读与决策');
        setPartial('');
      }
      if (event.kind === 'model_delta')
        setPartial((value) => value + String(event.data.text ?? ''));
      if (event.kind === 'tool_started') setActivity(`正在执行 · ${event.data.name}`);
      if (event.kind === 'guidance_pending') setActivity('已收到引导，等待安全点处理');
      if (event.kind === 'guidance_applied') {
        setActivity('已应用新指令');
        setPartial('');
      }
      if (event.kind === 'finished') onChanged();
    });
    source.addEventListener('closed', () => {
      source.close();
      onChanged();
    });
    source.onerror = () => setConnection('连接暂时断开，正在重连；后台任务仍可继续。');
    const timer = window.setInterval(() => {
      Promise.all([
        api<Run>(`/api/runs/${initial.id}`),
        api<Guidance[]>(`/api/runs/${initial.id}/guidance`),
      ])
        .then(([value, deliveries]) => {
          setRun(value);
          setGuidance(deliveries);
          if (terminal(value.status)) {
            source.close();
            onChanged();
          }
        })
        .catch((error: Error) => setError(error.message));
    }, 2000);
    return () => {
      source.close();
      window.clearInterval(timer);
    };
  }, [initial.id, onChanged]);
  return (
    <div className="live-run card">
      <div className="row between">
        <span className="row">
          <Activity size={17} />
          <strong>{activity}</strong>
          <Badge status={run.status} />
        </span>
        <button
          className="quiet small"
          disabled={run.cancel_requested}
          onClick={async () => {
            try {
              setRun(await send<Run>(`/api/runs/${run.id}/cancel`));
            } catch (error) {
              setError(messageOf(error));
            }
          }}
        >
          <Square size={12} />
          {run.cancel_requested ? '正在停止…' : '停止'}
        </button>
      </div>
      <p className="muted small">
        {run.tool_calls} 次工具尝试 · <Link to={`/runs/${run.id}`}>查看运行详情</Link>
      </p>
      {partial && <div className="stream-preview">{partial}</div>}
      {connection && <p className="muted small">{connection}</p>}
      {guidance.map((item) => (
        <p key={item.id} className="muted small">
          引导消息 {item.user_sequence} ·{' '}
          {{ pending: '等待应用', applied: '已应用', stale: '未应用，目标已变化' }[item.status]}
          {item.result.reason || item.result.authorization_error}
        </p>
      ))}
      <ErrorBox message={error} />
    </div>
  );
}

export function ChatPage() {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const { version, refresh } = useWorkspace();
  const messages = useResource<Message[]>(
    conversationId ? `/api/conversations/${conversationId}/messages` : null,
    version,
  );
  const runs = usePagedResource<Run>(
    conversationId ? `/api/runs/page?conversation_id=${conversationId}&roots_only=true` : null,
    version,
  );
  const running = useResource<CollectionPage<Run>>(
    conversationId
      ? `/api/runs/page?conversation_id=${conversationId}&roots_only=true&status=running`
      : null,
    version,
  );
  const materials = useResource<Source[]>('/api/sources', version);
  const drafts = usePagedResource<ChangeSet>(
    conversationId ? `/api/changesets/page?conversation_id=${conversationId}` : null,
    version,
  );
  const [content, setContent] = useState('');
  const [mode, setMode] = useState<'ask' | 'preview' | 'direct'>('ask');
  const [knowledgeOnly, setKnowledgeOnly] = useState(false);
  const [delegation, setDelegation] = useState(true);
  const [auxiliary, setAuxiliary] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  // 网络失败重试使用同一个 key；只有成功提交或输入变化后才换 key。
  const requestKey = useRef({ content: '', id: '' });
  const onChanged = useCallback(() => refresh(), [refresh]);
  async function submit() {
    if (!content.trim() || busy) return;
    setBusy(true);
    setError('');
    try {
      let id = conversationId;
      if (!id) {
        id = (
          await send<Conversation>('/api/conversations', { title: content.trim().slice(0, 30) })
        ).id;
        navigate(`/chat/${id}`, { replace: true });
      }
      const fingerprint = JSON.stringify([
        id,
        content,
        mode,
        knowledgeOnly,
        delegation,
        auxiliary,
        selected,
      ]);
      if (requestKey.current.content !== fingerprint)
        requestKey.current = { content: fingerprint, id: crypto.randomUUID() };
      await send(`/api/conversations/${id}/messages`, {
        content,
        request_key: requestKey.current.id,
        publication_mode: mode,
        knowledge_only: knowledgeOnly,
        delegation,
        auxiliary,
        source_ids: selected,
      });
      // 服务已确认接受；下一次相同文字也是新意图。异常分支保留原 key，
      // 以便网络断开后重试不会在服务端生成重复任务。
      requestKey.current = { content: '', id: '' };
      setContent('');
      setSelected([]);
      refresh();
    } catch (error) {
      setError(messageOf(error));
    } finally {
      setBusy(false);
    }
  }
  const active = running.data?.items?.[0];
  const hasQueued = runs.data.some((run) => run.status === 'queued');
  useEffect(() => {
    // 提交返回时任务通常还在排队，尚无 LiveRun/SSE。独立观察这段间隙，
    // 包括任务很快完成、两次轮询之间没有看到 running 的情况。
    if (!conversationId || !hasQueued || active) return;
    const timer = window.setInterval(onChanged, 2000);
    return () => window.clearInterval(timer);
  }, [conversationId, hasQueued, active, onChanged]);
  const pending =
    drafts.data?.filter((draft) => ['draft', 'approved'].includes(draft.status)) ?? [];
  return (
    <div className="chat-page">
      <PageTitle
        eyebrow="THINK · RESEARCH · REMEMBER"
        title={conversationId ? '继续这段思考' : '把探索变成可积累的知识'}
      >
        {conversationId ? (
          <ContextUsage
            conversationId={conversationId}
            active={Boolean(active) || hasQueued}
            version={version}
          />
        ) : (
          <span className="workspace-label">KnowledgeOps 工作台</span>
        )}
      </PageTitle>
      {conversationId && <ConversationLearning conversationId={conversationId} />}
      <div className="conversation">
        {!conversationId && (
          <div className="welcome">
            <div className="welcome-mark">
              <Sparkles size={28} />
            </div>
            <h2>从一个问题，或一份材料开始。</h2>
            <p>一起研究、整理和维护知识。每一次结论，都可以回到它的来源。</p>
            <div className="suggestions">
              <button
                onClick={() => setContent('帮我梳理知识库中已有的主题，指出值得补充的内容。')}
              >
                梳理已有知识 <ArrowUp size={14} />
              </button>
              <Link to="/sources">
                导入学习材料 <FileText size={15} />
              </Link>
            </div>
          </div>
        )}
        <ErrorBox
          message={messages.error || running.error || runs.error || drafts.error || error}
        />
        {messages.loading && !messages.data && <Loading />}
        {conversationId && messages.data?.length === 0 && (
          <Empty title="这段对话还没有消息">在下方写下问题，或关联材料开始整理。</Empty>
        )}
        {messages.data?.map((message) => (
          <article className={`message ${message.role}`} key={message.id}>
            <div className="message-label">
              {message.role === 'user' ? '你' : 'KnowledgeOps'}
              <span>
                {new Date(message.created_at).toLocaleTimeString([], {
                  hour: '2-digit',
                  minute: '2-digit',
                })}
              </span>
            </div>
            <Markdown text={message.content} />
          </article>
        ))}
        {active && <LiveRun key={active.id} initial={active} onChanged={onChanged} />}
        {runs.data
          .filter((run) => run.outcome.kind === 'waiting_approval' && run.status === 'interrupted')
          .map((run) => (
            <div key={run.id}>
              <MCPApprovals rootId={run.id} refreshKey={version} onChanged={refresh} />
              <Link to={`/runs/${run.id}`}>完成确认后，前往运行详情恢复任务 →</Link>
            </div>
          ))}
        {runs.data
          .filter((run) => run.status === 'queued')
          .map((run) => (
            <p className="muted small" key={run.id}>
              排队任务：{run.task.slice(0, 80)} · <Link to={`/runs/${run.id}`}>查看</Link>
              <button
                className="quiet small"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  setError('');
                  try {
                    await send(`/api/runs/${run.id}/cancel`, {});
                    refresh();
                  } catch (error) {
                    setError(messageOf(error));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                取消排队
              </button>
              {active && (
                <button
                  className="quiet small"
                  disabled={busy}
                  onClick={async () => {
                    setBusy(true);
                    setError('');
                    try {
                      await send(`/api/runs/${run.id}/guide`, {
                        target_root_id: active.id,
                        target_generation: active.generation,
                      });
                      refresh();
                    } catch (error) {
                      setError(messageOf(error));
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  引导当前任务
                </button>
              )}
            </p>
          ))}
        {runs.data
          .filter((run) => run.outcome.kind === 'guidance')
          .map((run) => (
            <p className="muted small" key={run.id}>
              已转为引导：{run.task.slice(0, 80)} ·{' '}
              <Link to={`/runs/${run.outcome.target_root_id}`}>查看投递记录</Link>
            </p>
          ))}
        {!active && runs.data?.[0] && (
          <div className="run-summary">
            <Badge status={runs.data[0].status} />
            {runs.data[0].outcome.kind && <Badge status={runs.data[0].outcome.kind} />}
            <Link to={`/runs/${runs.data[0].id}`}>运行记录 →</Link>
            {runs.data[0].stop_reason && <p>{runs.data[0].stop_reason}</p>}
          </div>
        )}
        {pending.map((draft) => (
          <Link className="draft-link" to={`/changes/${draft.id}`} key={draft.id}>
            <FileText size={20} />
            <span>
              <strong>{draft.operations[0]?.title || '知识维护草稿'}</strong>
              <small>{draft.operations.length} 篇文章 · 等待你的选择</small>
            </span>
            <span>审阅 →</span>
          </Link>
        ))}
        {drafts.next && (
          <button className="quiet" disabled={drafts.loading} onClick={drafts.loadMore}>
            加载更多会话变更
          </button>
        )}
        {runs.next && (
          <button className="quiet" disabled={runs.loading} onClick={runs.loadMore}>
            加载更多会话任务
          </button>
        )}
      </div>
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <label className="sr-only" htmlFor="message">
          发送给 KnowledgeOps 的消息
        </label>
        <textarea
          id="message"
          placeholder="提出问题，或说明希望如何整理知识…"
          rows={3}
          value={content}
          onChange={(e) => setContent(e.target.value)}
          onKeyDown={(event) => {
            // 普通 Enter 保留换行；中文输入法选字期间不能误发送。
            if (
              event.key === 'Enter' &&
              (event.ctrlKey || event.metaKey) &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              if (!busy && content.trim()) void submit();
            }
          }}
        />
        <div className="composer-options">
          <label>
            知识变更
            <select value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
              <option value="ask">需要时询问</option>
              <option value="preview">先看草稿</option>
              <option value="direct">本次直接保存</option>
            </select>
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={knowledgeOnly}
              onChange={(e) => setKnowledgeOnly(e.target.checked)}
            />
            仅使用知识库
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={delegation}
              onChange={(e) => setDelegation(e.target.checked)}
            />
            允许子Agent分工
          </label>
          <label className="check" title="按需增加模型请求，共享本任务预算">
            <input
              type="checkbox"
              checked={auxiliary}
              onChange={(e) => setAuxiliary(e.target.checked)}
            />
            允许辅助模型
          </label>
        </div>
        {materials.data && materials.data.length > 0 && (
          <details className="attachment-picker">
            <summary>
              <FileText size={14} />
              关联材料 {selected.length > 0 && `(${selected.length})`}
            </summary>
            <div>
              {materials.data.map((source) => (
                <label className="check" key={source.id}>
                  <input
                    type="checkbox"
                    checked={selected.includes(source.id)}
                    onChange={(e) =>
                      setSelected(
                        e.target.checked
                          ? [...selected, source.id]
                          : selected.filter((id) => id !== source.id),
                      )
                    }
                  />
                  {source.title}
                </label>
              ))}
            </div>
          </details>
        )}
        <div className="row between">
          <span className="muted small">Ctrl / ⌘ + Enter 发送 · 材料和草稿会保留</span>
          <button className="send" disabled={busy || !content.trim()} type="submit">
            {busy ? '提交中…' : '发送'}
            <ArrowUp size={17} />
          </button>
        </div>
      </form>
    </div>
  );
}
