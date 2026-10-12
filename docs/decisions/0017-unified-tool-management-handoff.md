# 0017：统一工具管理与 Agent 工具组的实施交接

日期：2026-10-09。状态：**已实现并交付，设计与开工时的调查保留于此，完成范围及验证见progress**。

用户提出下一轮统一后端工具管理、参考 DeerFlow 的 `tool_groups`，并考虑前端管理；随后明确要求不要长期只在聊天中保留为建议、遗忘已经调查的实现方法。本记录把调查转成可接续的任务。唯一任务入口仍为实施计划第七部分。

执行顺序已具体化：当前已启动的有界诊断与后端回归收束后，下一开发块直接进入本项，不以所有真实模型语义问题先解决为前置。原优化验收要求、失败记录和费用边界保留，不能用新增功能替代未通过的验收。这里明确的是开工顺序，不代表管理功能已经实现。

## 要实现的结果

保留现有 MCP 和子 Agent 管理模块，在其上提供统一工具目录、开关与 Agent 能力选择。主 Agent 和现有子 Agent 可按工具组选择能力；实际传给模型的工具以及程序实际允许执行的工具遵循同一有效策略。前端复用现有页面，补齐统一入口及选择界面。

这不是只增加一个列表接口。目录、构图、由中间件提供的工具、运行中撤回和恢复都要接上，否则会出现页面已关闭而模型仍能调用的矛盾。不重写工具执行、MCP 协议或子 Agent 循环，不引入新服务。

## 调查时核实的项目事实与改动目标

| 位置 | 已有职责 | 需要调整的地方 |
|---|---|---|
| `src/knowledgeops_agent/runtime.py::build_agent` | 本地工具列表、根/子角色、联网及辅助模型选择、MCP 装配 | 将分散选择汇入一个装配入口，保留工具函数和业务服务 |
| `mcp_registry.py` / `mcp_calls.py` | 连接、发现、许可、修订、撤回代次、真实调用和回执 | 为统一目录提供来源和许可视图；继续作为 MCP 配置的唯一事实源 |
| `subagent_registry.py` | 角色修订、启停、固定工具白名单和选项 | 复用统一工具目录，增加工具组选择；旧 `tools` 配置保持兼容 |
| `subagent_runtime.py` | Deep Agents `SubAgentMiddleware` / `CompiledSubAgent` / `task` | 工具组不替换委派机制；角色许可仍受根任务限制 |
| `skill_resources.py` | Deep Agents Skills 与仅 `read_file` 的文件中间件 | 中间件提供的工具必须纳入目录和装配检查，不只过滤 `tools=` 参数 |
| `run_resources.py` | 根任务不可变资源快照、验证及撤回 | 冻结实际有效工具、配置修订与来源；恢复检查撤回，不动态扩大旧任务权限 |
| `api.py`、`web/src/MCPPage.tsx`、`web/src/SubagentsPage.tsx` | 已有 MCP/角色管理 API 和 UI | 新统一入口复用既有服务和页面，不维护第二份 MCP/角色状态 |

上述表格记录调查时的职责和改动目标，不是当前待实施清单。共同目录、配置与分组、模型/工具调用控制、撤回、有效预览和管理页面均已接入；完成证据见[进展](../progress.md)。自定义子 Agent 只允许受控只读工具，不额外继承 MCP、发布或递归权限；增加工具组不能默默改变这一边界。

## 上游调查索引：后续从这里接着做

本次通过 GitHub API 核对：DeerFlow main=`5ecc1c2ac2de624af354bc515d5cb04f80df226c`（2026-10-09T00:10:41Z）；Deep Agents main=`efd88ff8fd1279361de77a83c9f75d98ed2f36d5`。这是本次固定证据；实际实施时检查相关文件的新提交差异即可，不必重新从 README 开始调查。

实施时增量复核：DeerFlow推进至`02ce9ab2832965eb3534e481e320d72f38bd5aec`（2026-10-09T07:33:58Z），Deep Agents仍为上述提交。DeerFlow差异涉及子任务批次取消清理及相关测试，不改变本表所用工具配置、组装和中间件工具收集文件；保留下列最初固定链接，不将上游修复误称为本项目变更。另核对Skills激活/工具策略中间件，确认技能说明加载、技能使用的文件工具及许可收窄属于不同职责。本项目继续直接依赖已锁定公开SDK，仅适配来源目录，不升级依赖或复制整套DeerFlow管理代码。

