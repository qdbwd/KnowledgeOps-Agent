# KnowledgeOps 项目上下文

更新：2026-10-19。开发对象：D:\codes\KnowledgeOps Agent。

## 当前接续状态

2026-10-19仓库发布范围调整：根docs/和evaluation/仅本地维护，不再纳入Git跟踪；代码、迁移和现有技能包属于本次提交范围。新克隆没有本地评估目录时不收集其专属测试，日常应用不依赖这些评估材料。文档维护要求继续有效，缺失本地文档时以可见代码和说明为准。

2026-10-19统一增量修改计划已完成代码与隔离验收，唯一范围与验收入口为`docs/implementation-plan.md`。U01—U15已逐项核对：网页正文/资产与回收、动态记忆类别、工具组和角色、统一模型审批、任务详情及诊断均有实现和验证证据。随后修复实际开发启动故障，正式库已从0030升级0038；历史HTML批量清理及真实模型验证仍未执行。旧计划保存在对应source文档，不构成另一套现行指令。

网页使用Trafilatura公开Markdown接口，按正文/格式/解析器去重，不保存新HTML；固定正文和旧Evidence身份保持。材料页可读保存正文，上传文件/PDF保留必要原件。present_files建立独立不可变交付版本及查看/下载/删除入口，工作文件随后改变不影响交付。

保存意图由可信入口登记library/conversation/run，不因Agent读取而变成独立资产。会话材料和草稿可明确独立保存；共享材料与真正存活引用由AssetReference图保护，裸UUID、已读目录或无根循环不构成永久依赖。会话/单份资产删除共用预览、持久作业和重试：先提交DB身份移除及精确文件清单，再重查共享关系并删文件。被正式引用的内部文本转固定SourceVersion并兼容旧地址；安装日志、租约及宽限期处理文件落盘而DB回滚的窗口，旧未知文件不按目录猜测清理。

工具与Skills改为中文摘要、共用选择器及按需完整详情，参数取真实装配，技能资源只读展示。15个原工具组沿稳定ID迁入可编辑目录，保存前预览关联影响；配置同事务修订，增加只影响新任务，收窄约束下一次未执行调用，删除保留单独选择。角色使用独立编辑页和未保存保护；研究者是可选普通模板，旧冻结绑定可恢复，不再强制加入新任务。没有开放子角色Shell、文件写入或递归委派。

记忆展示类别可选、可管理，与内部召回语义分离。原文立即保存，留空时复用Run/outbox/网关做一次有界分类；模型关闭、失败、暂停/取消及旧修订冲突都不覆盖原文。管理入口、旧记忆兼容与召回已通过确定性验收，真实分类语义质量尚未验证。

命令、文件write/edit/delete及需确认MCP共用独立无工具审查服务，固定禁止优先，失败/未知转人工；旧auto兼容降为ask，不自动开启收费审查。动作指纹、目标版本、权限和当前指令绑定到原回执，执行登记前再次校验；暂停保留prepared，取消/新指令撤回未开始动作，running/unknown继续核对原结果。预算、重复投递、HTTP审批恢复及同批补问均有真实图/PG/文件/本地MCP证据，模型供应商仍为替身。

任务详情使用有界摘要投影，诊断打开后才加载完整请求、事件和检查点，关闭中止请求；详情回收后独立交付仍可读。配置历史保留当前/墓碑、冻结绑定、子运行及学习审读报告引用的修订，超期无依赖部分按游标回收，配置锁保护冻结与编辑。设计正文为`docs/architecture.md`，阶段证据在`docs/progress.md`和`artifacts/verification/increment-delivery.json`。

整体验证完成：完整后端1126通过/8失败，34项定向复核覆盖全部失败，合计1134个不同案例有通过证据；最终修正均为旧测试前提及保护断言，产品代码未再改变。浏览器原批次41通过/10失败，12条顺序复核覆盖失败和相关前置配置，工具页全部3条另行通过，合计51个不同场景。前端84项组件、构建/lint及后端191个源文件mypy通过；失败与复核报告都保留，不称为一次全量全绿。最终报告为`increment-complete-backend.xml`、`increment-backend-final-followup.xml`、`increment-tools-browser-final.json`及`increment-final-mypy-followup.log`，索引在`artifacts/verification/increment-delivery.json`。浏览器与后端测试应顺序运行，避免共用测试库的全库锁竞争；已通过且未受影响的检查不反复运行。

