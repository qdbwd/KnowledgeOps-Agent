# KnowledgeOps 项目上下文

更新：2026-10-12。开发对象：D:\codes\KnowledgeOps Agent。

## 当前接续状态

此前的[两种学习模式与通用长期记忆增量计划](docs/implementation-plan.md)已完成工程交付，取代关系见[决策0023](docs/decisions/0023-learning-modes-and-general-memory.md)。用户随后追加的 Docker 快速启动、目录与配置、Skills及前端布局整理已落实，当前设计见[决策0024](docs/decisions/0024-docker-modes-and-deployment-resources.md)。没有启动新的长期目标；历史计划和exports不能重新当成待执行指令。

通用长期记忆、持久学习任务、两层配置、交互学习、真实读者/审读子运行、统一发布门、持续推进和Web闭环已完成工程交付。两单元离线演示、普通聊天、交互学习和配置操作通过；完整回归的四项失败已修正并定向复核，618个后端案例各有通过证据，前端40项及18条不同浏览器场景通过。不是一次全量无失败的报告；原始失败、逐项最终证据和质量边界见[进展](docs/progress.md)及 artifacts/verification/learning-delivery.json。真实模型语义质量未验证。

正常数据库仍为0022，本次部署整理没有新增业务迁移或删除数据。前后端源码已移至backend/src/knowledgeops_agent和frontend；config及skills位于根目录。用户进一步要求简化开发体验，make dev或start.ps1 -Mode development现在只准备dev/PG/Redis；进入dev执行python scripts/dev.py，API、worker、Vite在同一容器内作为三个进程运行。API/Vite热更新，worker显式重启；Ctrl+C统一收尾。make up保留独立api/worker/web部署方式。最新单容器启动、HTTP/消费者就绪、重复启动拒绝和退出清理已验证，报告为artifacts/verification/dev-single-container.json；验证进程已停止，当前仅dev/PG/Redis运行，需在dev终端运行脚本才有网页。未修改.env的MODEL_CALLS_ENABLED=true，验证子进程显式关闭模型调用；正常库仍无Run，本轮零付费。call_model仍需Run明确允许辅助模型，默认和自动推进均不允许。

现有三份技能保留，另移植并适配DeerFlow的code-documentation和academic-paper-review；/tools可查看五份技能。模型及搜索等部署资源支持指定位置，旧运行保持冻结内容。API/工具接口、容器服务名和持久卷名称保持兼容。最新证据见progress及artifacts/verification/docker-*-layout.json、docker-layout-resources.xml、docker-layout-browser.json。备份脚本已脱离常驻dev，实际备份及哈希检查通过，保存于artifacts/backups/docker-modes-20261012；未重复做全套恢复演练，既有非空恢复证据仍在d0ea729d9035。

## 产品与持久化

- 产品是以聊天为主要交互、由Agent推进任务的通用任务应用，重点特化知识库建设、维护、问答和学习。普通聊天/文件任务不强制创建学习任务或入库。
- 会话承载常规短期记忆；同一会话可串行切换多个学习任务。任务保存目标、范围、框架、个性化配置、授权、知识单元和进展，不另建一套任务短期记忆。旧授权、预算和待执行动作不因历史继承而继续生效。
- 交互学习围绕真人交流推进；委托整理由主Agent处理内容、读者追问、独立审读核对。协调程序推进有限单元，不增加管理模型。审读绑定草稿修订和要求版本；自动保存校验共用于业务发布和文件同步。人工明确接管具体草稿另留来源。
- 通用长期记忆是在原偏好服务上扩展；从新真人消息的既有语义解释入口产生候选，自动推进及子Agent消息不能成为真人证据。PG BM25召回、DeerMem纯函数排序、有界注入与按需补读共用一套记录及版本。
- 人工学习配置正文为不可变工作区文件；自动部分由生效记忆确定性生成，不覆盖人工正文。任务配置独立持久化，可明确选择固定版本复用；普通编辑下一Run生效，真人即时纠正通过原引导入口生效，撤回在下一相关调用前检查。
- 文件、上传原件、运行产物在 data/agent-workspace；PG保存身份、版本、引用、草稿、会话、状态和派生索引。正式知识编辑入口 knowledge/documents，历史正文 knowledge/versions；配置正文 profiles/versions。不恢复两套可分别编辑的正文真相。

