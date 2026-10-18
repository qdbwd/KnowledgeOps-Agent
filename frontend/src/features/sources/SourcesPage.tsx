import { Button } from '../../shared/ui/Button';
import { useState } from 'react';
import { Link, Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { ArrowLeft, FileUp, FileText, ExternalLink, Trash2 } from 'lucide-react';
import { api, messageOf, send, useResource } from '../../shared/api/api';
import { Badge, Empty, ErrorBox, Loading, PageTitle } from '../../shared/ui/components';
import type { Evidence, Run, Source, SourceDetail } from '../../shared/types/types';
import { useWorkspace } from '../../shared/state/workspace';
import { DocumentReader } from '../../shared/ui/DocumentReader';
import { evidenceTarget } from '../../shared/ui/EvidenceLink';

export function SourcesPage() {
  const { version, refresh } = useWorkspace();
  const result = useResource<Source[]>('/api/sources', version);
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState<Source | null>(null);
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
      <p className="intro text-muted">
        原始材料与知识文章分开保存。阅读位置和引用固定到实际使用的版本。
      </p>
      <ErrorBox message={error || result.error} />
      <div className="two-columns grid grid-cols-2 gap-5.5 max-lg:gap-[15px] max-md:grid-cols-1 import-area mt-6.5">
        <label className="upload-zone relative mb-5.5 min-h-[285px] cursor-pointer items-center justify-center gap-[15px] rounded-[10px] border border-dashed border-[#bacbaa] bg-[#f0f4e9] text-[#6b865b] max-md:min-h-[200px] [&>span]:text-[11px] [&>span]:text-[#93a482] [&>input]:max-w-[90%] [&>input]:border-0 [&>input]:bg-transparent [&>input]:p-[5px] [&>input]:text-[11px]">
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
          className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5 text-import grid gap-2.5 [&>h2]:m-0 [&>h2]:text-base [&>button]:justify-self-end"
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
          <Button disabled={busy || !title.trim() || !content.trim()}>保存材料</Button>
        </form>
      </div>
      <form
        className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5 text-import grid gap-2.5 [&>h2]:m-0 [&>h2]:text-base [&>button]:justify-self-end"
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
        <p className="muted text-muted small text-xs">
          保存原始快照和解析正文。导入后台进行，不自动整理为知识文章。
        </p>
        <Button disabled={busy || !url.trim()}>开始导入网页</Button>
      </form>
      <h2 className="section-title my-4.5 text-[17px] [&>span]:ml-[5px] [&>span]:text-xs [&>span]:text-[#90a184]">
        已保存来源 <span>{result.data?.length ?? 0}</span>
      </h2>
      {result.error ? null : result.loading ? (
        <Loading />
      ) : result.data?.length ? (
        <div className="list-cards grid gap-3">
          {result.data.map((source) => (
            <div
              className="list-card flex items-center gap-[17px] rounded-[9px] border border-line bg-white px-[23px] py-4.5 text-[#5c7057] hover:border-[#aec2a5] hover:no-underline max-md:flex-wrap max-md:gap-3 max-md:p-4 [&>div]:min-w-0 [&>div]:flex-1 [&_h3]:mt-0 [&_h3]:mb-[5px] [&_h3]:text-[#344c37] [&_h3]:[overflow-wrap:anywhere] max-md:[&_h3]:text-[13px] [&_p]:m-0 [&_p]:text-[11px] [&_p]:text-[#85917e] [&_p]:[overflow-wrap:anywhere] [&>span:last-child]:text-[11px] [&>span:last-child]:whitespace-nowrap max-md:[&>span:last-child]:hidden source-list-row [&>div]:min-w-0 [&>div]:flex-1 [&>div]:[overflow-wrap:anywhere]"
              key={source.id}
            >
              <FileText size={20} />
              <div>
                <h3>
                  {source.origin && /^https?:\/\//i.test(source.origin) ? (
                    <a href={source.origin} target="_blank" rel="noreferrer noopener">
                      {source.title} ↗
                    </a>
                  ) : (
                    <Link to={`/sources/${source.id}`}>{source.title}</Link>
                  )}
                </h3>
                <p>{source.origin || '本项目导入材料'}</p>
              </div>
              <span className="badge inline-flex items-center rounded-[5px] bg-[#edf1eb] px-[9px] py-[3px] text-[11px] leading-[1.6] whitespace-nowrap text-[#5f7765]">
                {source.kind}
              </span>
              <Button
                type="button"
                className="quiet button-quiet danger text-danger"
                aria-label={`删除材料 ${source.title}`}
                onClick={() => setRemoving(source)}
              >
                <Trash2 size={16} />
              </Button>
            </div>
          ))}
        </div>
      ) : (
        <Empty title="还没有来源材料">导入文件或文本后，可在对话中选择它们。</Empty>
      )}
      {removing && (
        <div className="modal-backdrop fixed inset-0 z-50 grid place-items-center bg-[#122e2460] p-5">
          <section
            className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5 source-delete-dialog w-[min(520px,100%)] bg-white"
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-source-title"
            onKeyDown={(event) => {
              if (event.key === 'Escape' && !busy) setRemoving(null);
              if (event.key !== 'Tab') return;
              const buttons = Array.from(
                event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'),
              );
              const next = event.shiftKey ? buttons.at(-1) : buttons[0];
              const edge = event.shiftKey ? buttons[0] : buttons.at(-1);
              if (document.activeElement === edge) {
                event.preventDefault();
                next?.focus();
              }
            }}
          >
            <h2 id="delete-source-title">删除材料</h2>
            <p>从材料库移除“{removing.title}”？它将不再出现在可选材料中。</p>
            <p className="muted text-muted small text-xs">
              已保存的版本与原件继续保留，已有聊天和知识文章的引用不受影响。
            </p>
            <ErrorBox message={error} />
            <div className="row flex items-center gap-3">
              <Button
                autoFocus
                className="secondary button-secondary"
                disabled={busy}
                onClick={() => setRemoving(null)}
              >
                取消
              </Button>
              <Button
                className="danger text-danger secondary button-secondary"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  setError('');
                  try {
                    await send(`/api/sources/${removing.id}`, {}, 'DELETE');
                    setRemoving(null);
                    refresh();
                  } catch (failure) {
                    setError(messageOf(failure));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {busy ? '正在删除…' : '确认删除材料'}
              </Button>
            </div>
          </section>
        </div>
      )}
    </>
  );
}

