# 补问拦截与消息工具字段同步

`tool_call_metadata.py` 直接摘取 bytedance/deer-flow `cb90f5dca47f68fec329746a53df7b437a11c4e8` 的同名模块（Apache-2.0，许可见本目录 LICENSE），保留工具参数、原始 additional_kwargs 和供应商内容块同步裁剪，避免删除工具提议后残留协议调用。只调整本地格式，不改变算法。

本轮另在线核对 `8d994cc1e7ee63c61283a71a169248beb4ed1c52` 的 ClarificationMiddleware，同批删除逻辑与本地参考一致。其 `test_clarification_drop_siblings_graph_integration.py` 验证真实 create_agent 中的混合调用，而不只调用中间件函数。本项目沿用这一验证方式，并补直接恢复工具节点的执行拦截。

`harness/runtime/clarification.py` 是职责适配，没有导入 DeerFlow 的交互策略或应用前端。提问定义沿用 question/options/context 思路；PG 问题版本、回答去重、outbox 接续和暂停兼容由本项目运行服务实现。等待使用 LangGraph 的公开 interrupt/Command 接口，不将等待问题标作任务成功，不把回答当权限批准。暂未引入 DeerFlow 的任意动态表单组件；MCP 表单在协议接续阶段按协议类型处理。
