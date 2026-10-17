# 文件读取版本门的来源与改造

参考/适配来源：bytedance/deer-flow，提交 `8d994cc1e7ee63c61283a71a169248beb4ed1c52`，`backend/packages/harness/deerflow/agents/middlewares/read_before_write_middleware.py`，Apache-2.0（本目录 LICENSE）。

本项目在 `harness/sandbox/files.py` 适配了读取 ToolMessage 附加版本标记、逆序查找最近标记、对检查和修改共同加锁的同步拦截流程。保留 Deep Agents 的工具定义、参数校验、结果类型和精确字符串替换算法；没有导入 DeerFlow 的沙盒、配置、异步锁及授权运行环境。

有意调整：标记针对真实模型工作消息视图，旧历史仍在数据库但被压缩后不能放行；加入工作区身份与最近操作身份，相同内容的成功写入也使旧读取失效；文件检查失败关闭写入，区别于上游失败放行。目录删除要求目录列表与其中各文件的读取标记，路径互斥覆盖祖先/后代关系。

新增的 PG 修改回执、不可变预期内容、原子替换/移出目录、恢复核对与知识同步身份是本项目应用层适配。程序内锁不限制外部编辑器，Shell 仍走原命令审批和回执，不声称解析任意脚本的文件副作用。
