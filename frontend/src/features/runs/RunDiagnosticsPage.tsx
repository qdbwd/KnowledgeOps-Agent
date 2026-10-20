import { ArrowLeft } from 'lucide-react';
import { Link, useParams } from 'react-router-dom';
import { Button } from '../../shared/ui/Button';
import { Badge, ErrorBox, Loading, PageTitle } from '../../shared/ui/components';
import { RunDiagnostics } from './RunDiagnostics';
import { useRunDetails } from './useRunDetails';
import { RemovedRun } from './DeleteRun';

/** 独立路由拥有诊断的完整生命周期，离页后卸载记录并中止未完成请求。 */
export function RunDiagnosticsPage() {
  const { runId } = useParams();
  const { result, refresh, detailsKey } = useRunDetails(runId);
  const run = result.data;
  if (run?.user_deleted_at) return <RemovedRun run={run} />;

  return (
    <div className="min-w-0">
      <Link
        className="back-link mb-[15px] inline-flex items-center gap-1.5 text-xs"
        to={`/runs/${runId}`}
      >
        <ArrowLeft size={15} />
        返回任务详情
      </Link>
      <PageTitle eyebrow="DIAGNOSTICS" title="任务诊断">
        <div className="flex flex-wrap items-center gap-3">
          {run?.conversation_id && (
            <Link className="button secondary button-secondary" to={`/chat/${run.conversation_id}`}>
              回到对话
            </Link>
          )}
          <Button className="secondary button-secondary" onClick={refresh}>
            刷新
          </Button>
        </div>
      </PageTitle>
      <ErrorBox message={result.error} />
      {result.loading && !run && <Loading />}
      {run && (
        <>
          <div className="flex min-w-0 flex-wrap items-start gap-3">
            <Badge status={run.status} />
            <p className="m-0 min-w-0 flex-1 whitespace-pre-wrap text-sm [overflow-wrap:anywhere]">
              {run.task}
            </p>
          </div>
          <RunDiagnostics key={`diagnostics:${run.id}`} run={run} refreshKey={detailsKey} />
        </>
      )}
    </div>
  );
}
