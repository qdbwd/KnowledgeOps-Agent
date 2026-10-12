# 0008：采用 Celery + Redis 承载后台任务

- 状态：已接受，2026-10-08。
- 用户决定：希望练习成熟任务队列、增强项目工程实践与面试解释能力；授权在判断无须 RocketMQ 时按 Celery + Redis 实施。

## 选择与理由

Celery 管理任务投递与消费，Redis 作为 broker；PostgreSQL 仍是任务、结果、事件、检查点、版本与审计的持久事实来源。消息只携带运行 ID 和调度代次，不携带文章、文件或密钥。不启用重复的 Celery 结果后端。

原 PostgreSQL 直接轮询领取方案在首版规模下可行；本次为采用成熟调度组件并学习其执行语义而调整，不将原方案描述为不规范。既有状态、授权、预算与幂等代码继续保留。

只读核对 Yufatong 本地代码确认其采用自定义 RocketMQ Celery transport、Redis result backend，并存在独立 Audio Agent MQ 消费链路。本项目没有其消息集成约束，无须复制适配层或再引入 RocketMQ。[Celery broker 支持](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/index.html)、[Redis broker](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html)

## 实现合同

- API 在同一 PostgreSQL 事务保存用户消息、运行与待投递记录；后台投递器向 Celery 发布，失败退避补发。
- Redis 开启 AOF，使用项目独立持久卷，不向宿主暴露端口；队列不使用会驱逐消息的缓存策略。
- Celery worker 首版并发为一；主 Agent 内仍允许最多两个研究子任务并发。
- 消息可能重复投递。开始执行前校验 run ID、调度代次、状态与执行者归属；文档发布依旧用事务与幂等回执保证效果。
- 服务重启将未完成任务标为中断，遗留消息不能自动启动模型调用；用户恢复后才创建新调度代次，同一 run/checkpoint 与已消费预算保留。
- 框架确认/重投递与业务恢复分别处理，不自动重试整段 Agent 任务。[Celery 任务语义](https://docs.celeryq.dev/en/stable/userguide/tasks.html)
- SSE 暂仍按 PostgreSQL 事件游标补读，不因为增加 Redis 就迁移所有状态或记忆。

## 影响与验证

独立容器组增加 redis，后端增加 celery[redis] 锁定依赖；更新启动和备份说明。需验证 Redis 不可用时任务可保留/补发、重复和旧代次消息不重复执行、重启不自动产生模型费用、手动恢复可继续。数据库基础服务和知识功能的验收不变。

取代 architecture/implementation-plan 以及导出历史方案中“不使用 Redis/Celery、直接由 PostgreSQL 调度”的部分；其他范围与 600 次真实模型额度不变。当前实现进度另见 progress。
