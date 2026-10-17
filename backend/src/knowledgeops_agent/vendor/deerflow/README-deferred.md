# 延迟工具目录与 Schema 过滤

来源为 DeerFlow 固定提交 `8d994cc1e7ee63c61283a71a169248beb4ed1c52`，MIT 许可见 LICENSE。

- [tools/builtins/tool_search.py](https://github.com/bytedance/deer-flow/blob/8d994cc1e7ee63c61283a71a169248beb4ed1c52/backend/packages/harness/deerflow/tools/builtins/tool_search.py)：摘取目录哈希、精确名称选择及有界关键词覆盖/名称优先排序。保持最多5项关键词匹配、256字符/16词限制。此应用不提供正则查询与上游专用自动路由，普通查询明确按关键词解释；因此不引入其正则超时执行依赖。
- [deferred_tool_filter_middleware.py](https://github.com/bytedance/deer-flow/blob/8d994cc1e7ee63c61283a71a169248beb4ed1c52/backend/packages/harness/deerflow/agents/middlewares/deferred_tool_filter_middleware.py)：摘取模型绑定过滤、执行前拒绝与同步/异步公开钩子。改动为状态字段 promoted_tools、附加 Run 身份、中文反馈、使用本地工具目录；并行搜索归并由本地状态 reducer 完成。
- 对照上游 `backend/tests/test_deferred_catalog.py` 的空输入、关键词查询、精确选择和目录变化，以及 `test_deferred_tool_crosscontext.py` 的跨上下文隔离及权限过滤。本地另验撤回、子角色上限、旧快照与有界输入；上游的正则查询不属于本地接口。

PG 权限/撤回、当前角色、共享预算与原 MCPCalls 执行服务仍是本项目的控制入口。搜索只查已经冻结的目录，不安装服务、不发现新权限、不调用路由模型。绑定目录哈希不能替代权限检查。
