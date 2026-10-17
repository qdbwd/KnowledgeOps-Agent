# 0029：临时数据生命周期与会话删除边界

2026-10-16，已实施并交付。完整范围见[实施计划](../implementation-plan.md)，逐项证据与边界见[交付核对](../lifecycle-delivery.md)，实际环境状态见[进度](../progress.md)。

数据是否临时与是否持久化不是同一判断。保留内存执行对象、Redis通知/缓存、PG可靠交接/恢复和独立用户资产的分工；不迁移整张run_events，不为这轮增加数据库或服务。

根任务及依赖明确结束后，详情默认保留30天。暂停、等待、审批、未知执行结果和未消费子报告受到保护。旧终态从首次纳管给予完整宽限。清理前先解除聊天连续性和业务决定对完整日志的依赖；保留用户消息私有协议和必要简短结果，旧消息不能仅从可见文字猜出隐藏字段。

访问目录只是读取许可范围，不是永久保留引用。快照中的累积artifact_ids保持权限合同；正文、笔记和有效历史中的实际使用另登记保留关系。清理不扩大访问范围，模型或文档提到一个ID也不能因此获得读取权限。

本轮固定核对DeerFlow提交`ec07de9618b4194533fea3d8bf8a2e4fbcca5385`（2026-10-16）。[会话删除入口](https://github.com/bytedance/deer-flow/blob/ec07de9618b4194533fea3d8bf8a2e4fbcca5385/backend/app/gateway/routers/threads.py)及[对应测试](https://github.com/bytedance/deer-flow/blob/ec07de9618b4194533fea3d8bf8a2e4fbcca5385/backend/tests/test_threads_router.py)明确包含会话目录的workspace/uploads/outputs清除；本项目把它们作为独立资产，所以不复用整目录删除。借鉴的是删除前互斥、拥有者核对及不存在对象的幂等处理。

[DeerFlow trash服务](https://github.com/bytedance/deer-flow/blob/ec07de9618b4194533fea3d8bf8a2e4fbcca5385/backend/packages/harness/deerflow/projects/trash.py)把文件删除放在行保护中，文件不存在允许推进，其他错误使记录保留供重试；孤立暂存有24小时保护。本项目适配为持久阶段作业，因为业务库、模型回执库、保存器和文件系统不能共同提交。没有复制其回收站业务、引入额外SDK或把best-effort当作删除完成。

LangGraph方面直接复用项目锁定PostgresSaver的公开`delete_thread(thread_id)`；已检查安装包实现，负责checkpoints、checkpoint_blobs和checkpoint_writes。现有CachedPostgresSaver还负责该线程Redis副本失效。清理服务不得手写另一套框架表删除器，升级依赖时须复核接口和缓存合同。

会话删除先封闭所有读写，再按持久作业清理副本。长期记忆正文、学习配置、知识/版本、草稿、来源与交付文件保留，来源聊天的摘录和失效链接另处理。失败可重试，隐藏会话不算物理清理完成。迁移只增量加结构，破坏性验证只在隔离测试数据完成。
