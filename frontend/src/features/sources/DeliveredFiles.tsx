import { Link } from 'react-router-dom';
import { useState } from 'react';
import { Download, FileText, Trash2 } from 'lucide-react';
import { useResource } from '../../shared/api/api';
import { useWorkspace } from '../../shared/state/workspace';
import { ErrorBox, Loading } from '../../shared/ui/components';
import { Button } from '../../shared/ui/Button';
import { AssetDeletionDialog } from './AssetDeletion';

type DeliveredFile = {
  id: string;
  name: string;
  bytes: number;
  created_at: string;
  url: string;
  view_url: string;
};

/** 已交付内容拥有固定身份；不从工作区目录猜测哪些文件是用户成果。 */
export function DeliveredFiles() {
  const { version } = useWorkspace();
  const result = useResource<DeliveredFile[]>('/api/deliveries', version);
  const [removing, setRemoving] = useState<DeliveredFile | null>(null);
  return (
    <section className="mt-8" aria-labelledby="delivered-files-title">
      <h2 id="delivered-files-title" className="mb-2 text-[17px]">
        已交付文件 <span className="ml-1 text-xs text-muted">{result.data?.length ?? 0}</span>
      </h2>
      <p className="mb-4 text-xs text-muted">
        这里保存交付时的固定内容，后续编辑工作文件不会改变它。
      </p>
      <ErrorBox message={result.error} />
      {result.loading ? (
        <Loading />
      ) : result.data?.length ? (
        <div className="grid gap-3">
          {result.data.map((item) => (
            <div
              key={item.id}
              className="flex items-center gap-4 rounded-lg border border-line bg-white p-5"
            >
              <FileText size={19} className="shrink-0 text-muted" />
              <div className="min-w-0 flex-1">
                <Link to={item.view_url} className="font-medium [overflow-wrap:anywhere]">
                  {item.name}
                </Link>
                <p className="mt-1 text-xs text-muted">
                  {Math.max(1, Math.ceil(item.bytes / 1024))} KiB ·{' '}
                  {new Date(item.created_at).toLocaleString('zh-CN')}
                </p>
              </div>
              <a
                href={item.url}
                download
                aria-label={`下载 ${item.name}`}
                className="rounded-md p-2 text-muted hover:bg-[#edf1eb]"
              >
                <Download size={18} />
              </a>
              <Button className="button-quiet text-danger" aria-label={`删除已交付文件 ${item.name}`}
                onClick={() => setRemoving(item)}><Trash2 size={18} /></Button>
            </div>
          ))}
        </div>
      ) : (
        !result.error && <p className="text-sm text-muted">智能体交付的文件会显示在这里。</p>
      )}
      {removing && <AssetDeletionDialog kind="delivery" identity={removing.id} title={removing.name} onClose={() => setRemoving(null)} />}
    </section>
  );
}
