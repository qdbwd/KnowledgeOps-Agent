# 临时数据生命周期与清理机制交付核对

2026-10-16。本轮A–F实现、正常库增量迁移及L01—L12核对已完成。范围以[实施计划](implementation-plan.md)为准；连贯的开发实现正文在[架构说明](architecture.md#数据分别存在哪里以及怎样关联)，逐表设计在[数据库说明](database-design.md)，逐案例最终证据在[lifecycle-delivery.json](../artifacts/verification/lifecycle-delivery.json)。

## 这次实际改变了什么

以前数据主要围绕“怎样可靠地执行、恢复和展示任务”保存，任务结束之后缺少统一的退出过程。这次补的是这个过程：纯内存执行对象和可重建的Redis缓存沿用已有释放方式，PG中的完整运行轨迹则获得明确的保留期。程序先确认根任务和依赖已经结束，再开始默认30天的倒计时。暂停、待回答、待审批、未消费子结果和结果未知都不满足条件；旧记录首次纳管获得完整宽限，不按旧完成时间立即删除。

能够删日志，前提是聊天不再依赖它。最终答复需要的真实模型消息协议现在与可见聊天消息单独绑定，必要用量和交付引用进入简短结果。清理后仍能继续这个会话，但不能原地恢复已过期的旧Run，不能导出一份假称完整的执行报告。当前延续快照、可恢复任务绑定的快照、可达历史原话和真正被使用的内部材料继续保留；快照里的材料访问目录本身不再等于永久保存义务。

这里需要判断“谁还在使用内容”，而不是只判断“内容多旧”。聊天正文、任务笔记、草稿、知识资产、有效历史、主图及子图检查点和未合并的写入都能形成保留引用；引用随业务事务维护。清理还核对工作文件中的显式内部ID和路径。没有存活拥有者的两份材料相互引用不会永久留下；原引用后来消失，日常维护可以再次检查已过期任务遗留的材料。

跨存储清理没有一个总事务。因此CleanupJob只保存对象、阶段、游标和简短错误：先在业务库封闭旧任务，再封闭独立模型预算身份，之后处理保存器、完整回执、记录、文件和缓存。每个阶段可重入，已经删除的对象按成功处理，其他错误保留阶段重试。文件删除只接受程序拥有的精确路径并校验内容，拒绝符号链接和外部改写。命令输出、上传和原子写入暂存另用登记及操作系统文件锁管理，超过24小时也必须先确认没有写入者和恢复依赖。

删除会话走同一套机制，但所有权不同：用户确认预览后立即封闭聊天读写，再后台清理消息、摘要、历史副本、恢复和调用详情。上传资料、正文及版本、草稿、独立文件、长期记忆正文和学习配置继续存在。记忆的原话摘录和来源消息链接会清除，学习任务解除会话关联并停止自动推进；页面显示真实的清理进度，失败可以重试。重复删除不会重新执行Agent，也不会要求还保留已过期的清理作业才能识别“已删除”。

以上采用方式在[决策0029](decisions/0029-data-lifecycle.md)逐项说明。LangGraph检查点删除直接使用公开PostgresSaver接口；Redis、MCP作用域关闭、文件服务、控制锁和API维护循环复用本项目已有模块。DeerFlow提供所有权、并发保护、暂存宽限及失败重试的参考；本项目适配了两套PG数据责任及独立资产关系，没有复制其会话整目录删除或新增回收站服务。

## L01—L12核对入口

下面逐项列出完成的行为和证据。报告均在artifacts/verification；完整批次与定向修正的关系见下文，原始失败报告保留。表中的实现期报告与最终逐案例索引共同使用，不能仅凭某个旧报告判断当前状态。

| 条件 | 实现与验证重点 | 证据入口 |
|---|---|---|
| L01 数据清单 | 64张业务表、5张回执表、保存器表、Redis键、内存对象及受管文件有归属和保留规则；正常库实际列、关系、索引与文档表名已核对。 | database-design.md；lifecycle-schema-inventory.json |
| L02 资格与期限 | 只读预览、首次纳管宽限、恢复后重新计时；暂停、unknown、交接依赖保护。 | lifecycle-eligibility.xml；lifecycle-settlement-regression.xml |
| L03 连续性 | 回执删除后聊天私有协议仍可重建；当前快照、历史、检查点实际引用及pending_writes保护。 | lifecycle-continuity.xml；lifecycle-preparation.xml；lifecycle-retention-edges.xml |
| L04 交接与防重 | 子结果持久消费后才可清；封闭根预算和Run拒绝旧消息、旧调用重放；输入缩减仍保留提交指纹。 | lifecycle-ledger.xml；lifecycle-continuity.xml；lifecycle-materials-integration.xml |
| L05 分阶段失败 | 检查点、缓存、文件删除及事务故障可重试；领取代次保护；完成作业到期回收，尚被父作业等待的子凭据例外保留。 | lifecycle-detail-stages.xml；lifecycle-retention-edges.xml；lifecycle-jobs-regression.xml |
| L06 并发 | 删除与发消息共用接受边界，晚到记忆提取拒绝，来源/配置编辑先取控制锁，作业及扫描拥有者代次防旧写回。 | lifecycle-conversation-integration.xml；lifecycle-retention-edges.xml；lifecycle-maintenance.xml |
| L07 独立资产 | 实际发布正文、版本、草稿、原件及角色配置在删聊天前后哈希不变，检索及证据读取保留。 | lifecycle-assets-v2.xml；lifecycle-browser-v3.json |
| L08 删除不可绕回 | 接受删除立即封闭旧聊天及运行读取；消息、历史索引、协议、来源摘录、请求/响应、缓存按阶段清理。 | lifecycle-access-v2.xml；lifecycle-conversation-integration.xml |
| L09 应用接续 | 过期分页结束、SSE关闭、导出拒绝伪完整数据；删除中的页面刷新可查询原作业；学习配置解绑；实际MCP进程关闭且禁止重借。 | lifecycle-access-v2.xml；lifecycle-integration.xml；lifecycle-browser-v3.json |
| L10 文件与引用 | 无根循环可回收，有效引用保留；路径、哈希、链接及特殊文件校验；暂存活锁、24小时和所属任务恢复保护。 | lifecycle-files.xml；lifecycle-temporary.xml；lifecycle-backend-full.xml中的引用案例 |
| L11 迁移与配置 | 0028→0029及独立账本迁移只增加结构；旧字段哈希保持；.env、示例及读取代码8项配置一致；旧任务仍未纳管。 | lifecycle-migration-entrypoint.xml；lifecycle-normal-migration.json；lifecycle-normal-preservation.json；lifecycle-schema-inventory.json |
| L12 完整交付 | 开发说明、数据库设计、使用命令、配置及逐案例证据已同步；当前章节已审读，本地文档链接无缺失，实际表结构覆盖完整。 | lifecycle-delivery.json；lifecycle-document-links.json；architecture.md；database-design.md；README.md；progress.md |

## 验证结论怎样得到

最终918个不同后端案例、71项前端组件案例和28条不同浏览器场景均有通过证据。完整后端批次是914通过、2失败，完整浏览器批次是27通过、1失败；随后修正并定向验证，**不是一次完整批次全部通过**。初期用于复现缺口的失败及未完成模块时的收集错误也保留，最终索引区分它们与正式测试案例，不重复累加测试次数。

两项后端失败发生在旧版迁移夹具：使用现行ORM默认值向0022/0024旧表写数据，附带了旧表尚不存在的生命周期列。夹具现按旧表真实结构准备数据，逐列比较所有原数据，新表为空和授权不伪造等约束不变；相关6项迁移案例定向通过。浏览器失败是旧测试假设目标任务始终在第一页；修正后本例主动创建21条较新任务，再通过实际“加载更多运行”找到旧任务，验证原文分页和返回链接。没有改生产迁移或删掉数据保留断言来消除失败。

Ruff、后端类型检查、前端ESLint/TypeScript和构建均完成：完整mypy只发现一处worker局部列表缺注解，修正后对该模块与迁移入口复核；最终新增和修改的测试另行静态检查。保留上游jieba转义、MCP beta及前端主块体积提示，没有为了本轮清理工作扩展依赖升级。实际审看并保存了桌面与390px删除页面截图。原始证据见lifecycle-backend-full.xml、lifecycle-legacy-migration-fixed.xml、lifecycle-bootstrap.xml、lifecycle-frontend-full.xml、lifecycle-browser-full.json及lifecycle-browser-pagination-fixed.json。

## 正常环境与使用边界

正常业务库已是0029，回执库已执行0003_lifecycle。迁移前的业务库、回执库和完整工作区备份在artifacts/backups/e595363e5a6b；原表原列与原请求/预算未改变，18个工作区文件逐一一致。6个旧Run没有被纳管、封闭或清理，清理作业为空。只读扫描命令返回not_adopted。dev、PG和Redis仍运行，正常API、worker、Vite未启动。

日常运行由原dev.py入口启动，维护沿用API后台循环，不新增服务。README提供默认只预览的维护命令；显式执行可以纳管或推进已有作业，关闭自动扫描不会遗弃用户已经接受的删除请求。普通用户资产没有参与破坏性验证。

本轮验证使用模型替身和协议样例，以及真实隔离PG、Redis、保存器、文件、MCP和浏览器。没有付费模型调用，也不把这些检查解释为知识回答质量评估。未提交或推送Git、未启用CI、未修改桌面个人知识库，未删除持久卷、备份或独立用户资产。

剩余能力边界：本地shell仍不是操作系统级隔离；宿主编辑器不遵守应用锁，清理靠内容及元数据核对保守处理。工作区引用扫描只识别原始字节中的显式ID/路径，不解压ZIP/DOCX内部关系，也不推断自然语言暗指的材料；用户文件本身不会被运行GC删除。一次工作区扫描上限为一万文件、256MiB，超过则失败保护；检查点按公开接口分页读取，但完整保留计算仍可能随历史规模变慢。没有增加多租户容量设计、文档回收站、任意文件格式解析或数据库物理缩盘操作。
