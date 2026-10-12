import { useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { ArrowLeft, FileUp, FileText, ExternalLink } from 'lucide-react';
import { api, messageOf, send, useResource } from './api';
import { Badge, Empty, ErrorBox, Loading, PageTitle } from './components';
import type { Evidence, Run, Source, SourceDetail } from './types';
import { useWorkspace } from './workspace';

export function SourcesPage() {
  const { version, refresh } = useWorkspace();
  const result = useResource<Source[]>('/api/sources', version);
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  async function upload(file: File | undefined) {
    if (!file) return;
    setBusy(true);
    setError('');
    try {
      const form = new FormData();
      form.append('file', file);
      const source = await api<SourceDetail | { source_id: string; job_id: string }>(
        '/api/sources/upload',
        { method: 'POST', body: form },
      );
      refresh();
      navigate('job_id' in source ? `/runs/${source.job_id}` : `/sources/${source.id}`);
    } catch (error) {
      setError(messageOf(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <PageTitle eyebrow="FOLLOW THE EVIDENCE" title="材料与来源" />
      <p className="intro">原始材料与知识文章分开保存。阅读位置和引用固定到实际使用的版本。</p>
      <ErrorBox message={error || result.error} />
      <div className="two-columns import-area">
        <label className="upload-zone">
          <FileUp size={27} />
          <strong>{busy ? '正在导入…' : '上传学习材料'}</strong>
          <span>Markdown / TXT / PDF · 最大 10 MiB</span>
          <span>PDF 后台解析，最多 40 页，支持普通中英文扫描页。</span>
          <input
            aria-label="上传材料文件"
            type="file"
            accept=".md,.txt,.pdf"
            disabled={busy}
            onChange={(e) => void upload(e.target.files?.[0])}
          />
        </label>
        <form
          className="card text-import"
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError('');
            try {
              const source = await send<SourceDetail>('/api/sources/text', { title, content });
              refresh();
              navigate(`/sources/${source.id}`);
            } catch (error) {
              setError(messageOf(error));
            } finally {
              setBusy(false);
            }
          }}
        >
          <h2>粘贴文本</h2>
          <label className="sr-only" htmlFor="material-title">
            材料标题
          </label>
          <input
            id="material-title"
            placeholder="给这份材料一个标题"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <label className="sr-only" htmlFor="material-content">
            材料内容
          </label>
          <textarea
            id="material-content"
            rows={4}
            placeholder="粘贴正文，稍后在对话中关联…"
            value={content}
            onChange={(e) => setContent(e.target.value)}
          />
          <button disabled={busy || !title.trim() || !content.trim()}>保存材料</button>
        </form>
      </div>
      <form
        className="card text-import"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError('');
          try {
            const run = await send<Run>('/api/sources/url', {
              url,
              request_key: crypto.randomUUID(),
            });
            refresh();
            navigate(`/runs/${run.id}`);
          } catch (error) {
            setError(messageOf(error));
          } finally {
            setBusy(false);
          }
        }}
      >
        <h2>导入公开网页</h2>
        <label className="sr-only" htmlFor="material-url">
          网页地址
        </label>
        <input
          id="material-url"
          type="url"
          placeholder="https://…"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
        <p className="muted small">保存原始快照和解析正文。导入后台进行，不自动整理为知识文章。</p>
        <button disabled={busy || !url.trim()}>开始导入网页</button>
      </form>
      <h2 className="section-title">
        已保存来源 <span>{result.data?.length ?? 0}</span>
      </h2>
      {result.error ? null : result.loading ? (
        <Loading />
      ) : result.data?.length ? (
        <div className="list-cards">
          {result.data.map((source) => (
            <Link className="list-card" key={source.id} to={`/sources/${source.id}`}>
              <FileText size={20} />
              <div>
                <h3>{source.title}</h3>
                <p>{source.origin || '本项目导入材料'}</p>
              </div>
              <span className="badge">{source.kind}</span>
              <span>查看 →</span>
            </Link>
          ))}
        </div>
      ) : (
        <Empty title="还没有来源材料">导入文件或文本后，可在对话中选择它们。</Empty>
      )}
    </>
  );
}

function Segment({ id }: { id: string }) {
  const result = useResource<{ text: string }>(`/api/source-segments/${id}`);
  return result.loading ? (
    <Loading />
  ) : (
    <>
      <ErrorBox message={result.error} />
      <pre className="source-text">{result.data?.text}</pre>
    </>
  );
}

