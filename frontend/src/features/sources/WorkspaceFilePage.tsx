import { useLocation, useSearchParams } from 'react-router-dom';
import { useResource } from '../../shared/api/api';
import { DocumentReader } from '../../shared/ui/DocumentReader';
import { ErrorBox, Loading, PageTitle } from '../../shared/ui/components';

export function WorkspaceFilePage() {
  const [params] = useSearchParams();
  const { hash } = useLocation();
  const path = params.get('path') ?? '';
  const line = Number(hash.match(/^#L(\d+)/)?.[1] ?? params.get('line')) || undefined;
  const result = useResource<{
    name: string;
    text: string | null;
    markdown?: boolean;
    reason?: string;
  }>(`/api/workspace/preview?path=${encodeURIComponent(path)}`);
  if (result.loading) return <Loading />;
  if (!result.data) return <ErrorBox message={result.error} />;
  return (
    <>
      <PageTitle eyebrow="WORKSPACE FILE" title={result.data.name}>
        <a
          className="button secondary button-secondary"
          href={`/api/workspace/download?path=${encodeURIComponent(path)}`}
          download
        >
          下载文件
        </a>
      </PageTitle>
      <article className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5 source-reader min-w-0">
        {result.data.text === null ? (
          <p>{result.data.reason}</p>
        ) : (
          <DocumentReader
            key={path}
            text={result.data.text}
            markdown={result.data.markdown}
            line={line}
          />
        )}
      </article>
    </>
  );
}