备份恢复覆盖0030旧结构、当前/清理后版本，以及动态类别、组和当前/冻结配置。2026-10-19启动修复再次生成业务库、回执库和完整工作区同批备份`artifacts/backups/b07c37a876af`，隔离恢复及升级全部通过后正式迁移0030→0038。73张原表原列投影、完整工作区文件摘要保持一致，3会话、4份来源、两份HTML及固定引用保留；旧备份`ccf6bd921295`继续保留。此次演练库已收尾，历史HTML及共享文件批量清理未执行。精确清单和切换/回退顺序见迁移说明与HTML影响清单，批量清理单独授权。12例真实模型验证建议尚未获授权、未执行。

开发只用`knowledgeops-agent-dev-1`、`/workspace/.venv/bin/python`及`python scripts/dev.py --run ...`加载配置。启动器新增只读数据库版本检查，API健康检查通过后才启动worker/Vite；13项回归通过。迁移后关闭模型的运行验证中，直连和Vite代理的原报错接口全部200，新类别/工具组/角色可读；随后按原.env恢复正常启动，日志为`artifacts/verification/startup-20261019-normal.log`。没有修改.env、提交推送、公开部署、收费调用或操作桌面个人知识库，原有未跟踪Skills保留。早期失败证据继续保留；本次修复见DEV-110及progress，统一增量原证据索引是修复前的历史快照。

## 已交付基础与历史边界
2026-10-18已完成 Tailwind CSS 4.3.3 迁移，用户确认继续保留 React + Vite + React Router 的 SPA。官方Vite插件生成静态CSS，页面布局、状态和窄屏规则归组件；styles分为主题、基础排版/表单、共享按钮及Markdown/源码阅读样式。保留原绿色风格和聊天底栏，不改变后端协议、部署进程或业务流程。实现说明见architecture的“Tailwind 样式组织”、frontend/README.md及决策0030；验证覆盖构建、lint、72项组件及13条不同浏览器场景，原始失败和定向修复证据在progress。

当前dev、PG、Redis容器运行，普通API/Vite端口8000/5173未监听，未启动正常应用；验证使用自动结束的隔离API/Vite和模型替身，无付费模型调用。新前端依赖已在dev安装；日常仍从dev容器运行 `uv run scripts/dev.py`。本轮没有改.env、迁移正常数据库或操作业务材料；Git修改尚未提交推送，原有未跟踪Skills目录保留。以下2026-10-17及更早内容说明既有功能与历史交付，不代表当前应用正在运行。

2026-10-17按用户截图调整聊天与来源阅读：固定底部输入框，权限在左，右侧依次为上下文圆环、模型名称、思考强度与发送，统一居中对齐；模型文字从侧栏移入输入框，原模型状态保留在名称悬停提示中。其余设置放加号；历史独立滚动与回到底部。主动压缩用独立context_compaction Run复用原outbox/worker/摘要器/快照，不创建聊天消息、不推进学习任务；有未结束任务时不可并发压缩。来源引用默认打开网页原网址或完整固定版本，工作区文件有全文预览与代码行号；来源删除为逻辑移除，原件/版本/Evidence保留，不释放磁盘空间。详细设计与检索切片区别见architecture的“输入框、主动压缩和全文阅读”。

正常业务库0030、回执库仍为0003_lifecycle。2026-10-17交付时原开发应用经dev.py优雅重启并就绪，未更改.env模型开关；当时只读核对原4份材料仍可见，未删除正常材料。那轮验证使用隔离PG/文件、供应商替身与浏览器，没有付费模型调用；当前启动状态及本轮验证入口以上文和docs/progress.md为准。

此前2026-10-16“临时数据生命周期与清理机制”A–F及L01—L12已经完成，范围在docs/implementation-plan.md，交付为docs/lifecycle-delivery.md，证据索引为artifacts/verification/lifecycle-delivery.json。数据分类、纳管、详情回收、会话删除与资产保留规则继续有效，本轮没有重开该长期目标或改变保留期限。以下更早条目是历史交付，不能作为当前运行状态。

