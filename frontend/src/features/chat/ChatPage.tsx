import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { ArrowUp, FileText, Sparkles, Square, Activity, ArrowDown } from 'lucide-react';
import { api, messageOf, send, terminal, usePagedResource, useResource } from '../../shared/api/api';
import type { CollectionPage } from '../../shared/api/api';
import { Badge, Empty, ErrorBox, Loading, Markdown, PageTitle } from '../../shared/ui/components';
import type { ChangeSet, Conversation, Guidance, Message, Run, RunEvent, Source } from '../../shared/types/types';
import { useWorkspace } from '../../shared/state/workspace';
import { ExecutionApprovals } from '../permissions/ExecutionApprovals';
import { ContextUsage } from './ContextUsage';
import { ComposerControls } from './ComposerControls';
import { ConversationLearning } from '../learning/LearningPage';
import { RunPauseControl } from '../runs/RunPauseControl';
import { RunQuestions } from '../runs/RunQuestions';
import { RunChildren } from '../runs/RunChildren';

type ConversationSettings = { permission_mode: string; reasoning_effort: string; revision: number };

/** 一个任务的流式观察，不承担运行生命周期；组件卸载只关闭连接，不取消 Agent。 */
function LiveRun({ initial, onChanged }: { initial: Run; onChanged: () => void }) {
  const [run, setRun] = useState(initial);
  const [activity, setActivity] = useState(initial.status === 'waiting_children' ? '等待子任务结果' : '等待执行');
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
      if (event.kind === 'pause_requested') setActivity('正在收尾暂停，保留当前响应');
      if (event.kind === 'waiting_children') setActivity('等待子任务结果');
      if (event.kind === 'children_ready') setActivity('子结果已就绪，准备继续');
      if (event.kind === 'guidance_ready') setActivity('已收到新要求，准备继续');
      if (['finished', 'paused', 'waiting_input', 'question_pending', 'waiting_children',
        'child_started', 'child_finished', 'child_updated', 'children_ready', 'guidance_ready'].includes(event.kind)) onChanged();
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
          if (terminal(value.status) || ['paused', 'waiting_input'].includes(value.status)) {
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
        <RunPauseControl run={run} onChanged={onChanged} />
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
          {run.cancel_requested ? '正在取消…' : '取消任务'}
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

export function ChatPage({ model, modelEnabled }: { model?: string; modelEnabled?: boolean } = {}) {
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
    true,
  );
  const running = useResource<CollectionPage<Run>>(
    conversationId
      ? `/api/runs/page?conversation_id=${conversationId}&roots_only=true&status=running`
      : null,
    version,
  );
  // 与分页历史分开读取，刷新历史时不能卸载等待中的 SSE 并反复重播旧事件。
  const waitingChildren = useResource<CollectionPage<Run>>(
    conversationId
      ? `/api/runs/page?conversation_id=${conversationId}&roots_only=true&status=waiting_children`
      : null,
    version,
  );
  const materials = useResource<Source[]>('/api/sources', version);
  const capabilities = useResource<{
    levels: string[];
    default_level: string | null;
    can_disable: boolean;
    note: string;
  }>('/api/model-capabilities', version);
  const drafts = usePagedResource<ChangeSet>(
    conversationId ? `/api/changesets/page?conversation_id=${conversationId}` : null,
    version,
  );
  const [content, setContent] = useState('');
  const scrollArea = useRef<HTMLDivElement>(null);
  const historyArea = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const [awayFromBottom, setAwayFromBottom] = useState(false);
  const toBottom = useCallback(() => {
    const area = scrollArea.current;
    if (area) area.scrollTop = area.scrollHeight;
    following.current = true;
    setAwayFromBottom(false);
  }, []);
  useEffect(() => {
    following.current = true;
    const frame = requestAnimationFrame(toBottom);
    return () => cancelAnimationFrame(frame);
  }, [conversationId, toBottom]);
  useEffect(() => {
    // 流式内容增长时只跟随已在底部的读者，不把正在翻看旧消息的人拉回去。
    const update = () => { if (following.current) toBottom(); };
    const frame = requestAnimationFrame(update);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
    if (historyArea.current) observer?.observe(historyArea.current);
    return () => { cancelAnimationFrame(frame); observer?.disconnect(); };
  }, [messages.data, toBottom]);
  const [mode, setMode] = useState<'ask' | 'preview' | 'direct'>('ask');
  const [knowledgeOnly, setKnowledgeOnly] = useState(false);
  const [delegation, setDelegation] = useState(true);
  const [auxiliary, setAuxiliary] = useState(false);
  const [reasoning, setReasoning] = useState('default');
  const [permission, setPermission] = useState('ask');
  const [settingsVersion, setSettingsVersion] = useState(0);
  const [savedSettings, setSavedSettings] = useState<{
    id: string;
    value: ConversationSettings;
  } | null>(null);
  const defaults = useResource<{
    permission_mode: string;
    reasoning_effort: string;
    revision: number;
  }>(
    conversationId ? `/api/conversations/${conversationId}/runtime-settings` : null,
    `${version}:${settingsVersion}`,
  );
  const [settingBusy, setSettingBusy] = useState(false);
  const currentSettings =
    savedSettings &&
    savedSettings.id === conversationId &&
    savedSettings.value.revision >= (defaults.data?.revision ?? -1)
      ? savedSettings.value
      : defaults.data;
  const chosenReasoning = conversationId
    ? (currentSettings?.reasoning_effort ?? 'default')
    : reasoning;
  const chosenPermission = conversationId
    ? (currentSettings?.permission_mode ?? 'ask')
    : permission;
  async function changeSetting(field: 'permission_mode' | 'reasoning_effort', value: string) {
    if (!conversationId) {
      if (field === 'permission_mode') setPermission(value);
      else setReasoning(value);
      return;
    }
    if (!currentSettings || settingBusy) return;
    setSettingBusy(true);
    setError('');
    try {
      const saved = await send<ConversationSettings>(
        `/api/conversations/${conversationId}/runtime-settings`,
        { expected_revision: currentSettings.revision, [field]: value },
        'PATCH',
      );
      setSavedSettings({ id: conversationId, value: saved });
      refresh();
    } catch (failure) {
      setError(messageOf(failure));
      refresh();
    } finally {
      setSettingBusy(false);
      setSettingsVersion((value) => value + 1);
    }
  }
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  // 网络失败重试使用同一个 key；只有成功提交或输入变化后才换 key。
  const requestKey = useRef({ content: '', id: '' });
  const onChanged = useCallback(() => refresh(), [refresh]);
  async function submit() {
    if (
      !content.trim() ||
      busy ||
      settingBusy ||
      (conversationId && (!defaults.data || defaults.loading))
    )
      return;
    setBusy(true);
    setError('');
    try {
      let id = conversationId;
      if (!id) {
        id = (
          await send<Conversation>('/api/conversations', { title: content.trim().slice(0, 30) })
        ).id;
        await send(
          `/api/conversations/${id}/runtime-settings`,
          { expected_revision: 0, permission_mode: permission, reasoning_effort: reasoning },
          'PATCH',
        );
        navigate(`/chat/${id}`, { replace: true });
      }
      const fingerprint = JSON.stringify([
        id,
        content,
        mode,
        knowledgeOnly,
        delegation,
        auxiliary,
        chosenReasoning,
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
        reasoning_effort: chosenReasoning,
        source_ids: selected,
      });
      // 服务已确认接受；下一次相同文字也是新意图。异常分支保留原 key，
      // 以便网络断开后重试不会在服务端生成重复任务。
      requestKey.current = { content: '', id: '' };
      following.current = true;
      setContent('');
      setSelected([]);
      refresh();
    } catch (error) {
      setError(messageOf(error));
    } finally {
      setBusy(false);
    }
  }
  // 分别查询 running/waiting 时，任务可能正处于两次响应之间的 queued 阶段。
  // 保留列表中未终结的观察者，直到获取终态；否则会同时丢掉 SSE 和轮询。
  const active = running.data?.items?.[0] ?? waitingChildren.data?.items?.[0]
    ?? runs.data.find((run) => ['running', 'waiting_children', 'queued'].includes(run.status));
  const guideTarget =
    (active?.kind === 'agent' ? active : undefined) ??
    runs.data.find(
      (run) => run.kind === 'agent' && (['paused', 'waiting_input'].includes(run.status) ||
        (run.status === 'interrupted' && run.outcome.kind === 'waiting_approval')),
    );
  const hasQueued = runs.data.some((run) => run.status === 'queued');
  const pending =
    drafts.data?.filter((draft) => ['draft', 'approved'].includes(draft.status)) ?? [];
  return (
    <div className="chat-page">
      <div className="chat-scroll" ref={scrollArea} onScroll={() => {
        const area = scrollArea.current;
        if (!area) return;
        const away = area.scrollHeight - area.scrollTop - area.clientHeight > 80;
        following.current = !away;
        setAwayFromBottom(away);
      }}>
      <div className="chat-content" ref={historyArea}>
      <PageTitle
        eyebrow="THINK · RESEARCH · REMEMBER"
        title={conversationId ? '继续这段思考' : '把探索变成可积累的知识'}
      >
        {conversationId ? (
          <div className="row wrap">
          <Link className="button secondary small" to={`/chat/${conversationId}/deletion`}>删除会话</Link>
          </div>
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
          message={
            messages.error || running.error || waitingChildren.error || runs.error || drafts.error || defaults.error || error
          }
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
        {runs.data.filter((run) => ['paused', 'waiting_input'].includes(run.status)).map((run) => (
          <section className="card" key={run.id}>
            <div className="row between">
              <span><Badge status={run.status} /> {run.task.slice(0, 80)}</span>
              <RunPauseControl run={run} onChanged={refresh} />
            </div>
            <p className="muted small">进展已保留。<Link to={`/runs/${run.id}`}>查看详情或取消</Link></p>
            {run.outcome.answer && <Markdown text={run.outcome.answer} />}
          </section>
        ))}
        {runs.data.filter((run) => run.question_count).map((run) => (
          <RunQuestions key={`questions-${run.id}`} run={run} refreshKey={version} onChanged={refresh} />
        ))}
        {runs.data.filter((run) => run.child_count).map((run) => (
          <RunChildren key={`children-${run.id}`} run={run} refreshKey={version} onChanged={refresh} />
        ))}
        {runs.data
          .filter(
            (run) =>
              ['waiting_approval', 'execution_unknown'].includes(run.outcome.kind ?? '') &&
              ['interrupted', 'paused'].includes(run.status),
          )
          .map((run) => (
            <div key={run.id}>
              <ExecutionApprovals run={run} refreshKey={version} onChanged={refresh} />
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
              {guideTarget && (
                <button
                  className="quiet small"
                  disabled={busy}
                  onClick={async () => {
                    setBusy(true);
                    setError('');
                    try {
                      await send(`/api/runs/${run.id}/guide`, {
                        target_root_id: guideTarget.id,
                        target_generation: guideTarget.generation,
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
      </div>
      </div>
      <div className="composer-dock">
      {awayFromBottom && <button type="button" className="scroll-to-bottom secondary" aria-label="回到最新消息" onClick={toBottom}>
        <ArrowDown size={19} />
      </button>}
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
        <ComposerControls conversationId={conversationId} permission={chosenPermission} model={model} modelEnabled={modelEnabled}
          reasoning={chosenReasoning} settingBusy={settingBusy || Boolean(conversationId && !defaults.data)}
          changeSetting={(field, value) => void changeSetting(field, value)} capabilities={capabilities.data}
          mode={mode} setMode={setMode} knowledgeOnly={knowledgeOnly} setKnowledgeOnly={setKnowledgeOnly}
          delegation={delegation} setDelegation={setDelegation} auxiliary={auxiliary} setAuxiliary={setAuxiliary}
          materials={materials.data ?? []} selected={selected} setSelected={setSelected}
          busy={busy} disabled={busy || settingBusy || Boolean(conversationId && defaults.loading) || !content.trim()}
          context={conversationId ? <ContextUsage key={conversationId} conversationId={conversationId}
            active={Boolean(active) || hasQueued} version={version} onChanged={refresh} /> : null} />
      </form>
      </div>
    </div>
  );
}
