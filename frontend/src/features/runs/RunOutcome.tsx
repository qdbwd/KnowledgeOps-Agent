import { Link } from 'react-router-dom';
import { useResource } from '../../shared/api/api';
import { Badge } from '../../shared/ui/components';
import type { Run } from '../../shared/types/types';

/** 运行结果是历史事实；草稿是否仍待审必须从当前版本读取。 */
export function RunOutcome({ run, refreshKey }: { run: Run; refreshKey: string | number }) {
  const result = useResource<{
    drafts: { id: string; status: string }[];
    drafts_truncated: boolean;
  }>(run.outcome.kind === 'draft_ready' ? `/api/runs/${run.id}/summary` : null, refreshKey);
  if (run.outcome.kind !== 'draft_ready')
    return run.outcome.kind ? <Badge status={run.outcome.kind} /> : null;
  const drafts = result.data?.drafts;
  const pending = drafts?.filter((draft) => ['draft', 'approved'].includes(draft.status));
  if (pending?.length)
    return (
      <Link
        to={pending.length === 1 ? `/knowledge/reviews/${pending[0].id}` : '/knowledge/reviews'}
      >
        草稿待审核 →
      </Link>
    );
  if (
    !result.loading &&
    drafts?.length &&
    !result.data?.drafts_truncated &&
    drafts.every((draft) => draft.status === 'published')
  )
    return <Badge status="published" />;
  if (
    !result.loading &&
    drafts?.length &&
    !result.data?.drafts_truncated &&
    drafts.every((draft) => draft.status === 'rejected')
  )
    return <Badge status="rejected" />;
  return <Link to="/knowledge/reviews">查看知识审阅 →</Link>;
}
