# 0021：Redis 实时事件桥与检查点内容缓存

日期：2026-10-10。状态：用户要求已明确，按本决策实现；验证和部署状态见 progress。

## 问题与参考

项目原本已有 SSE，模型增量和工具进度保存为 PG RunEvent，API 每半秒轮询。用户希望参照 DeerFlow 增加 Redis 流式事件传递和检查点历史缓存，正式检查点仍由底层保存器负责。不能把“已有 SSE”误写成新增流式功能，也不能把缓存误当作第二份持久真相。

核对当时最新 DeerFlow main 后固定到 `0fe6e63a68f014e931393f15e639fe0042cebd93`：

- [Redis stream bridge](https://github.com/bytedance/deer-flow/blob/0fe6e63a68f014e931393f15e639fe0042cebd93/backend/packages/harness/deerflow/runtime/stream_bridge/redis.py)：XADD/XREAD、事件保留窗口、TTL、重连缺口。
- [CachedHistorySaver](https://github.com/bytedance/deer-flow/blob/0fe6e63a68f014e931393f15e639fe0042cebd93/backend/packages/harness/deerflow/runtime/checkpointer/cached_saver.py)：只缓存按已确定 checkpoint_id 定位的封存历史，不缓存 latest 解析和目标自身尚未完成的写入。
- [Redis 缓存及测试](https://github.com/bytedance/deer-flow/blob/0fe6e63a68f014e931393f15e639fe0042cebd93/backend/tests/test_checkpoint_cache_redis.py)：数据库身份命名空间、批量读取、TTL、故障当作未命中、线程删除后清理。

DeerFlow 的这项历史缓存用于 DeltaChannel 的历史重建，Redis 实现针对异步 Gateway。本项目是同步 worker + 完整快照，直接复制不会进入有效调用路径，因此复用设计不变量，适配现有协议。没有为获得缓存而改用 delta 状态或迁移现有检查点。

## 实时事件

继续以 PG 事务分配序号并保存事件。最外层事务成功后才把同一事件放入 Redis Streams；嵌套提交先合并，任一回滚不会发布对应事件。PG 成功但 Redis 失败不撤销业务，也不把业务报成失败。

API 首次连接从 PG 追平，然后通过异步 XREAD 阻塞等待新事件，直接发送连续序号的 Redis 内容。保留原 SSE `id`/`Last-Event-ID` 整数协议，前端无需换游标。事件被裁剪、发布乱序、通知丢失或 Redis 不可用时从 PG 补齐；每约五秒核对一次持久状态，防止最后一条完成通知丢失后页面永远等候。Redis 故障时退回原半秒轮询。终态也必须读完全部事件才关闭；浏览器断开不取消运行。

这里使用 Streams 而非 Pub/Sub，是为了支持短期重读；PG 已有完整日志，因此无需另建事件可靠投递外盒或复制 DeerFlow 的前端 gap 协议。Celery 任务投递外盒不变。

## 检查点缓存

`CachedPostgresSaver` 继承锁定的 `langgraph-checkpoint-postgres 3.1.2`。写入、pending writes、迁移和历史列表继续走官方保存器；只适配 `get_tuple` 的读取路径：

1. 每次从 PG 读取真实检查点头、最新 checkpoint_id 和当前 pending_writes。
2. 对消息等复杂通道，按数据库/schema、thread、namespace、checkpoint_id、channel/version 查 Redis。
3. 命中时复用官方格式的二进制；未命中/损坏/不可用时从 PG 补读并尽力缓存。标量仍直接来自 PG 头部，反序列化仍由官方保存器负责。

缓存不会给已删除的检查点返回状态，不会用旧 pending_writes 重做已成功工具，不缓存“latest 指向谁”。按 checkpoint_id 进一步区分同 thread 删除后重建的内容。旧格式交给官方迁移路径。缓存关闭、丢失和故障都不改变业务结果；不声称 PG 不可用时能只靠 Redis 恢复。

读取适配涉及官方 PG 表结构和两个内部转换/连接方法，已明确集中在一个模块，升级 saver 时须核对。许可在 vendor/langgraph 保留。不是跨任意保存器、异步 saver 或 DeltaChannel 的通用缓存；将来采用这些机制时需重新验证。

## 配置与资源边界

复用现有 Redis 容器，Celery 用 DB 0，可丢弃加速用 DB 1；按业务数据库/schema 派生命名空间，测试用 DB 15 的随机前缀。逻辑 DB 不是内存和故障隔离。继续使用全局 noeviction，不为缓存淘汰队列任务。

默认每运行保留 1024 条、每事件至多 64 KiB、闲置 TTL 一小时。检查点缓存每数据库命名空间至多约 128 个二进制条目、每条至多 2 MiB、TTL 十五分钟，按写入顺序淘汰；跨进程保守多淘汰不影响结果。并发数仍是现有单根任务和有限子任务，不把这些初值解释为行业统一最佳值。参数与独立开关放在 Settings/.env.example，需要重启 API/worker 生效，不冻结进业务资源快照。

同一 Redis 进程故障时，事件观察和检查点读取可以回 PG，但原有 Celery 新任务投递仍受影响。缓存不备份、不接管业务事实；当前恢复脚本创建隔离数据库，自然使用新命名空间。若人工将备份覆盖回同名同 schema 的现有数据库，必须在恢复前停写并清除该命名空间的缓存，不能跨恢复保留缓存副本。

## 验证要求与性能解释

覆盖事务提交/回滚、重连分页、Stream 裁剪和缺口、Redis 读写故障、最后通知丢失、最新检查点和 pending writes 更新、损坏缓存、容量限制及真实图失败恢复。使用独立 PG schema、Redis 测试前缀和离线模型响应；不增加收费请求、不重做全套干净部署。

缓存收益须实际测量。小状态可能因额外 Redis 往返略慢，长状态的重复读取能减少 PG 二进制传输；这不同于模型推理加速，也不是整个 Web/Agent 的端到端性能结论。原始比较与范围写 progress，不能仅凭缓存开关已启用就宣称应用普遍更快。
