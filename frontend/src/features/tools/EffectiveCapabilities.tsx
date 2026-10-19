import { useState } from 'react';
import { useResource } from '../../shared/api/api';
import { Button } from '../../shared/ui/Button';
import { Dialog } from '../../shared/ui/Dialog';
import { ErrorBox, Loading } from '../../shared/ui/components';
import { capabilityName, type CapabilityChoice } from './CapabilitySelector';
import { CapabilityDetails } from './CapabilityDetails';

type EffectiveTool = CapabilityChoice & {
  available: boolean;
  unavailable_explanations?: string[];
  requires_approval?: boolean;
};
type Preview = {
  policy_revision: number;
  tools: EffectiveTool[];
  skills: { available: boolean; explanation: string };
};

/** 可用性仍由服务器计算；按需查看保存结果，不在主页面重复铺开完整目录。 */
export function EffectiveCapabilities({
  roleId,
  version = 0,
  initialOpen = false,
}: {
  roleId?: string;
  version?: number;
  initialOpen?: boolean;
}) {
  const [open, setOpen] = useState(initialOpen);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');
  const [knowledge, setKnowledge] = useState(false);
  const [auxiliary, setAuxiliary] = useState(false);
  const [delegation, setDelegation] = useState(true);
  const [detail, setDetail] = useState<CapabilityChoice | null>(null);
  const params = new URLSearchParams({
    knowledge_only: String(knowledge),
    auxiliary: String(auxiliary),
    delegation: String(delegation),
  });
  if (roleId) params.set('role_id', roleId);
  const result = useResource<Preview>(open ? `/api/tools/effective?${params}` : null, version);
  const rows =
    result.data?.tools.filter(
      (item) =>
        `${item.name} ${capabilityName(item)} ${item.description}`
          .toLowerCase()
          .includes(query.toLowerCase()) &&
        (filter === 'all' || item.available === (filter === 'available')),
    ) ?? [];
  return (
    <>
      <Button type="button" className="secondary button-secondary" onClick={() => setOpen(true)}>
        查看有效工具
      </Button>
      {open && (
        <Dialog
          title={roleId ? '角色的有效工具' : '主 Agent 的有效工具'}
          onClose={() => setOpen(false)}
        >
          <p className="mt-0 text-sm text-muted">
            按已保存配置与下列任务选项计算；未保存的编辑不影响这里。实际任务仍会校验最新许可和操作条件。
          </p>
          <div className="mb-4 flex flex-wrap gap-4">
            <label className="m-0 flex-row items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={knowledge}
                onChange={(e) => setKnowledge(e.target.checked)}
              />
              仅知识库
            </label>
            <label className="m-0 flex-row items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={auxiliary}
                onChange={(e) => setAuxiliary(e.target.checked)}
              />
              允许辅助模型
            </label>
            {!roleId && (
              <label className="m-0 flex-row items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={delegation}
                  onChange={(e) => setDelegation(e.target.checked)}
                />
                允许子 Agent 分工
              </label>
            )}
          </div>
          <div className="flex flex-wrap gap-3">
            <input
              className="min-w-0 flex-1"
              type="search"
              aria-label="搜索有效工具"
              placeholder="搜索工具"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <select
              className="w-auto!"
              aria-label="可用状态"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            >
              <option value="all">全部状态</option>
              <option value="available">可用</option>
              <option value="unavailable">不可用</option>
            </select>
          </div>
          {result.loading && <Loading />}
          <ErrorBox message={result.error} />
          {result.data && !result.loading && !result.error && (
            <>
              <p className="text-xs text-muted">
                当前配置修订 {result.data.policy_revision} · 可用{' '}
                {result.data.tools.filter((item) => item.available).length} 项
              </p>
              <p className="text-xs text-muted">
                Skills 按需读取：{result.data.skills.available ? '可用' : '不可用'}。
                {result.data.skills.explanation}
              </p>
              <ul className="grid list-none grid-cols-2 gap-3 p-0 max-md:grid-cols-1">
                {rows.map((item) => (
                  <li key={item.id} className="rounded-xl border border-line p-3">
                    <div className="flex items-start justify-between gap-3">
                      <strong className="text-sm">{capabilityName(item)}</strong>
                      <span className="shrink-0 text-xs text-muted">
                        {item.available ? '可用' : '不可用'}
                      </span>
                    </div>
                    <p className="line-clamp-2 text-xs text-muted">
                      {item.summary || item.description}
                    </p>
                    {item.unavailable_explanations?.map((reason) => (
                      <p className="text-xs text-[#965b26]" key={reason}>
                        {reason}
                      </p>
                    ))}
                    {item.requires_approval && <p className="text-xs">每次调用需确认</p>}
                    <button
                      type="button"
                      className="text-xs text-brand"
                      onClick={() => setDetail(item)}
                    >
                      查看{capabilityName(item)}详情
                    </button>
                  </li>
                ))}
              </ul>
              {!rows.length && <p>没有匹配的工具。</p>}
            </>
          )}
        </Dialog>
      )}
      {detail && <CapabilityDetails choice={detail} onClose={() => setDetail(null)} />}
    </>
  );
}
