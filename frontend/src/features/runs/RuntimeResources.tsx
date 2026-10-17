/** 冻结清单表示本次可用资源；实际读取与调用仍以事件和回执为准。 */
export type RuntimeManifest = {
  content_hash: string;
  content: {
    format_version: number;
    policy: { version: string; sha256: string; resource: string };
    skills: { name: string; version: string; resource: string; sha256: string }[];
    preferences: { id: string; revision: number; content: string; scope: string; topic: string }[];
    mcp: {
      id: string;
      name: string;
      revision: number;
      tools: { name: string; permission: string }[];
    }[];
    model: { provider: { model: string }; capacity: { configured_window?: number } };
  };
};
export type Continuation = {
  id: string;
  run_id: string;
  generation: number;
  covered_sequence: number;
  content_hash: string;
};

export function RuntimeResources({
  resources,
  continuation,
}: {
  resources?: RuntimeManifest | null;
  continuation?: Continuation | null;
}) {
  return (
    <section className="card">
      <h2>本次运行依据</h2>
      {!resources ? (
        <p className="muted">
          此运行尚未冻结资源，或属于缺少资源清单的历史格式；不推测当时使用的配置。
        </p>
      ) : (
        <>
          <p>
            模型：{resources.content.model.provider.model} · 核心知识库规则：
            {resources.content.policy.version}
          </p>
          <p className="muted">
            以下是首次执行时冻结的版本。普通编辑从新任务生效，撤回许可在下一相关调用前检查。
          </p>
          <details>
            <summary>Skills、偏好和服务配置</summary>
            <h3>按需可读的 Skills</h3>
            <ul>
              {resources.content.skills
                .filter((item) => item.resource.endsWith('/SKILL.md'))
                .map((item) => (
                  <li key={item.resource}>
                    {item.name} · {item.version}
                  </li>
                ))}
            </ul>
            <p className="muted">
              已装载目录不代表已使用某项技能；实际读取见下方事件中的 skill_read 记录。
            </p>
            <h3>冻结时有效的偏好</h3>
            {resources.content.preferences.length ? (
              <ul>
                {resources.content.preferences.map((item) => (
                  <li key={item.id}>
                    {item.content} · 修订 {item.revision}
                    {item.topic && ` · 主题：${item.topic}`}
                  </li>
                ))}
              </ul>
            ) : (
              <p>无</p>
            )}
            <h3>MCP 连接</h3>
            {resources.content.mcp.length ? (
              <ul>
                {resources.content.mcp.map((item) => (
                  <li key={item.id}>
                    {item.name} · 修订 {item.revision} ·{' '}
                    {item.tools.filter((tool) => tool.permission !== 'deny').length} 个已许可工具
                  </li>
                ))}
              </ul>
            ) : (
              <p>无</p>
            )}
            <p className="muted">清单哈希：{resources.content_hash}</p>
          </details>
        </>
      )}
      {continuation ? (
        <p>
          接续运行 {continuation.run_id} 的第 {continuation.generation} 代结果，覆盖到用户消息序号{' '}
          {continuation.covered_sequence}。本任务重新拥有独立权限与预算。
        </p>
      ) : (
        <p className="muted">没有绑定前序延续快照。</p>
      )}
    </section>
  );
}
