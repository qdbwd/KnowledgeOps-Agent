# 0016：可配置子 Agent 目录与按需委派

日期：2026-10-09。用户明确要求本轮实现配置入口，并要求两参考项目从最新上游核对。取代0015中“配置入口仅后续建议”，其他默认辅助关闭、按需委派、不深入专用网络研究、权限/费用边界不变。

## 设计与范围

以现有只读委派为基础，新增Web页面、API和PostgreSQL可复用角色目录。首版单机用户通过应用管理，不提供主模型修改角色定义的工具。主Agent使用目录中启用角色，动态生成任务并委派；不是必须多Agent执行。

配置字段：稳定name、显示名、用途description、system_prompt、只读工具白名单、Skills允许列表、enabled。名称创建后不变，保留researcher内置兼容角色；自定义角色不覆盖保留名。模型固定继承已授权deepseek-flash，不新增模型渠道。只读工具从已有本地检索/材料/网页/产物/计划/辅助调用中选择；网页与辅助仍受根任务开关约束。自定义角色不额外得到MCP、发布、任意文件、脚本或递归委派权限；扩展这些能力另行设计。

PG保存当前定义与不可变修订，更新携expected_revision防止旧页面覆盖；删除为墓碑，不破坏历史。disabled角色不进入新任务目录。普通提示/显示编辑及新增技能许可供新根任务使用；禁用/删除/收窄工具或技能许可提升authorization_epoch，旧绑定在下次调用时失效，再启用也不恢复旧绑定。

根任务首次执行在原资源清单中冻结subagents列表（含定义、revision、epoch和内容）；主模型只见名称、用途和有效工具等目录摘要，子模型才接收所选角色提示。子运行输入记录所选完整冻结定义及父task调用关联，检查点与费用沿用现有实现。任务中新增角色不自动注入；新任务可用。旧资源快照没有目录时仅支持原researcher，不伪造当时已有自定义定义。

用户进一步要求优先复用：不再新增delegate_tasks协议。直接使用Deep Agents公开SubAgentMiddleware + CompiledSubAgent + RunnableLambda及标准task(description, subagent_type)，上游负责目录、参数校验、选择及Command回包。提示主Agent每轮最多发出两个task；数据库硬限制是同时运行的子任务最多两个，不把同一响应中的调用数量与实际并发数混为一谈。旧快照保留delegate_research用于旧检查点及兼容。子任务只得到主Agent提供的任务/材料及项目核心规则，不继承完整主对话；根预算、并发2、禁止递归、取消和恢复复用不变。未知、禁用、被撤回或不在冻结目录的名称由程序拒绝。具体角色不会因提示文字获得额外工具。

## 接口与模块分工

- subagent_registry.py：SubagentRegistry.list/get/create/update/history；snapshot()返回含内置researcher和启用自定义角色的完整定义，validate_binding(snapshot)复核撤回边界。工具/技能选项提供只读目录。
- GET /api/subagents、GET /api/subagents/options、POST /api/subagents、PATCH /api/subagents/{id}、GET /api/subagents/{id}/history。无动态外部连接、无模型调用。PATCH含expected_revision，删除用deleted=true。
- db.py及0014迁移：subagent_definitions / subagent_definition_revisions；保留业务与费用隔离。
- run_resources.py冻结目录；delegation.py解析所选定义并创建子运行；runtime.py应用角色提示、工具/Skills过滤以及下一调用前撤回检查。
- web/src/SubagentsPage.tsx和导航：创建、编辑、启停/删除、版本查看、加载/空/错误/冲突反馈；保留聊天中“允许研究分工”的兼容选项，显示词调整为“允许子Agent分工”。

## 依赖实现与完成证据

1. 先实现版本目录和迁移/API，测试唯一保留名称、非法工具/技能、并发修订、禁用后重新启用仍使旧绑定失效，旧数据不改。
2. 冻结目录并接入真实主/子图，测试主模型发现/选择、子工具与技能限制、上下文隔离、根开关不能绕过、恢复复用、角色禁用/删除/改版本期间的行为。
3. 实现UI并测创建编辑、冲突、开关与错误；在真实浏览器用隔离数据核验，不以源码或接口存在代替入口可用。
4. 迁移前备份，隔离迁移/恢复先验，正式正常库升级不重跑旧任务。使用现有累计1600范围完成必要的真实委派验证，调用前读取账本。新定义是否改善业务质量另行评估，不要求专用研究员胜出。
5. 合并运行受影响测试，最终全量回归；同步使用说明、实际源码参考和未完成项。无需提交或推送Git。

