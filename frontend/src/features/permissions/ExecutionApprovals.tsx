import { Button } from '../../shared/ui/Button';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { messageOf, send, useResource } from '../../shared/api/api';
import { ErrorBox } from '../../shared/ui/components';
import { MCPApprovals } from '../mcp/MCPPage';
import type { Receipt } from '../mcp/MCPPage';
import type { Run } from '../../shared/types/types';

type ApprovalFileTarget = {
  kind: string; path: string; bytes?: number; text?: string | null; text_complete?: boolean;
  members?: ApprovalFileTarget[];
};
type Action = {
  id: string;
  run_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  fingerprint: string;
  cwd: string;
  status: string;
  rule_candidate: { argv: string[]; cwd: string; match: string } | null;
  decision?: {
    actor?: string;
    file_target?: ApprovalFileTarget;
    review?: { result?: { verdict: string; reason: string } };
    invalidated_reason?: string;
  };
};
type Command = {
  id: string;
  status: string;
  revision: number;
  attempt: number;
  request: { args: { command: string }; cwd: string };
};
type FileReceipt = {
  id: string;
  path: string;
  operation: string;
  status: string;
  revision: number;
  matches_expected: boolean;
  current_preview?: string;
  inspection_error?: string;
};
type Approvals = { actions: Action[]; commands: Command[]; mcp: Receipt[]; files?: FileReceipt[] };
const names: Record<string, string> = {
  pending: '等待批准',
  reviewing: '正在审查',
  approved: '已批准',
  denied: '已拒绝',
  invalidated: '批准已失效，未执行',
};
const actionNames: Record<string, string> = {
  execute: '命令执行', write_file: '新建文件', edit_file: '编辑文件', delete: '删除文件',
};

function FileActionPreview({ row }: { row: Action }) {
  const target = row.decision?.file_target;
  const paths = (item: ApprovalFileTarget): string[] =>
    (item.members ?? []).flatMap(child => [child.path + (child.kind === 'directory' ? '/' : ''), ...paths(child)]);
  const members = target ? paths(target) : [];
  return <div className="my-3 space-y-3">
    <p className="break-all font-mono text-sm">{String(row.arguments.file_path ?? '')}</p>
    <p className="text-sm text-muted">
      {target?.kind === 'missing' ? '当前不存在，将创建文件。'
        : target?.kind === 'directory' ? '将删除此目录及其中内容。'
          : '此操作只适用于核对过的文件版本；文件变化后需要重新提出操作。'}
    </p>
    {row.tool_name === 'write_file' && <>
      <strong className="text-sm">待写内容</strong>
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words">{String(row.arguments.content ?? '')}</pre>
    </>}
    {row.tool_name === 'edit_file' && <div className="grid gap-3 md:grid-cols-2">
      <div><strong className="text-sm">查找内容</strong><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words">{String(row.arguments.old_string ?? '')}</pre></div>
      <div><strong className="text-sm">替换为{row.arguments.replace_all ? '（替换全部匹配）' : ''}</strong><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words">{String(row.arguments.new_string ?? '')}</pre></div>
    </div>}
    {typeof target?.text === 'string' && <details>
      <summary>查看核对时的文件内容{target.text_complete ? '' : '（部分内容）'}</summary>
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words">{target.text}</pre>
    </details>}
    {target?.kind === 'file' && row.status === 'pending' && <Link to={`/files?path=${encodeURIComponent(target.path)}`}>查看当前完整文件</Link>}
    {target?.kind === 'directory' && <details>
      <summary>查看目录范围（{members.length} 项）</summary>
      <ul className="max-h-64 overflow-auto break-all text-sm">{members.map(path => <li key={path}>{path}</li>)}</ul>
    </details>}
  </div>;
}

