# Harness 可靠性与异步协作交付核对

2026-10-15：implementation-plan.md 的 A–G 已完成，H01–H14均有最终通过证据。机器可核对索引在 [reliability-delivery.json](../artifacts/verification/reliability-delivery.json)，机制说明见 [architecture](architecture.md)，使用方式见 [README](../README.md)。以下报告均位于 artifacts/verification。

本轮优先使用确定性模型替身，但实际运行图、数据库、文件、进程、消息队列、MCP协议和浏览器；另完成一次11请求的真实模型联调。替身证明程序控制，真实联调补充工具理解与实际协作证据，两者不能互相替代。

| 条件 | 实际完成的行为 | 关键证据 |
| --- | --- | --- |
| H01 文件冲突 | 读取标记绑定文件版本；压缩后失效；同路径串行校验，旧读取不能覆盖新内容。 | reliability-file-boundaries.xml；最终后端回归 |
| H02 文件恢复 | 文件已改但回执或知识同步未完成时，核对实际哈希并补齐记录；未知/冲突交给真人查看、保留现状或复核，不自动重写。独立进程在写前、写后崩溃均已覆盖。 | reliability-file-sync-fixed.xml、reliability-file-reconciliation.xml；最终后端回归及reliability-file-visual.json |
| H03 Skill隔离 | 单包加载错误按包报告，其余技能继续；全局启停配置损坏不放开所有技能。 | reliability-skills.xml、reliability-skill-ui.xml；最终后端回归 |
| H04 安全暂停 | 流式响应保存后停在安全点，恢复重放已有模型回执；主子暂停传播、暂停期间保存决定但不启动、重启后明确恢复均有证据。 | reliability-model-step-persistence.xml、reliability-pause-integration.xml、reliability-approval-compatibility.xml |
| H05 补问 | 同批询问和操作先阻止后者，问题/回答持久化；拒绝、取消、重复提交、刷新与跨进程继续保持原问题身份。 | reliability-questions-integration.xml、reliability-questions-persistence.xml、reliability-questions-browser.json |
| H06 真实并发 | 实际Celery三槽、Redis、PG与替身屏障证明主加两子可以同时推进；数据库限制组内并发与领取。 | reliability-async-controls.xml、reliability-async-worker-persistence.xml |
| H07 共享预算 | 主子请求在同一根预算预占，保留主收尾请求；活跃时间按运行区间并集，全部等待时停表；恢复/追加不清零，学习单元累计预算保留。 | reliability-async-edges.xml、reliability-async-controls.xml、reliability-approval-compatibility.xml |
| H08 子任务管理 | 六个管理工具与Web卡片共用服务；启动立即返回、追加要求撤下旧提议、单独取消、结果查询、只读和禁止递归。同步task与学习读者/审读使用同一持久调度。 | reliability-async-wait.xml、reliability-async-compat-recovery.xml、reliability-approval-compatibility.xml、reliability-browser-final-fixes.json |
| H09 事件接续 | 子结果去重后接续原主Run；等待不占槽；真人即时插入可提前唤醒，暂停不能被结果复活。页面等待转重新入队后持续观察，直到终态。 | reliability-async-controls.xml、reliability-async-guidance.xml、reliability-live-run-fix.xml、reliability-browser-final-fixes.json |
| H10 MCP | 子角色只继承允许的只读目录，连接与会话不串用；现代协议表单/URL补问保存轮次及requestState，答案进入后续轮，服务计数证明首轮未重复。旧协议和服务端失效明确报告边界。 | reliability-child-mcp-isolation.xml、reliability-mcp-input-persistent-service.xml、reliability-mcp-input-invalidations.xml、reliability-mcp-input-compatibility.xml；最终回归 |
| H11 上下文 | 完整请求分项计量，大背景先去重/缩减并保存补读引用，近期完整消息组保留；按需工具发现仍受冻结目录、撤回及角色上限约束。约1–2万tokens测试窗口验证，不填满正式窗口。 | reliability-input-budget-fixed.xml、reliability-discovery-fixed.xml、reliability-context-output-integration.xml、reliability-context-browser.json；真实联调 |
| H12 输出保存 | 短命令输出边接收边保存，预览截断仍可补读上限内正文；超过保存上限、超时或取消均有真实标记，并收尾普通子进程。 | reliability-command-output.xml、reliability-context-output-integration.xml；最终回归 |
| H13 迁移兼容 | 旧会话/快照、同步委派、消息插入/排队、审批、草稿与发布均有回归。正常库0024→0028完成备份和原列哈希核验；旧许可不会因新增目录项而自动扩大。 | reliability-normal-migration.json、reliability-final-backend.xml、reliability-backend-regressions.xml、reliability-approval-compatibility.xml |
| H14 界面闭环 | 实际浏览器覆盖暂停、补问、子任务管理、MCP输入、文件核对、刷新及自动接续；桌面与移动截图已审看。 | reliability-final-browser.json及定向修复报告；reliability-file-visual.json |