## 最新上游核对

2026-10-09实时GitHub API：DeerFlow main=5ecc1c2ac2de624af354bc515d5cb04f80df226c（2026-10-09T00:10:41Z）；DeepAgents main=efd88ff8fd1279361de77a83c9f75d98ed2f36d5（2026-10-08T22:39:36Z）。本轮从这两个提交读取相关源码与测试，后续相关新工作重新核对最新上游，再固定一次实现的证据；不把旧本地副本当最新，也不未经验证直接升级全部依赖。
## 复用实施约定（用户补充）

通用能力优先调用上游公开接口；能直接复用的实现不再从零重写。学习价值通过注释、数据流与适配依据体现，不以重复造轮子衡量。现有基础服务能复用时直接接入；必要自定义记录具体差距与证据。

- 直接使用已锁定Deep Agents 0.7.23的SubAgentMiddleware、CompiledSubAgent、RunnableLambda适配（RunnableLambda来自LangChain Core）。[最新源码](https://github.com/langchain-ai/deepagents/blob/efd88ff8fd1279361de77a83c9f75d98ed2f36d5/libs/deepagents/deepagents/middleware/subagents.py)及test_subagents.py中RunnableLambda/检查点测试已核对；不复制模型工具循环或自造委派参数协议。
- 上游task只传任务消息，不向Runnable提供真实tool_call_id。本项目通过公开wrap_tool_call钩子和标准ContextVar桥接；在进独立线程前取出ID，finally清理。已有RunService继续负责根预算、两个活跃子任务、独立checkpoint和恢复结果复用。适配器只返回messages及structured_response，不把子State回灌主State。
- [DeerFlow最新定义/校验](https://github.com/bytedance/deer-flow/blob/5ecc1c2ac2de624af354bc515d5cb04f80df226c/backend/packages/harness/deerflow/persistence/managed_subagents/base.py)及registry/router测试用于对齐配置字段、严格校验、启停和冲突行为。其持久层绑定自身数据库/鉴权，不整包移入；PG修订与撤回复用本项目已有MCP管理模式。
- 上游两个项目为MIT；若摘取代码保留版权、许可、提交和改动说明。通过依赖使用不另复制源码。上游main和当前wheel即使都标0.7.23，LangChain最低版本仍有差异；本轮只使用已验证存在的公开接口，不直接升级所有依赖。

以上为接入依据，离线图/浏览器/真实模型验证结果另见progress，不以静态接口存在宣称功能完成。


### Python包与SDK状态核实

2026-10-09补充核对：Deep Agents是已公开发布、当前锁文件已安装的Python库，可直接导入子Agent中间件。DeerFlow最新核对提交也已拆分`backend/packages/harness`（包名`deerflow-harness`，源码版本2.1.0），并提供`from deerflow.client import DeerFlowClient`嵌入式入口；不能说它没有Python包或必须启动整套Web应用。其官方SDK安装页仍写尚未公开发布，本次只读查询PyPI的`deerflow`与`deerflow-harness`均返回404。这是本次核对结果，不保证未来仍未发布。

依据：[SDK安装说明](https://github.com/bytedance/deer-flow/blob/5ecc1c2ac2de624af354bc515d5cb04f80df226c/frontend/src/content/en/harness/index.mdx)、[包声明](https://github.com/bytedance/deer-flow/blob/5ecc1c2ac2de624af354bc515d5cb04f80df226c/backend/packages/harness/pyproject.toml)、[嵌入式入口](https://github.com/bytedance/deer-flow/blob/5ecc1c2ac2de624af354bc515d5cb04f80df226c/README.md#embedded-python-client)。

本轮选择Deep Agents的依据是已发布依赖、所需中间件适合单独组合，以及与现有持久运行服务的适配成本。DeerFlow的Harness可独立嵌入这一事实不改变本次选择；其管理模块对自身资源、存储和权限契约的依赖仍需评估，不能因为整个SDK存在就认定任意内部模块均可直接替换本项目实现。