function SourceContent({ source, page }: { source: SourceDetail; page: string | null }) {
  const [parsed, setParsed] = useState(false);
  const content = useResource<{
    text: string;
    segments: { id: string; page: number | null; text: string; status: string; error?: string }[];
  }>(source.kind === 'pdf' && !parsed ? null : `/api/source-versions/${source.version_id}/content`);
  return (
    <>
      {source.kind === 'pdf' && (
        <div className="reader-toolbar mb-4.5 flex items-center gap-3">
          <Button
            className="secondary button-secondary small text-xs"
            onClick={() => setParsed(!parsed)}
          >
            {parsed ? '查看 PDF 原件' : '查看提取文本'}
          </Button>
        </div>
      )}
      {source.kind === 'pdf' && !parsed ? (
        <iframe
          className="pdf-reader block h-[75dvh] w-full rounded-lg border border-line"
          title={source.title}
          src={`/api/source-versions/${source.version_id}/original${page ? `#page=${encodeURIComponent(page)}` : ''}`}
        />
      ) : content.loading ? (
        <Loading />
      ) : content.error ? (
        <ErrorBox message={content.error} />
      ) : (
        content.data &&
        (source.kind === 'pdf' ? (
          content.data.segments.map((item) => (
            <section key={item.id}>
              <h3>第 {item.page} 页</h3>
              {item.status !== 'ready' && (
                <ErrorBox message={item.error || '本页提取失败，请查看原件'} />
              )}
              <DocumentReader text={item.text} />
            </section>
          ))
        ) : (
          <DocumentReader
            text={content.data.text}
            markdown={source.kind !== 'file' || /\.md$/i.test(source.title)}
          />
        ))
      )}
    </>
  );
}

export function SourcePage() {
  const { sourceId } = useParams();
  const [params] = useSearchParams();
  const version = params.get('version');
  const result = useResource<SourceDetail>(
    `/api/sources/${sourceId}${version ? `?version_id=${encodeURIComponent(version)}` : ''}`,
  );
  if (result.loading) return <Loading />;
  if (!result.data) return <ErrorBox message={result.error} />;
  const source = result.data;
  return (
    <>
      <Link className="back-link mb-[15px] inline-flex items-center gap-1.5 text-xs" to="/sources">
        <ArrowLeft size={15} />
        全部来源
      </Link>
      <PageTitle eyebrow={`DOCUMENT / VERSION ${source.version}`} title={source.title}>
        <Badge status={source.status} />
      </PageTitle>
      {source.deleted && (
        <p className="notice border-l-[3px] border-[#c0a76b] bg-[#f5f0e0] px-4 py-3 text-[13px] [overflow-wrap:anywhere]">
          此材料已从材料库移除；这里保留引用时的完整版本。
        </p>
      )}
      <div className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5 source-reader min-w-0">
        <ErrorBox message={source.error} />
        <div className="row flex items-center gap-3 wrap flex-wrap reader-toolbar mb-4.5 flex items-center gap-3">
          <a
            href={`/api/source-versions/${source.version_id}/original`}
            target="_blank"
            rel="noreferrer"
            className="button secondary button-secondary"
          >
            <FileText size={16} />
            查看原件
          </a>
          {source.origin && /^https?:\/\//i.test(source.origin) && (
            <a
              href={source.origin}
              target="_blank"
              rel="noreferrer noopener"
              className="button secondary button-secondary"
            >
              <ExternalLink size={16} />
              原始网页
            </a>
          )}
          <span className="muted text-muted small text-xs">完整保存版本 v{source.version}</span>
        </div>
        <SourceContent key={source.version_id} source={source} page={params.get('page')} />
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
  const target = evidenceTarget(evidence);
  if (target && !/^https?:\/\//i.test(target)) return <Navigate to={target} replace />;
  // 旧书签仍可访问。网页优先给原始链接，保存的完整快照作为可选回看入口。
  return (
    <>
      <PageTitle eyebrow="SOURCE" title={evidence.title} />
      {target && (
        <a className="button" href={target} target="_blank" rel="noreferrer noopener">
          打开原始网页 ↗
        </a>
      )}
      {evidence.source_id && evidence.source_version_id && (
        <Link
          className="button secondary button-secondary"
          to={`/sources/${evidence.source_id}?version=${encodeURIComponent(evidence.source_version_id)}`}
        >
          查看完整保存版本
        </Link>
      )}
      {!target && <ErrorBox message="此引用没有可打开的完整来源。" />}
    </>
  );
}