function ActionCard({
  row,
  ready,
  busy,
  decide,
}: {
  row: Action;
  ready: boolean;
  busy: boolean;
  decide: (row: Action, approve: boolean, scope: string, match: string) => void;
}) {
  const [match, setMatch] = useState('exact');
  const reviewDenied = row.status === 'pending' && row.decision?.review?.result?.verdict === 'deny';
  return (
    <article className="list-card flex items-center gap-[17px] rounded-[9px] border border-line bg-white px-[23px] py-4.5 text-[#5c7057] hover:border-[#aec2a5] hover:no-underline max-md:flex-wrap max-md:gap-3 max-md:p-4 [&>div]:min-w-0 [&>div]:flex-1 [&_h3]:mt-0 [&_h3]:mb-[5px] [&_h3]:text-[#344c37] [&_h3]:[overflow-wrap:anywhere] max-md:[&_h3]:text-[13px] [&_p]:m-0 [&_p]:text-[11px] [&_p]:text-[#85917e] [&_p]:[overflow-wrap:anywhere] [&>span:last-child]:text-[11px] [&>span:last-child]:whitespace-nowrap max-md:[&>span:last-child]:hidden">
      <div>
        <strong>{actionNames[row.tool_name] ?? '工具调用'} · {names[row.status] ?? row.status}</strong>
        {row.tool_name === 'execute' ? <pre>{String(row.arguments.command ?? '')}</pre>
          : ['write_file', 'edit_file', 'delete'].includes(row.tool_name) ? <FileActionPreview row={row} />
            : <pre>{JSON.stringify(row.arguments, null, 2)}</pre>}
        {row.decision?.review?.result && <p className="my-2 text-sm" role="status">
          审查说明：{row.decision.review.result.reason}
        </p>}
        {reviewDenied && <p className="my-2 text-sm">审查未批准，操作尚未执行。你可以保持拒绝，或核对后仅允许这一次；固定禁止仍不可覆盖。</p>}
        {row.decision?.invalidated_reason && <p className="my-2 text-sm">{row.decision.invalidated_reason}</p>}
        <p className="muted text-muted small text-xs">
          工作目录：{row.cwd} · <Link to={`/runs/${row.run_id}`}>查看执行者</Link>
        </p>
        {row.status === 'pending' && (
          <>
            <div className="row flex items-center gap-3 wrap flex-wrap">
              <Button disabled={busy || !ready} onClick={() => decide(row, true, 'once', 'exact')}>
                {reviewDenied ? '核对后仍允许一次' : '允许一次'}
              </Button>
              <Button
                className="secondary button-secondary"
                disabled={busy || !ready}
                onClick={() => decide(row, false, 'once', 'exact')}
              >
                {reviewDenied ? '保持拒绝' : '拒绝一次'}
              </Button>
            </div>
            {row.rule_candidate && !reviewDenied ? (
              <details className="mt-5">
                <summary>减少重复询问</summary>
                <label>
                  匹配方式
                  <select value={match} onChange={(event) => setMatch(event.target.value)}>
                    <option value="exact">仅相同命令参数</option>
                    <option value="prefix">允许在这些参数后追加参数</option>
                  </select>
                </label>
                <p>
                  仅适用于这个工作区和工作目录。
                  {match === 'prefix'
                    ? '以下参数是完整前缀，之后的参数不受这条允许规则限制。'
                    : '全部参数必须完全一致。'}
                </p>
                <pre>{JSON.stringify(row.rule_candidate.argv)}</pre>
                <div className="row flex items-center gap-3 wrap flex-wrap">
                  <Button
                    disabled={busy || !ready}
                    onClick={() => decide(row, true, 'session', match)}
                  >
                    本会话允许
                  </Button>
                  <Button
                    disabled={busy || !ready}
                    onClick={() => decide(row, true, 'persistent', match)}
                  >
                    保存为跨会话规则
                  </Button>
                </div>
              </details>
            ) : (
              <p className="muted text-muted small text-xs">
                {row.tool_name === 'execute' ? '此命令包含脚本或复杂语法，需要逐次确认。'
                  : '仅批准这一次具体操作，不保存为后续允许规则。'}
              </p>
            )}
          </>
        )}
      </div>
    </article>
  );
}

