import { useEffect, useState } from 'react';
import { terminal, useResource } from '../../shared/api/api';
import { useWorkspace } from '../../shared/state/workspace';
import type { Run } from '../../shared/types/types';

/** 详情与诊断共用运行状态轮询；完整诊断记录仍由诊断页单独读取。 */
export function useRunDetails(runId: string | undefined) {
  const { version, refresh } = useWorkspace();
  const [tick, setTick] = useState(0);
  const key = `${version}:${tick}`;
  const result = useResource<Run>(runId ? `/api/runs/${runId}` : null, key);
  const status = result.data?.status;
  const detailsAvailable = result.data?.details_available;
  const detailsKey = `${version}:${status ?? ''}:${result.data?.last_event ?? ''}:${detailsAvailable ?? ''}`;

  useEffect(() => {
    if (!status || terminal(status) || status === 'paused' || detailsAvailable === false) return;
    const timer = setInterval(() => setTick((n) => n + 1), 2000);
    return () => clearInterval(timer);
  }, [status, runId, detailsAvailable]);

  return { result, refresh, key, detailsKey };
}