2026-10-15“可靠性与异步协作”增量计划A–G已完成，H01–H14均有最终通过证据：829个不同后端案例、66项组件案例、26条不同浏览器场景，完整批次的失败与定向修复均保留，不描述为一次全量全绿。最终修正包括等待重新入队时的页面观察竞态、子图审批按实际Run接续，以及旧同步验收器沿持久子任务调度推进。完整计划在docs/implementation-plan.md，逐项交付在docs/harness-reliability-delivery.md，机器证据索引为artifacts/verification/reliability-delivery.json。

本轮已按用户追加许可完成一次真实deepseek-flash联调，共11次请求：主任务异步委派两个子任务，子任务经tool_search发现只读MCP并分别读取材料，结果唤醒主任务形成汇总。关键事实通过，答案比“四句话”要求冗长，不能当作通用语义质量结论；原始报告为artifacts/verification/reliability-real-model-fbafde63b5.json。其他本轮验证使用模型替身，没有继续付费重复试验。

正常库已从0024增量升级至0028。升级前备份业务库、回执库和完整工作区到artifacts/backups/c6068f6d9526；57张原表的原列投影哈希一致，既有工具策略设置保持并新增三次策略修订，新表为空，没有伪造历史事实。报告为artifacts/verification/reliability-normal-migration.json。当前dev/PG/Redis运行，已按原dev.py入口启动API/worker/Vite，网页与健康接口返回200、三槽worker就绪，未发送正常业务模型任务。保留既有未提交工作，不提交推送，不修改个人知识库。
MCP连接池增量沿用2026-10-14用户确认的官方`langchain.mcp.MCPAdapter`。worker按聊天/子Run隔离并复用连接，有容量、空闲回收、配置/凭据失效、取消及退出清理；审批和unknown不重做合同保留。SDK自动协商的新版协议不保证服务端会话状态，连接配置可显式选`protocol_mode: legacy`，回执提供实际协议与是否复用。实现、复用来源及状态边界见[决策0027](docs/decisions/0027-mcp-session-lifecycle.md)和architecture的MCP节。该次连接池增量本身无需数据库迁移；本轮整体迁移、真实调用及应用重启状态以顶部为准。正常配置仍没有实际外部MCP服务。

2026-10-14按用户追加授权整理前后端源码。后端按app、harness、知识/学习/记忆/材料、检索与持久化分包；原runtime的状态、模型适配、控制中间件和领域工具已分开，HTTP路由按功能拆分。前端采用app/features/shared/styles，组件测试随功能归类。目录导航见[后端](backend/README.md)、[前端](frontend/README.md)，取舍见[决策0026](docs/decisions/0026-source-layout.md)。启动命令、API、工具Schema、提示和资源正文保持原合同，不新增长期目标或业务功能；最终验证结论见progress的目录整理记录。

[Harness 补齐与执行权限实施计划](docs/implementation-plan-permissions.source.md)已完成：会话模式/思考继承、命令规则、原生HITL审批、原outbox续跑、命令回执与unknown处置、前端入口和迁移均已交付。A01–A14逐项对应、验证边界与遗留限制见[交付核对](docs/harness-delivery.md)，机制与复用边界见[决策0025](docs/decisions/0025-execution-permissions.md)。最终705个不同后端案例、49项组件与22条不同浏览器场景各有通过证据；包含修正后的定向复核，不是单次全量全绿。索引为artifacts/verification/harness-delivery.json。此前[两种学习模式与通用长期记忆增量计划](docs/implementation-plan-learning-memory.source.md)已交付；Docker启动、目录与配置设计见[决策0024](docs/decisions/0024-docker-modes-and-deployment-resources.md)。

通用长期记忆、持久学习任务、两层配置、交互学习、真实读者/审读子运行、统一发布门、持续推进和Web闭环已完成工程交付。两单元离线演示、普通聊天、交互学习和配置操作通过；完整回归的四项失败已修正并定向复核，618个后端案例各有通过证据，前端40项及18条不同浏览器场景通过。不是一次全量无失败的报告；原始失败、逐项最终证据和质量边界见[进展](docs/progress.md)及 artifacts/verification/learning-delivery.json。学习双模式的真实模型语义质量仍未验证；后续单篇普通整理试用不替代该验收。