## 环境与执行边界

- 本机.env与模板保持22个公开字段，模型与凭据值保留；APP_LOG_LEVEL同时控制API/worker，宿主工作区用AGENT_WORKSPACE_HOST。开发dev只保留两个识别标志，不注入应用配置；dev.py每次读取当前.env并生成基础服务/测试连接与内部目录，显式进程覆盖仍优先。单独调试、维护及需环境的测试使用python scripts/dev.py --run 命令；端口、挂载仍由Docker管理。最终24项定向离线检查及Compose/真实dev读取链通过；已按最终配置重新创建dev，未构建镜像、安装依赖、执行业务迁移或启动应用。证据见progress中的dev-dotenv-v2记录。
- LangChain create_agent / LangGraph，直接依赖Deep Agents 0.7.23，按具体职责借鉴DeerFlow；不整体二开DeerFlow。上游引用与适配区别在[开发实现说明](docs/architecture.md)。
- Python3.12/FastAPI/SQLAlchemy/Alembic、React/TypeScript/Vite、PG17+pg_search、Celery+Redis。只用本项目容器组；运行部署为api/worker/web/postgres/redis，开发为dev/postgres/redis（应用三进程在dev内），init准备完成后退出。开发.venv实际在项目根，运行镜像使用镜像内依赖。Windows用于编辑、Git和容器控制；日常开发启动不需要回宿主执行脚本。
- Redis承载broker、SSE事件桥和确定检查点的可丢弃缓存；PG保存事实。正文/原件/配置需要与PG一起备份。模型回执库保留根预算与重放身份，不恢复已废弃的项目累计用量。
- 原工作窗口262144 tokens、90%压缩保留。根Run预算和单元跨Run预算都保留；单元默认40模型请求、80工具、20搜索、1200秒活跃时间，恢复不清零。服务重启等待人工恢复，关闭浏览器不自动取消后台任务。
- worker内LocalShellBackend用于可信本地任务，执行期间检查取消并回收普通子进程；不宣称操作系统级安全隔离，不新增AIO容器。
- 真实模型由.env/环境的MODEL_CALLS_ENABLED控制，UI只显示状态；普通应用参数修改后生产需重新创建API/worker，开发只需重启dev.py，不重建镜像或dev。用户.env保留原启用设置，当前没有应用进程；验证命令继续显式注入false，不修改配置值，不作本轮真实语义质量达标声明。不要在开发终端长期export旧参数，否则显式覆盖仍会优先于文件。
- Git提交和推送仅按用户明确授权执行；2026-10-12完整应用已上传GitHub的main分支，用户随后要求将早期exercises移出公开仓库，仅在本地保留并加入忽略规则。本次使用普通提交同步发布范围，不改写历史，后续提交/推送仍需相应授权。当前本地分支main，检查与变更见progress。不增加GitHub CI、不公开部署应用、不购买服务、不删除正常业务数据/持久卷、不读取或修改桌面个人知识库。
- 不引入额外数据库、向量/GraphRAG、多租户、递归委派或独立记忆队列。MCP默认外部服务为空。通用call_model默认关闭，不补跑已取消的效果实验。

## 文档与交付

修改前读AGENTS、本文、progress，再查对应设计/实际代码。工程问题自主解决；产品范围、权限、费用或无法自主解决的外部条件变化才询问。

[architecture](docs/architecture.md)保存连贯开发实现说明，沿实际运行顺序解释问题、设计、模块/数据协作、上游采用方式和限制；它不是测试流水，也不把解释顺序冒充历史开发顺序。[progress](docs/progress.md)集中保存完成状态与必要证据，[development-issues](docs/development-issues.md)记录有复查价值的问题和解决办法。最终在聊天中主动说明实际开发内容，不只发文件链接或功能清单。

历史开发、收费实验、失败原文、旧部署及验收证据继续保留在progress及其链接中；本文只描述当前接续状态。开发报告不进入普通用户页面，历史23/24结果不作为本轮新增学习模式的语义质量证明。