## 如何解读验证结果

最终共有829个不同后端案例、66项前端组件案例、26条不同浏览器场景具有通过证据。这是完整批次加定向修复后的逐项结果，**不是一次完整运行没有失败**。初次后端批次826项中21项失败、初次组件批次65项中2项失败、初次浏览器批次26项中8项失败，原始报告均保留。两个文件崩溃和一个真实三槽worker案例已独立执行，不为凑整批再次重复。索引按同一案例的最新证据归并，不累加重复次数。

收尾实际修复了等待→重新入队时前端丢失观察者的竞态，以及子图待审批但旧服务只按主图状态判断的接入缺口；其余兼容调整主要是让旧用例与显式验收器通过真实异步调度推进，并更新固定字段/目录断言。保留原来的只读、越权、预算和不重复副作用检查，未通过降级生产约束获得通过。Ruff、149个后端源码的mypy、TypeScript、ESLint与构建通过；最终修改的审批模块另行复核。前端主块体积和上游jieba/MCP beta提示仍保留。

## 真实模型联调及质量边界

deepseek-flash实际调用11次：意图解释1次、主Agent4次、两个子Agent合计6次。两个真实子Run各自用tool_search发现并调用本地只读MCP，主Run等待后汇总。关键数字22及两个材料标记正确；回答超出“四句话”要求，子结果偏冗长。这说明本例的工具理解和协作链路可工作，同时保留表达约束未完全遵守的事实；不推导任意主题、任意外部服务或所有长任务都可靠，也未用真实付费模型验证崩溃恢复。

供应商回报输入84951、输出4146 tokens，输入含缓存计数，不能直接当作现金账单。原始报告reliability-real-model-fbafde63b5.json与独立审读reliability-real-model-review.json均保留。本次真实联调及正常配置保持call_model关闭，没有追加已取消的辅助模型效果实验，也未为争取一次更好的表述重复付费。

## 迁移、部署与使用

正常数据库已从0024升级至0028。备份artifacts/backups/c6068f6d9526同时包含业务库、调用回执库和完整工作区，迁移前核验备份哈希；57张原表原列哈希一致，保留原策略并增加三条能力策略修订，新表没有伪造历史记录。迁移和正常应用就绪证据分别为reliability-normal-migration.json、reliability-dev-readiness.json。

开发模式仍为dev/PG/Redis三个容器，API、三槽worker与Vite由原dev.py入口运行；应用地址为http://localhost:8787。新增审批逻辑已随最后一次应用重启加载，未向正常业务会话提交验证模型任务。真实.env保留已有配置与密钥，只补输出保存上限缺项；新容量字段进入model-capacity.json并随运行冻结，旧快照沿原合同恢复。

用户通过聊天中的暂停/继续、问题卡、子任务卡与文件核对卡操作即可；详细说明已加入README。仍不包含交互终端、独立安全沙盒、递归子Agent或自动安装第三方MCP。旧式MCP活连接补问没有跨进程恢复保证，现代续接也依赖服务端保存必要状态；本地命令执行不构成操作系统级隔离。原工作窗口262144 tokens、90%压缩触发保持不变。

既有未提交工作保留；未提交或推送Git、未启用GitHub CI、未修改桌面个人知识库，也未删除正常业务数据或持久卷。
