# 0020：单PG内的BM25片段检索与领域提示装配

日期：2026-10-10。用户明确要求同时改善会话记忆检索和知识正文检索，优先保持PG，采用真实BM25；最终确认直接用官方预装`pg_search`的PG17镜像重建容器，不在旧容器手动装扩展。本文记录取舍，当前实现说明仍在architecture，运行证据在progress。

## 决定与适用范围

保留PG作为业务与检索的唯一数据库体系。Compose使用ParadeDB官方`paradedb/paradedb:0.26.1-pg17`，固定manifest摘要`sha256:c1c147499c6649739d7977f1e47db149578c894f35249953acdd7680ccbae75b`；主版本仍为17，继续挂载原`postgres-data`卷。不是PostgreSQL社区基础镜像自行附带了BM25，而是ParadeDB维护的预装镜像。`pg_search`与先前调查过的`pg_textsearch`是不同扩展，本项目最终只接入前者。

启动参数显式预加载pg_search和镜像默认的pg_cron、pg_stat_statements，避免旧卷跳过initdb后没有预加载。关闭镜像的自动硬件调参，以保持新装/旧卷行为清晰。迁移0017启用pg_search并创建、回填两张派生片段表。pg_search 0.26依赖vector，安装依赖存在不表示本项目启用了向量检索；没有embedding调用或新向量服务。

知识正文和会话历史共享分段/分词代码及BM25引擎，分别维护索引与权限。`knowledge_search_chunks`关联当前知识版本，`task_history_chunks`关联可见原话；原文、不可变版本、引用、检查点和费用账本不改写。保留旧索引列供结构降级兼容，正常检索只使用新BM25路径，不在失败时静默换成旧评分。

## 参考和实际复用

- DeerFlow固定到`0fe6e63a68f014e931393f15e639fe0042cebd93`：核对`agents/task_continuity/archive.py`、`agents/memory/backends/deermem/deermem/core/retrieval.py`以及连续性/记忆检索测试。采用中文预处理、自然语言OR召回、BM25相关性、范围过滤先于限量、稳定ID与分页补读。没有复制SQLite存储；没有把记忆置信度、时间权重套到知识正文上。上游FTS5索引单位由应用决定，不能宣称FTS5自动切段。
- 分段直接依赖`langchain-text-splitters`，锁定1.1.3；使用RecursiveCharacterTextSplitter，保留段落/句界及原文偏移。1200字符/160重叠是本项目初始值，不是上游默认或已经过质量调参的结论。
- BM25直接调用pg_search 0.26.1的公开SQL接口，SQLAlchemy仅作参数绑定和SQL类型薄适配。标题/元数据/正文的boost为3/2/1，保留数字为硬约束；不自行实现算法，不添加SQLite或语义重排。依据：[建索引](https://www.paradedb.com/docs/reference/indexing/create-index)、[匹配查询](https://www.paradedb.com/docs/reference/full-text/match)、[BM25评分](https://www.paradedb.com/docs/reference/full-text/score)。
- 同一DeerFlow提交的`agents/lead_agent/prompt.py`与`test_soul_prompt_injection.py`提供系统提示分区、角色渲染和转义参考。`prompt_assembly.py`是设计适配，没有复制完整提示词或harness。已有`knowledge_policy.md`承担常驻领域规则，不另建第二份SOUL真相。

## 数据和调用合同

1. 知识发布事务同时更新当前索引父记录与片段，归档/改版级联移除旧片段，旧版本原文仍保留。按文章选最佳命中片段后限量，返回版本与字符偏移，读取服务才登记证据。
2. 历史先限定scope、检查点批次及角色，再按原消息去重取前8条。当前消息按需幂等缓存以获得真实BM25评分，但不创建可达批次或改写State。库里存在不等于任意检查点有权读取，旧状态不能利用缓存看到未来消息。
3. 新任务冻结系统模板与检索说明全文；旧资源没有模板字段时保持原拼接合同。动态任务背景、摘要、偏好及检索工具结果仍由既有中间件组织，不把资料升为系统指令。
4. 长期偏好保持原来的生效/撤回与快照机制；本轮优化会话原话和知识正文，不宣称已经实现跨所有会话自由提炼和搜索任意事实。

## 维护、验证和限制

迁移前停止API/worker写入并备份。虽然PG主版本相同，官方镜像的Debian从bookworm变为trixie，glibc排序规则2.36→2.41，因此先重建受影响索引再刷新数据库排序规则版本，不能只修改版本号消除警告。正常业务与费用表逐表比较内容摘要；不删除原卷。换回旧镜像也要处理排序规则和扩展可用性，不能把任意同主版本镜像视为可无条件互换。

验证采用固定中文/技术词/数值材料，覆盖长段末尾定位、原文补读、文章替换、历史权限、缓存幂等、真实BM25分数和回滚、非空0016回填、空数据库启用扩展。另用非空隔离表做pg_dump/pg_restore，确认恢复后排名与分数一致。不调用收费模型，不重跑历史业务评估来证明排序改动；K09等模型综合问题未据此改判。

索引可从原文重建。BM25仍是词法相关性，分数不代表事实可靠度；全表统计随材料增减变化，候选名次不是运行快照的一部分。分段大小、权重和检索质量后续可用实际查询集调整；本轮不宣称优于DeerFlow、不保证模型一定检索或正确使用命中内容。