上一轮正常数据库由0022增量迁移到0024；本轮当前版本与备份见顶部。升级前按原脚本备份业务库、回执库和完整工作区到artifacts/backups/5cf03358c844；54张原表的原列内容逐表哈希一致，新增权限表没有伪造历史记录，证据为artifacts/verification/harness-normal-migration.json。新依赖tree-sitter/tree-sitter-bash已写入uv.lock并在dev安装。上一轮结束时正常应用未启动；本轮启动状态见顶部，浏览器验证继续使用隔离临时服务。原.env的MODEL_CALLS_ENABLED=true保持，离线验证显式false并使用替身；本轮追加的11次真实请求只在隔离测试空间执行。call_model仍默认关闭。

2026-10-13按用户要求修复技能包加载。根skills现有26个包，12个启用、14个因缺少平台工具/依赖/外部服务而保留未启用，availability.json记录原因，/tools展示状态。只认顶层SKILL.md入口，嵌套样例作为资料；脚本、模板和二进制资源纳入根运行快照，既有execute通过临时SKILLS_DIR执行固定资源，调用后清理。不会自动运行脚本、安装依赖或传递凭据。子Agent仍无工作区shell；未引入deerflow-harness整套依赖。相关离线真实图/子进程和公开GitHub API读取已验证，零付费模型，细节见progress与skills/README.md。API/工具名称、数据库结构与旧快照继续兼容。

思考档位按供应商能力生成，DeepSeek在线GET /models优先、五分钟缓存、显式配置回退；其他OpenAI兼容接口采用声明与ChatOpenAI通道。当前选择保存为会话默认，新普通与自动学习Run固定该选择，子Agent继承根参数，恢复不重新取UI默认；内部摘要保持专用配置。自动协调器在事务内不访问供应商，使用已校验绑定或本地能力声明。旧Run不回填。

2026-10-13进一步接通Skill用户管理：“工具与能力”页可启停兼容包，选择保存到PG原ToolPolicy及修订历史；availability.json仍作为部署兼容性上限，UI不能绕过。新根运行只冻结两层都允许的资源；关闭推进撤回代次，重新开启不复活旧绑定。14个不兼容包已获删除授权，但目录删除被开发工具自动审批以“blocked by policy”拒绝，尚未删除并继续禁用，不绕过拦截。

当前权限采用已确认的请求批准/自动执行两档，默认Shell逐次确认；已开放文件和公开检索直接使用。模式不能绕过工具/角色上限、撤回、deny、MCP确认或发布门。审批直接复用LangChain公开HITL和LangGraph中断，规则借鉴Codex逐参数匹配并直接使用Bash解析器；DeerFlow用作能力过滤与执行守卫参照。没有自动审查模型、AIO或新增OS隔离。取消/插入使旧提议失效，unknown不自动重试，服务重启仍等待真人恢复。

2026-10-13真实前端试用是独立的历史验证：官方材料导入、人工修订发布和两道溯源问答完成，共22次模型请求；初稿仍有需要人工修正的概念错误。证据保留在artifacts/verification/frontend-trial-20261013/trial.json，不把那次应用启动状态或模型语义结果当成本轮状态。该历史前端试用与本轮11次Harness联调分别记账；本轮没有Git提交/推送或桌面个人知识库操作。

## 产品与持久化

- 产品是以聊天为主要交互、由Agent推进任务的通用任务应用，重点特化知识库建设、维护、问答和学习。普通聊天/文件任务不强制创建学习任务或入库。
- 会话承载常规短期记忆；同一会话可串行切换多个学习任务。任务保存目标、范围、框架、个性化配置、授权、知识单元和进展，不另建一套任务短期记忆。旧授权、预算和待执行动作不因历史继承而继续生效。
- 交互学习围绕真人交流推进；委托整理由主Agent处理内容、读者追问、独立审读核对。协调程序推进有限单元，不增加管理模型。审读绑定草稿修订和要求版本；自动保存校验共用于业务发布和文件同步。人工明确接管具体草稿另留来源。
- 通用长期记忆是在原偏好服务上扩展；从新真人消息的既有语义解释入口产生候选，自动推进及子Agent消息不能成为真人证据。PG BM25召回、DeerMem纯函数排序、有界注入与按需补读共用一套记录及版本。
- 人工学习配置正文为不可变工作区文件；自动部分由生效记忆确定性生成，不覆盖人工正文。任务配置独立持久化，可明确选择固定版本复用；普通编辑下一Run生效，真人即时纠正通过原引导入口生效，撤回在下一相关调用前检查。
- 文件、上传原件、运行产物在 data/agent-workspace；PG保存身份、版本、引用、草稿、会话、状态和派生索引。正式知识编辑入口 knowledge/documents，历史正文 knowledge/versions；配置正文 profiles/versions。不恢复两套可分别编辑的正文真相。