export function SourcePage() {
  const { sourceId } = useParams();
  const [params, setParams] = useSearchParams();
  const version = params.get('version');
  // 历史证据的版本和定位保存在URL中，刷新/前进后退也不能跳到最新材料。
  const result = useResource<SourceDetail>(
    `/api/sources/${sourceId}${version ? `?version_id=${encodeURIComponent(version)}` : ''}`,
  );
  const requestedSegment = params.get('segment');
  if (result.loading) return <Loading />;
  if (!result.data) return <ErrorBox message={result.error} />;
  const source = result.data;
  const segment = source.segments.some((item) => item.id === requestedSegment)
    ? requestedSegment
    : null;
  return (
    <>
      <Link className="back-link" to="/sources">
        <ArrowLeft size={15} />
        全部来源
      </Link>
      <PageTitle eyebrow={`SOURCE / VERSION ${source.version}`} title={source.title}>
        <Badge status={source.status} />
      </PageTitle>
      <div className="card">
        <ErrorBox message={source.error} />
        {source.kind === 'pdf' && (
          <p className="notice">
            文本提取与 OCR 用于辅助阅读；复杂表格、公式和手写内容请核对原件。
          </p>
        )}
        <div className="row wrap">
          <a
            href={`/api/source-versions/${source.version_id}/original`}
            target="_blank"
            rel="noreferrer"
            className="button secondary"
          >
            <FileText size={16} />
            查看原件
          </a>
          {source.origin && /^https?:\/\//i.test(source.origin) && (
            <a href={source.origin} target="_blank" rel="noreferrer" className="button secondary">
              <ExternalLink size={16} />
              原始网址
            </a>
          )}
          <Link className="button" to="/">
            在对话中使用 →
          </Link>
        </div>
        <p className="muted small">
          版本 ID：{source.version_id} · {source.segments.length} 个定位片段
        </p>
        <div className="segment-list">
          {source.segments.map((item) => (
            <button
              className={segment === item.id ? 'selected secondary' : 'secondary'}
              key={item.id}
              onClick={() =>
                setParams((previous) => {
                  const next = new URLSearchParams(previous);
                  next.set('segment', item.id);
                  return next;
                })
              }
            >
              片段 {item.position + 1}
              {item.page ? ` · 第 ${item.page} 页` : ''}
              <span className="muted">
                {item.status === 'ready' ? `${item.start}–${item.end}` : item.error}
              </span>
            </button>
          ))}
        </div>
        {requestedSegment && !segment ? (
          <ErrorBox message="指定片段不属于当前来源版本。" />
        ) : segment ? (
          <Segment key={segment} id={segment} />
        ) : (
          <p className="muted">选择片段查看解析后的正文。</p>
        )}
      </div>
    </>
  );
}

export function EvidencePage() {
  const { evidenceId } = useParams();
  const result = useResource<Evidence>(`/api/evidence/${evidenceId}`);
  if (result.loading) return <Loading />;
  if (!result.data) return <ErrorBox message={result.error} />;
  const evidence = result.data;
  return (
    <>
      <PageTitle eyebrow="EVIDENCE / PINNED VERSION" title={evidence.title} />
      <p className="intro">这是当时实际读取并引用的内容。文章之后的修改不会改变这条历史引用。</p>
      <article className="card">
        <dl className="metadata">
          <div>
            <dt>定位</dt>
            <dd>
              {evidence.page ? `第 ${evidence.page} 页 · ` : ''}字符 {evidence.start}–{evidence.end}
            </dd>
          </div>
          <div>
            <dt>证据 ID</dt>
            <dd>{evidence.id}</dd>
          </div>
        </dl>
        <pre className="source-text">{evidence.text}</pre>
        <div className="row wrap">
          {evidence.source_id && evidence.source_version_id && (
            <Link
              className="button secondary"
              to={`/sources/${evidence.source_id}?version=${encodeURIComponent(evidence.source_version_id)}${evidence.segment_id ? `&segment=${encodeURIComponent(evidence.segment_id)}` : ''}`}
            >
              查看来源材料
            </Link>
          )}
          {evidence.document_id && (
            <Link
              className="button secondary"
              to={`/knowledge/${evidence.document_id}?version=${evidence.knowledge_version_id}`}
            >
              查看引用的知识版本
            </Link>
          )}
          {evidence.source_version_id && (
            <a
              href={`/api/source-versions/${evidence.source_version_id}/original${evidence.page ? `#page=${evidence.page}` : ''}`}
              target="_blank"
              rel="noreferrer"
            >
              打开当时的原件 ↗
            </a>
          )}
          {evidence.origin && /^https?:\/\//i.test(evidence.origin) && (
            <a href={evidence.origin} target="_blank" rel="noreferrer">
              原始网址 ↗
            </a>
          )}
        </div>
      </article>
    </>
  );
}