| 参考源码/测试 | 已核对的设计及本项目采用方式 |
|---|---|
| [DeerFlow ToolConfig](https://github.com/bytedance/deer-flow/blob/5ecc1c2ac2de624af354bc515d5cb04f80df226c/backend/packages/harness/deerflow/config/tool_config.py) | 工具具有 name、group、use；借鉴稳定目录及分组，不让管理 API 任意导入 Python 对象 |
| [get_available_tools](https://github.com/bytedance/deer-flow/blob/5ecc1c2ac2de624af354bc515d5cb04f80df226c/backend/packages/harness/deerflow/tools/tools.py) | 配置工具按 groups 筛选，MCP、内置/子任务工具另行装配；不能误称 `tool_groups` 自动统一控制所有来源 |
| [AgentConfig](https://github.com/bytedance/deer-flow/blob/5ecc1c2ac2de624af354bc515d5cb04f80df226c/backend/packages/harness/deerflow/config/agents_config.py) | `tool_groups`、`mcp_plugins`、`allowed_subagents` 分别表达不同选择；工具选择不能代替宿主授权 |
| [授权过滤测试](https://github.com/bytedance/deer-flow/blob/5ecc1c2ac2de624af354bc515d5cb04f80df226c/backend/tests/test_authorization_tool_filter.py) | 核对了禁止工具过滤、保留顺序、关闭授权时兼容、失败拒绝等测试；借鉴构建阶段筛选和执行阶段检查的分工，不引入完整多租户 RBAC |
| [中间件声明工具测试](https://github.com/bytedance/deer-flow/blob/5ecc1c2ac2de624af354bc515d5cb04f80df226c/backend/tests/test_middleware_declared_tools.py) | 核对声明工具收集、顺序、非法声明等测试及文件合同；`task`、`read_file` 不能成为统一开关的遗漏路径。本次没有运行上游测试或审读整份 871 行文件 |
| [能力中心](https://github.com/bytedance/deer-flow/blob/5ecc1c2ac2de624af354bc515d5cb04f80df226c/frontend/src/components/workspace/capabilities/capability-center.tsx) / [Agent 能力选择](https://github.com/bytedance/deer-flow/blob/5ecc1c2ac2de624af354bc515d5cb04f80df226c/frontend/src/components/workspace/agents/agent-capability-selection.tsx) | 中心集中工具集成、Skills、扩展；Agent 选择 MCP 与 Skills。借鉴组织和选择方式，不声称它已有我们设想的所有本地工具开关页 |
| [Deep Agents 子 Agent 中间件](https://github.com/langchain-ai/deepagents/blob/efd88ff8fd1279361de77a83c9f75d98ed2f36d5/libs/deepagents/deepagents/middleware/subagents.py) | 沿用0016已接入并验证的公开接口；本任务不另写子 Agent 循环，也不要求升级已锁定的0.7.23依赖 |

初次调查是静态源码与相关测试片段核对，没有启动参考项目的UI或运行上游测试；之后本项目的实施验证单独记录在progress。直接使用公开依赖；若确需摘取源码，记录许可、提交、摘取范围和必要改动。

## 推荐实现及需要保留的语义

配置与执行集成时再次核对上游：DeerFlow固定到`3e5bd081db383b57e4f20a6a67efdc16fe72dd98`，Deep Agents固定到`a2ad7b8b2c4db7865db31825eb238f1f279e63e6`。逐文件比较DeerFlow的`config/tool_config.py`、`tools/tools.py`、`agents/middlewares/tool_declarations.py`以及Deep Agents的`middleware/subagents.py`、`middleware/filesystem.py`，与此前各自固定参考一致；这只说明所比较的五个文件，不代表两个仓库整体没有更新。继续使用锁定的Deep Agents 0.7.23，没有随参考版本自动升级依赖。动态可见性复用[LangChain公开模型中间件的request.override(tools=...)](https://docs.langchain.com/oss/python/langchain/middleware/custom#dynamically-selecting-tools)，执行检查复用原RuntimeControl工具钩子，不修改框架源码。

1. 新增一个小型统一目录/策略模块（候选名 `tool_registry.py`），定义稳定工具 ID、模型调用名、来源、说明、分组、读写性质和依赖。工具实现继续在原模块；数据库不保存 Python 函数或可执行导入路径。MCP 使用连接 ID 与原始工具名标识身份，不能只靠可能碰撞的显示名。
2. 新增或复用 PG 修订配置保存本地工具启停和分组选择；MCP、子 Agent 的原记录继续是唯一事实源。统一入口对其调用现有服务，不另复制一份 enabled/permission。更新携带 `expected_revision`，旧页面不能覆盖新配置。
3. 把工具组作为批量选择能力的方式，最终解析成明确工具集合，再与全局启用、根任务选项、角色上限和业务权限求交。候选组为知识读取、材料读取、知识维护、网页获取、辅助模型、子任务委派；展示名与稳定标识分开，具体标识实施时统一固定。
4. 旧角色的显式 `tools` 继续保持原有权限，不因新增工具或新组自动扩权。新配置区分未设置（兼容继承）、空选择与指定组；解析结果写资源快照。工具组及 MCP 选择应分开表达，不能通过网页组顺便启用所有外部服务。
5. 主 Agent 使用当前默认角色的配置即可，不为此建立另一套 Agent 平台。子 Agent 保留只读/无递归、根预算及并发边界。辅助默认关闭、仅知识库禁止联网/MCP等现有约束继续生效。
6. 装配器统一考虑普通工具、MCP、委派和技能中间件，使用公开配置或模型请求中间件控制最终工具 Schema，并在执行前复核。关闭意味着后续请求不再声明该工具；旧消息中存在工具名字不等于继续授权，不删除历史掩盖事实。
7. 普通新增和配置编辑从新任务生效；禁用/许可收窄在下一相关调用前检查。重新启用不能复活已撤回的旧绑定，恢复不能绕过检查；正在执行或已经提交的动作不承诺撤销。保留历史输出与准确停止原因。
8. 必需工具依赖需要显式表达：例如启用渐进 Skills 却禁止 `read_file` 时，拒绝矛盾配置或明确说明该能力不可用，不能隐藏失败。核心领域规则、业务授权、预算与审计不是可由模型关闭的普通工具。
9. 统一 API 返回目录、有效工具及不可用原因；写操作走上述现有服务。前端增加“工具与能力”入口，复用 MCP/子 Agent 页面，增加分组选择和有效结果预览。页面开关不直接成为任意业务操作授权。

本轮常规实现取舍：本地策略服务为`tool_policy.py`，运行接入为`tool_access.py`。主角色选择与全局启停分层：主角色只选task仍可把读操作交给具有独立只读工具组的子角色；全局禁用及根任务联网/辅助选项则约束主子双方。组成员在保存选择时展开；省略字段不改，null恢复继承，显式空列表不选。关闭read_file采用上面第8项允许的“明确不可用”方案：不安装渐进Skills加载链并给模型不可用说明，核心领域规则照常加载。配置普通扩充只影响新绑定，关闭/收窄以逐工具代次使旧许可失效；重新启用不复活旧绑定，也不清空会话记忆。正常环境尚未部署这些代码，具体通过证据及未完成UI统一记在progress。

这些条目描述完整实现目标，不能当作所有接口已经交付。若执行中发现影响产品行为的选择，再给出具体差异；不应因“尚未写最终接口名”而把整项任务长期退回泛泛建议。

## 实施顺序与验收

UI实施时再次核对最新上游，DeerFlow固定为`db1e0e10b43e9822888cbda68185309d1d5c51c7`，Deep Agents仍`a2ad7b8b2c4db7865db31825eb238f1f279e63e6`。完整阅读DeerFlow当前`capability-center.tsx`（120行）和`agent-capability-selection.tsx`（96行）：借鉴统一入口复用既有页面、null/显式空选择区分、加载错误及已失效选择保留。上游能力中心并不等同于本项目的全部本地工具开关，因此只借鉴组织与交互设计，不复制组件，也不随参考源码更新已锁定SDK。

本项目常规UI取舍：`/tools`集中展示，原`/mcp`与`/subagents`保留；预览由服务端共用约束生成，明确为已保存配置的新任务视图。前端不连接MCP、不启动Agent来计算预览。只有选择字段变化才提交该字段，避免无关编辑重新展开工具组；冲突保留本地值，可显式放弃并刷新。角色工具组保存在原配置修订，不添加第二套角色存储。正常环境已迁移0015并部署；独立T01已取得受限工具及真实委派/恢复轨迹，正文审读未接受，不能扩成整个Goal完成。具体报告与失败记录统一见progress。

| 顺序 | 产物与落点 | 完成证据 |
|---|---|---|
| 1 | 统一目录及来源适配；抽取现有重复选项 | 默认配置下主/子实际工具 Schema 与改造前一致；覆盖中间件工具和名称冲突 |
| 2 | PG配置/修订及查询更新 API；角色工具组兼容 | 旧配置升级不扩权，空选择/继承可区分；修订冲突、未知组/工具和依赖矛盾明确失败 |
| 3 | `build_agent`、子角色、MCP和中间件统一装配 | 最终模型请求精确包含可用工具，关闭项不可见；辅助/联网/子角色等组合约束实际生效 |
| 4 | 快照、撤回和执行检查 | 停用后旧提议/检查点不执行；重新启用不复活旧许可；MCP回执、子Agent恢复和发布幂等不回退 |
| 5 | 统一入口及现有页面增量 UI | 创建/修改/启停、空/加载/失败/冲突、不可用原因和组展开预览；浏览器确认刷新后配置与后端一致 |
| 6 | 受影响回归、必要真实模型验证、文档收束 | 一条受限工具任务及按需委派真实轨迹；先离线验证不可见/不可执行，不用付费模型证明确定性拒绝；正常数据、额度和默认模型关闭保持 |

所有收费验证继续使用当时已授权的累计账本；本交接不增加额度、不授权安装新 MCP 服务、任意脚本、递归委派、提交或推送。具体下一轮实现及通过证据记入 progress，不以本文存在认定已交付。