## 环境与执行边界

- 本机.env与模板保持公开配置字段同步，模型与凭据值保留；APP_LOG_LEVEL同时控制API/worker，宿主工作区用AGENT_WORKSPACE_HOST。开发dev只保留两个识别标志，不注入应用配置；dev.py每次读取当前.env并生成基础服务/测试连接与内部目录，显式进程覆盖仍优先。单独调试、维护及需环境的测试使用python scripts/dev.py --run 命令；端口、挂载仍由Docker管理。最终24项定向离线检查及Compose/真实dev读取链通过；已按最终配置重新创建dev，未构建镜像、安装依赖、执行业务迁移或启动应用。证据见progress中的dev-dotenv-v2记录。
- LangChain create_agent / LangGraph，直接依赖Deep Agents 0.7.23，按具体职责借鉴DeerFlow；不整体二开DeerFlow。上游引用与适配区别在[开发实现说明](docs/architecture.md)。
- Python3.12/FastAPI/SQLAlchemy/Alembic、React/TypeScript/Vite/Tailwind CSS v4、PG17+pg_search、Celery+Redis。前端保留SPA。只用本项目容器组；运行部署为api/worker/web/postgres/redis，开发为dev/postgres/redis（应用三进程在dev内），init准备完成后退出。开发.venv实际在项目根，运行镜像使用镜像内依赖。Windows用于编辑、Git和容器控制；日常开发启动不需要回宿主执行脚本。
- Redis承载broker、SSE事件桥和确定检查点的可丢弃缓存；PG保存事实。正文/原件/配置需要与PG一起备份。模型回执库保留根预算与重放身份，不恢复已废弃的项目累计用量。
- 原工作窗口262144 tokens、90%压缩保留。根Run预算和单元跨Run预算都保留；单元默认40模型请求、80工具、20搜索、1200秒活跃时间，恢复不清零。服务重启等待人工恢复，关闭浏览器不自动取消后台任务。
- worker内LocalShellBackend用于可信本地任务，执行期间检查取消并回收普通子进程；不宣称操作系统级安全隔离，不新增AIO容器。
- 真实模型由.env/环境的MODEL_CALLS_ENABLED控制，UI只显示状态；普通应用参数修改后生产需重新创建API/worker，开发只需重启dev.py，不重建镜像或dev。用户.env保留原启用设置，离线验证显式注入false。当前统一增量没有正式迁移或启动正常应用；本轮真实模型调用按现行目标先给样例、请求量和费用范围，取得明确授权后执行，不能沿用早期联调授权。不要在开发终端长期export旧参数，否则显式覆盖仍会优先于文件。
- Git提交和推送仅按用户明确授权执行；2026-10-12完整应用已上传GitHub的main分支，用户随后要求将早期exercises移出公开仓库，仅在本地保留并加入忽略规则。本次使用普通提交同步发布范围，不改写历史，后续提交/推送仍需相应授权。当前本地分支main，检查与变更见progress。不增加GitHub CI、不公开部署应用、不购买服务、不删除正常业务数据/持久卷、不读取或修改桌面个人知识库。
- 不引入额外数据库、向量/GraphRAG、多租户、递归委派或独立记忆队列。MCP默认外部服务为空。通用call_model默认关闭，不补跑已取消的效果实验。

## 文档与交付

修改前读AGENTS、本文、progress，再查对应设计/实际代码。工程问题自主解决；产品范围、权限、费用或无法自主解决的外部条件变化才询问。

[architecture](docs/architecture.md)保存连贯开发实现说明，沿实际运行顺序解释问题、设计、模块/数据协作、上游采用方式和限制；它不是测试流水，也不把解释顺序冒充历史开发顺序。[progress](docs/progress.md)集中保存完成状态与必要证据，[development-issues](docs/development-issues.md)记录有复查价值的问题和解决办法。最终在聊天中主动说明实际开发内容，不只发文件链接或功能清单。

历史开发、收费实验、失败原文、旧部署及验收证据继续保留在progress及其链接中；本文只描述当前接续状态。开发报告不进入普通用户页面，历史23/24结果不作为本轮新增学习模式的语义质量证明。
