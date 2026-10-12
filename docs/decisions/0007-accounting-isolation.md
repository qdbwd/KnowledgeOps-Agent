# 0007：验收额度独立于业务数据库

- 状态：已实施基础账本，2026-10-08；业务流程接入持续验证。

同一个 PostgreSQL 服务使用 knowledgeops（业务）、knowledgeops_accounting（真实模型额度/请求证据）、knowledgeops_test（离线测试）三个数据库，不增加服务或数据库产品。

Ruling: 模型总额度账本放在独立 accounting 数据库，预占先于网络调用，项目额度与根任务额度同事务锁定；业务测试/备份恢复不能清零账本。代价是需要单独备份账本，部署者不得把业务恢复误当作费用状态恢复。

所有请求含主/子 Agent、重试、摘要、裁判均走 ModelGateway；SDK max_retries=0。未知结果保留 reserved 占额，失败也计费次，不声称供应商侧恰好一次。总额度最多 600、首次 canary 最多 10，重建客户端不增加已存授权。

并发锁顺序为 scope→root；默认 scope 固定，实际应用不允许 Agent 选择 scope。测试使用隔离测试库及随机 scope，不消耗真实额度。

证据：13 项基础/账本/配置测试后追加配置错误脱敏测试；首次 canary 三次真实请求通过工具往返、流式 JSON，记录在 artifacts/verification/model-canary-2026-10-08.json。