/** 展示 PG 中的原调用；浏览器只提交真人决定，不生成或恢复图状态。 */
export function ExecutionApprovals({
  run,
  refreshKey,
  onChanged,
}: {
  run: Run;
  refreshKey: string | number;
  onChanged: () => void;
}) {
  const [version, setVersion] = useState(0);
  const result = useResource<Approvals>(
    `/api/runs/${run.root_id}/approvals`,
    `${refreshKey}:${version}`,
  );
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const changed = () => {
    setVersion((value) => value + 1);
    onChanged();
  };
  async function action(path: string, body: unknown) {
    setBusy(true);
    setError('');
    try {
      await send(path, body);
      changed();
    } catch (failure) {
      setError(messageOf(failure));
      changed();
    } finally {
      setBusy(false);
    }
  }
  const paused = run.status === 'paused';
  const ready = (run.status === 'interrupted' || paused) && !run.cancel_requested;
  const unknown = result.data?.commands.filter((row) => row.status === 'unknown') ?? [];
  const files = result.data?.files ?? [];
  if (!result.data) return <ErrorBox message={result.error} />;
  const rows = result.data.actions;
  const mcpWaiting = result.data.mcp.some((receipt) => receipt.status === 'pending');
  return (
    <>
      {(rows.length > 0 || unknown.length > 0 || files.length > 0 || (ready && mcpWaiting)) && (
        <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
          <h2>执行确认</h2>
          <ErrorBox message={error || result.error} />
          <p>
            {paused
              ? '处理决定后仍保持暂停，请明确继续任务后再执行。'
              : '所有待办处理后会自动继续原任务。拒绝会交给 Agent 选择其他方案；等待期间不占用执行时间。'}
          </p>
          <p className="muted text-muted small text-xs">
            命令在应用所在容器执行。批准只控制是否启动，不提供额外的文件或网络隔离。
          </p>
          {rows.map((row) => (
            <ActionCard
              key={row.id}
              row={row}
              busy={busy}
              ready={ready}
              decide={(item, approve, scope, match) =>
                action(`/api/runs/${run.root_id}/approvals/${item.id}/decision`, {
                  expected_fingerprint: item.fingerprint,
                  approve,
                  scope,
                  match,
                })
              }
            />
          ))}
          {unknown.map((row) => (
            <article
              key={row.id}
              className="list-card flex items-center gap-[17px] rounded-[9px] border border-line bg-white px-[23px] py-4.5 text-[#5c7057] hover:border-[#aec2a5] hover:no-underline max-md:flex-wrap max-md:gap-3 max-md:p-4 [&>div]:min-w-0 [&>div]:flex-1 [&_h3]:mt-0 [&_h3]:mb-[5px] [&_h3]:text-[#344c37] [&_h3]:[overflow-wrap:anywhere] max-md:[&_h3]:text-[13px] [&_p]:m-0 [&_p]:text-[11px] [&_p]:text-[#85917e] [&_p]:[overflow-wrap:anywhere] [&>span:last-child]:text-[11px] [&>span:last-child]:whitespace-nowrap max-md:[&>span:last-child]:hidden"
            >
              <div>
                <strong>执行结果未知 · 第 {row.attempt} 次尝试</strong>
                <pre>{row.request.args.command}</pre>
                <p>工作目录：{row.request.cwd}</p>
                <p>
                  命令可能已产生修改，但结果没有完整保存。请先检查文件或外部结果，再选择；重试可能重复产生副作用。
                </p>
                <div className="row flex items-center gap-3 wrap flex-wrap">
                  <Button
                    disabled={busy || !ready}
                    onClick={() =>
                      action(`/api/runs/${run.root_id}/commands/${row.id}/resolve`, {
                        expected_revision: row.revision,
                        decision: 'skip',
                      })
                    }
                  >
                    {paused ? '已核对，跳过（保持暂停）' : '已核对，跳过并继续'}
                  </Button>
                  <Button
                    className="secondary button-secondary"
                    disabled={busy || !ready}
                    onClick={() =>
                      action(`/api/runs/${run.root_id}/commands/${row.id}/resolve`, {
                        expected_revision: row.revision,
                        decision: 'retry',
                      })
                    }
                  >
                    已核对，明确重试一次
                  </Button>
                </div>
              </div>
            </article>
          ))}
          {files.map((row) => (
            <article
              key={row.id}
              className="list-card flex items-center gap-[17px] rounded-[9px] border border-line bg-white px-[23px] py-4.5 text-[#5c7057] hover:border-[#aec2a5] hover:no-underline max-md:flex-wrap max-md:gap-3 max-md:p-4 [&>div]:min-w-0 [&>div]:flex-1 [&_h3]:mt-0 [&_h3]:mb-[5px] [&_h3]:text-[#344c37] [&_h3]:[overflow-wrap:anywhere] max-md:[&_h3]:text-[13px] [&_p]:m-0 [&_p]:text-[11px] [&_p]:text-[#85917e] [&_p]:[overflow-wrap:anywhere] [&>span:last-child]:text-[11px] [&>span:last-child]:whitespace-nowrap max-md:[&>span:last-child]:hidden"
            >
              <div>
                <strong>
                  文件操作待核对 · {row.status === 'conflict' ? '内容冲突' : '结果未知'}
                </strong>
                <p>
                  {row.path} · {row.operation}
                </p>
                <p>
                  不会自动覆盖文件。保留现状并跳过会把未确认结果交给
                  Agent；重新核对只检查当前内容是否等于原预期，再补齐回执和知识同步。
                </p>
                {row.current_preview && (
                  <details className="mt-5">
                    <summary>查看当前文件片段</summary>
                    <pre>{row.current_preview}</pre>
                  </details>
                )}
                <p className="muted text-muted small text-xs">
                  {row.inspection_error ||
                    (row.matches_expected
                      ? '当前内容与预期一致，可以重新核对。'
                      : '当前内容不等于预期结果；可以保留现状跳过，或在外部核对后刷新。')}
                </p>
                <div className="row flex items-center gap-3 wrap flex-wrap">
                  <Button
                    disabled={busy || !ready}
                    onClick={() =>
                      action(`/api/runs/${run.root_id}/files/${row.id}/resolve`, {
                        expected_revision: row.revision,
                        decision: 'skip',
                      })
                    }
                  >
                    {paused ? '保留现状，跳过（保持暂停）' : '保留现状，跳过并继续'}
                  </Button>
                  <Button
                    className="secondary button-secondary"
                    disabled={busy || !ready || !row.matches_expected}
                    onClick={() =>
                      action(`/api/runs/${run.root_id}/files/${row.id}/resolve`, {
                        expected_revision: row.revision,
                        decision: 'verify',
                      })
                    }
                  >
                    重新核对（不改文件）
                  </Button>
                </div>
              </div>
            </article>
          ))}
          {ready && (
            <Button
              className="secondary button-secondary"
              disabled={busy}
              onClick={() => action(`/api/runs/${run.root_id}/cancel`, {})}
            >
              停止本次任务
            </Button>
          )}
        </section>
      )}
      <MCPApprovals
        rootId={run.root_id}
        refreshKey={`${refreshKey}:${version}`}
        receipts={result.data.mcp}
        onChanged={changed}
      />
    </>
  );
}
