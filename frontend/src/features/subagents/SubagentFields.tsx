import { CapabilitySelector, type CapabilityChoice } from '../tools/CapabilitySelector';

export type Role = {
  id: string;
  name: string;
  display_name: string;
  description: string;
  system_prompt: string;
  tools: string[];
  tool_groups?: string[] | null;
  resolved_tools?: string[] | null;
  skills: string[];
  enabled: boolean;
  deleted: boolean;
  revision: number;
  authorization_epoch: number;
  builtin: boolean;
};
type Option = {
  name: string;
  description: string;
  label?: string;
  display_name?: string;
  summary?: string;
  members?: CapabilityChoice[];
};
export type Options = {
  tools: Option[];
  skills: Option[];
  tool_groups?: { id: string; name: string; tool_ids: string[]; description?: string }[];
};
export type Fields = Pick<
  Role,
  'name' | 'display_name' | 'description' | 'system_prompt' | 'tools' | 'skills'
> & { tool_groups: string[] | null };
export const emptyFields: Fields = {
  name: '',
  display_name: '',
  description: '',
  system_prompt: '',
  tools: [],
  tool_groups: null,
  skills: [],
};
export function fieldsOf(row: Role): Fields {
  return {
    name: row.name,
    display_name: row.display_name,
    description: row.description,
    system_prompt: row.system_prompt,
    tools: row.tools,
    tool_groups: row.tool_groups ?? null,
    skills: row.skills,
  };
}

function Choices({
  title,
  options,
  selected,
  disabled,
  onChange,
}: {
  title: string;
  options: Option[];
  selected: string[];
  disabled: boolean;
  onChange: (value: string[]) => void;
}) {
  return (
    <CapabilitySelector
      title={title}
      options={options.map((item) => ({
        ...item,
        id: item.name,
        display_name: item.display_name || item.label,
        detail_url:
          title === 'Skills'
            ? `/api/skills/${encodeURIComponent(item.name)}`
            : title === '工具'
              ? `/api/tools/detail?tool_id=${encodeURIComponent(`builtin:${item.name}`)}`
              : undefined,
      }))}
      selected={selected}
      disabled={disabled}
      onChange={onChange}
      description={`空列表表示不授予任何${title}；打开选择窗口可搜索或查看详情。`}
    />
  );
}

export function RoleFields({
  value,
  onChange,
  options,
  disabled,
  creating = false,
}: {
  value: Fields;
  onChange: (value: Fields) => void;
  options: Options;
  disabled: boolean;
  creating?: boolean;
}) {
  const selectedGroups =
    value.tool_groups ?? (options.tool_groups?.some((group) => group.id === 'mcp') ? ['mcp'] : []);
  return (
    <>
      <div className="two-columns grid grid-cols-2 gap-5.5 max-lg:gap-[15px] max-md:grid-cols-1">
        <label>
          稳定名称
          <input
            value={value.name}
            readOnly={!creating}
            disabled={disabled}
            required
            placeholder="例如 fact-checker"
            onChange={(event) => onChange({ ...value, name: event.target.value })}
          />
        </label>
        <label>
          显示名称
          <input
            value={value.display_name}
            disabled={disabled}
            required
            onChange={(event) => onChange({ ...value, display_name: event.target.value })}
          />
        </label>
      </div>
      {creating && (
        <p className="muted text-muted small text-xs">
          名称为 1–64
          位小写英文、数字或连字符，首位为英文或数字。创建后不可修改，删除后也不复用；显示名称可调整。
        </p>
      )}
      <label>
        用途说明
        <input
          value={value.description}
          disabled={disabled}
          required
          onChange={(event) => onChange({ ...value, description: event.target.value })}
        />
      </label>
      <label>
        角色提示
        <textarea
          rows={4}
          value={value.system_prompt}
          disabled={disabled}
          required
          onChange={(event) => onChange({ ...value, system_prompt: event.target.value })}
        />
      </label>
      <div className="two-columns grid grid-cols-2 gap-5.5 max-lg:gap-[15px] max-md:grid-cols-1">
        <Choices
          title="工具组"
          options={(options.tool_groups ?? []).map((group) => ({
            name: group.id,
            label: group.name,
            description: group.description ?? '',
            members: options.tools
              .filter((tool) => group.tool_ids.includes(`builtin:${tool.name}`))
              .map((tool) => ({ ...tool, id: tool.name })),
          }))}
          selected={selectedGroups}
          disabled={disabled}
          onChange={(tool_groups) => onChange({ ...value, tool_groups })}
        />
        <Choices
          title="工具"
          options={options.tools}
          selected={value.tools}
          disabled={disabled}
          onChange={(tools) => onChange({ ...value, tools })}
        />
        <Choices
          title="Skills"
          options={options.skills}
          selected={value.skills}
          disabled={disabled}
          onChange={(skills) => onChange({ ...value, skills })}
        />
      </div>
      {selectedGroups.includes('mcp') && (
        <p className="muted text-muted small text-xs">
          MCP 成员在新任务开始时取自已允许的只读服务目录；可在“工具与能力”查看。
          不增加外部服务或写入权限，既有任务保持原冻结范围。
        </p>
      )}
      <p className="muted text-muted small text-xs">
        工具组和单独选择的工具合并，Skills
        需另行选择。核心系统规则始终生效；联网与辅助模型还受主任务开关限制。
      </p>
    </>
  );
}
