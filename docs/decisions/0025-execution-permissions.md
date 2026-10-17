# 0025 执行权限、审批与命令回执

日期：2026-10-14。状态：已确认，实施与验收状态见 progress。

采用会话默认请求批准/自动执行，既有能力上限、发布授权和MCP精确确认优先。规则由真人API创建，不由模型、Skill或记忆提取授权。命令匹配绑定工作区、cwd和argv；复杂语法仅逐次确认。

直接使用 LangChain 1.4.3 HumanInTheLoopMiddleware、LangGraph interrupt/Command与现有PG保存器。应用适配审批事实、执行前复核、原outbox续跑、主子运行中断识别。直接引入 tree-sitter 0.25.2、tree-sitter-bash 0.25.1（MIT许可）解析Bash，不自写Shell解析器。

参考版本与具体设计：

- [Codex 7b06e4a 的 execpolicy/rule.rs](https://github.com/openai/codex/blob/7b06e4af664049a3c1ed3113c4ee52ec3d5993f2/codex-rs/execpolicy/src/rule.rs)：PrefixPattern逐参数匹配、正反例验证的思路；Python策略为本项目适配，未复制Rust实现。
- [DeerFlow a1e3e75 授权过滤](https://github.com/bytedance/deer-flow/blob/a1e3e75b17b0fd73ba42a33b6f135f1710cadabc/backend/packages/harness/deerflow/authz/enforcement.py)：保持工具顺序、过滤有效能力、失败不扩大许可。沿用本项目tool_access/run_resources再接入执行策略，没有引入DeerFlow身份系统。
- Deep Agents保持0.7.23的文件/Shell公开工具合同，不升级整套SDK。人工介入实际接口已检查本机锁定LangChain源码及真实图行为。

命令回执与调用身份和精确参数绑定，预算与启动事实原子提交。结果保存后重放；执行后崩溃的未知结果不自动重试。用户skip返回未知而非成功，retry记新尝试并消耗预算。允许只读子角色保持只读，审批不提升能力。

不新增AIO、自动审查模型或操作系统隔离。LocalShellBackend仍在应用容器本地执行。旧会话默认请求批准，旧检查点在最终工具关口也受保护。迁移先在隔离PG验证，正常库增量升级；无损降级不能删除执行事实。

自然语言设计说明维护在architecture；完整实施与验收合同在implementation-plan。工程验证使用替身供应商，不把通过的机制测试解释为真实模型语义质量。
