import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useResource } from '../../shared/api/api';
import { Button } from '../../shared/ui/Button';
import { ErrorBox, Loading, PageTitle } from '../../shared/ui/components';

/** 与 /api/readiness 对齐：只展示服务端算出的事实，不在前端推导状态。 */
export type Readiness = {
  status: 'ready' | 'degraded' | 'blocked';
  checked_at: number;
  cached: boolean;
  layers: {
    process: { ok: boolean; detail: string };
    foundation: {
      ok: boolean;
      database: { ok: boolean; detail?: string };
      migration: { ok: boolean; current?: string | null; head?: string | null; detail?: string };
      receipts: { ok: boolean; detail?: string };
      directories: { ok: boolean; items: Record<string, boolean | string> };
    };
    execution: {
      ok: boolean;
      state: 'ready' | 'busy' | 'offline';
      worker_lease: boolean;
      active_runs: number;
      heartbeat_seconds: number | null;
      redis: Record<string, { ok: boolean; detail?: string }>;
    };
    backlog: { ok: boolean; queued_runs: number; due_dispatches: number; retrying_dispatches: number };
  };
};

const statusText: Record<Readiness['status'], string> = {
  ready: '运行就绪',
  degraded: '基础就绪，执行未就绪',
  blocked: '存在阻塞，需要处理',
};
const executionText: Record<Readiness['layers']['execution']['state'], string> = {
  ready: 'Worker 空闲，可接单',
  busy: 'Worker 正在执行任务',
  offline: 'Worker 未启动或已停止',
};

export type BackupRecord = {
  id: string;
  created_at: string | null;
  format: string;
  status: 'verified' | 'created' | 'incomplete';
  detail: string;
  operations: { id: string; kind: string; state: string; phase: string; database: string }[];
  restore: { checked: boolean; passed: boolean; failed_checks: string[]; database: string };
};
export type BackupRecords = { available: boolean; detail?: string; items: BackupRecord[]; truncated: boolean };

const backupStatus: Record<BackupRecord['status'], string> = {
  verified: '已校验并有隔离恢复',
  created: '已创建，未通过校验',
  incomplete: '未完成',
};

function Row({ label, ok, detail }: { label: string; ok: boolean; detail?: string }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line py-2 last:border-b-0">
      <span className="w-28 shrink-0 text-sm">{label}</span>
      <span className={ok ? 'text-[#347044]' : 'text-[#a44b40]'}>{ok ? '正常' : '未就绪'}</span>
      {detail && <span className="min-w-0 break-words text-xs text-muted">{detail}</span>}
    </div>
  );
}

export function SystemPage() {
  const [tick, setTick] = useState(0);
  const { data, loading, error } = useResource<Readiness>('/api/readiness', tick);
  const backups = useResource<BackupRecords>('/api/maintenance/backups', tick);
  useEffect(() => {
    // 页面隐藏时不探测；回到前台由下一次可见检查刷新。
    const timer = setInterval(() => {
      if (document.visibilityState !== 'hidden') setTick((value) => value + 1);
    }, 5000);
    return () => clearInterval(timer);
  }, []);

  const layers = data?.layers;
  return (
    <>
      <PageTitle eyebrow="SYSTEM" title="系统与维护">
        <Button className="button-secondary" onClick={() => setTick((value) => value + 1)}>重新检查</Button>
      </PageTitle>
      <ErrorBox message={error} />
      <p className="text-sm text-muted">
        任务进度与结果仍以运行记录为准；本页只读展示当前运行条件，不领取、批准或恢复任何任务。
      </p>
      {loading && !data && <Loading />}
      {data && layers && (
        <div className="mt-4 grid gap-4">
          <section className="card rounded-xl border border-line bg-white p-5" aria-label="总体状态">
            <h2 className="m-0 text-base">总体状态：{statusText[data.status]}</h2>
            <p className="mt-2 mb-0 text-xs text-muted">
              检查时间 {new Date(data.checked_at * 1000).toLocaleString()}
              {data.cached ? '（来自短缓存）' : ''}
            </p>
          </section>

          <section className="card rounded-xl border border-line bg-white p-5" aria-label="基础依赖">
            <h2 className="m-0 mb-2 text-base">基础依赖</h2>
            <Row label="API 进程" ok={layers.process.ok} detail="能响应请求即表示进程存活" />
            <Row label="业务库" ok={layers.foundation.database.ok} detail={layers.foundation.database.detail} />
            <Row label="数据库版本" ok={layers.foundation.migration.ok}
              detail={layers.foundation.migration.ok
                ? `当前 ${layers.foundation.migration.current}`
                : layers.foundation.migration.detail
                  ?? `当前 ${layers.foundation.migration.current ?? '未知'} / 期望 ${layers.foundation.migration.head ?? '未知'}`} />
            <Row label="回执库" ok={layers.foundation.receipts.ok} detail={layers.foundation.receipts.detail} />
            <Row label="工作目录" ok={layers.foundation.directories.ok}
              detail={Object.entries(layers.foundation.directories.items ?? {})
                .filter(([key]) => key.endsWith('_path'))
                .map(([key, value]) => `${key.replace('_path', '')}: ${value || '未配置'}`)
                .join('；')} />
          </section>

          <section className="card rounded-xl border border-line bg-white p-5" aria-label="执行就绪">
            <h2 className="m-0 mb-2 text-base">执行就绪</h2>
            <p className="mt-0 text-sm">{executionText[layers.execution.state]}</p>
            <Row label="Worker 租约" ok={layers.execution.worker_lease} detail="同一库只允许一个 Worker 实例" />
            <Row label="正在执行" ok detail={`${layers.execution.active_runs} 个任务`} />
            <Row label="最近心跳" ok={layers.execution.heartbeat_seconds !== null}
              detail={layers.execution.heartbeat_seconds === null
                ? '没有正在执行的任务'
                : `${layers.execution.heartbeat_seconds} 秒前`} />
            {Object.entries(layers.execution.redis ?? {}).map(([name, item]) => (
              <Row key={name} label={name === 'broker' ? '任务队列' : '实时通道'} ok={item.ok}
                detail={item.ok ? '' : item.detail} />
            ))}
          </section>

          <section className="card rounded-xl border border-line bg-white p-5" aria-label="排队与积压">
            <h2 className="m-0 mb-2 text-base">排队与积压</h2>
            <Row label="排队任务" ok detail={`${layers.backlog.queued_runs} 个`} />
            <Row label="待投递" ok={layers.backlog.due_dispatches === 0}
              detail={`${layers.backlog.due_dispatches} 个到期未发出`} />
            <Row label="投递重试" ok={layers.backlog.retrying_dispatches === 0}
              detail={`${layers.backlog.retrying_dispatches} 个正在重试`} />
            <p className="mb-0 text-xs text-muted">
              单个任务为什么还在排队，可在该任务详情页查看；入口见 <Link to="/runs">运行记录</Link>。
            </p>
          </section>

          <section className="card rounded-xl border border-line bg-white p-5" aria-label="备份与恢复">
            <h2 className="m-0 mb-2 text-base">备份与恢复</h2>
            <ErrorBox message={backups.error} />
            {backups.loading && !backups.data && <Loading />}
            {backups.data && !backups.data.available && (
              <p className="m-0 text-sm text-muted">{backups.data.detail}。</p>
            )}
            {backups.data?.available && (
              <>
                {backups.data.items.length === 0 && <p className="m-0 text-sm text-muted">尚无备份记录。</p>}
                {backups.data.items.map((record) => (
                  <div key={record.id} className="border-b border-line py-2 last:border-b-0">
                    <div className="flex flex-wrap items-center gap-2 text-sm">
                      <span className="font-mono text-xs">{record.id}</span>
                      <span className="text-xs text-muted">
                        {record.created_at ? new Date(record.created_at).toLocaleString() : '时间未知'}
                      </span>
                      {record.format && <span className="text-xs text-muted">格式 {record.format}</span>}
                      <span className={record.status === 'verified' ? 'text-[#347044]' : 'text-[#a44b40]'}>
                        {backupStatus[record.status]}
                      </span>
                    </div>
                    <p className="m-0 text-xs text-muted">
                      {record.detail}
                      {record.restore.failed_checks.length > 0 && `；未通过：${record.restore.failed_checks.join('、')}`}
                    </p>
                    {record.operations.map((operation) => (
                      <p key={operation.id} className="m-0 text-xs text-muted">
                        操作 {operation.kind} · {operation.state}
                        {operation.database ? ` · ${operation.database}` : ''}
                      </p>
                    ))}
                  </div>
                ))}
                {backups.data.truncated && <p className="mb-0 text-xs text-muted">仅显示最近的记录。</p>}
              </>
            )}
            <p className="mt-2 mb-0 text-xs text-muted">
              创建与校验由本机命令执行（<code>./scripts/backup.ps1</code>、独立校验 <code>./scripts/verify_backup.ps1</code>）；
              网页只读展示结果，不控制 Docker，也不覆盖正式库。
            </p>
          </section>
        </div>
      )}
    </>
  );
}
