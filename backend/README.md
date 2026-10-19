# 后端源码导航

源码入口为 `src/knowledgeops_agent/`，仍是一个 Python 包。开发与容器启动方式见[项目 README](../README.md)，这里说明按职责阅读代码的顺序。

```text
knowledgeops_agent/
├── api.py、worker.py      稳定的启动入口
├── app/                  Web 应用装配与后台任务
│   ├── application.py    生命周期、请求边界、路由注册
│   ├── dependencies.py   取得当前应用的服务
│   ├── schemas.py        HTTP 输入字段和校验
│   ├── services.py       数据库、网关和业务服务装配
│   ├── routes/           会话、知识、材料、学习、记忆、权限等 HTTP 接口
│   ├── runs/             领取、队列、执行、取消、恢复、事件、任务摘要和用户引导
│   └── lifecycle/        保存意图、依赖图、详情/资产/配置历史及受管文件的分阶段回收
├── harness/              模型—工具循环及运行控制
│   ├── runtime/          Agent 装配、状态、控制中间件、冻结资源
│   ├── models/           模型网关、LangChain 适配、供应商协议、响应检查
│   ├── context/          请求组装、容量、摘要、历史补读与任务连续性
│   ├── tools/            工具目录/真实定义详情、可编辑组、策略/角色配置事务、过滤与可用视图
│   ├── permissions/      人工/独立模型审批、命令规则、执行回执及会话设置
│   ├── skills/           技能包发现、冻结和只读资源适配
│   ├── mcp/              连接注册、协议适配、会话复用与调用许可
│   ├── subagents/        角色定义、委派与子运行适配
│   └── sandbox/          本地 Shell、工作目录与文件工具
├── knowledge/            知识服务、编辑/发布工具、文件同步、规则及引用
├── learning/             学习任务、配置、协调、协作和预算
├── memory/               长期记忆版本、动态类别及有界分类、召回和补读工具
├── sources/              材料归属、导入、PDF、网页正文、固定交付和搜索工具
├── retrieval/            共用词法查询、分段和 PG 索引
├── persistence/          ORM、文件存储、Redis及检查点适配
├── core/                 部署设置、资源定位和分页
├── diagnostics/          显式开发验证工具，不注册为产品页面
├── legacy/               早期 M0 图合同，仅由原回归测试引用
├── prompts/              随包分发的固定提示资源
└── vendor/               保留出处和许可证的上游适配
```

一次请求可以按以下顺序阅读：`app/routes/conversations.py` 接收消息，`app/runs/service.py` 保存运行并建立 outbox，`app/runs/queue.py` 投递，`app/runs/worker.py` 领取并打开检查点，`harness/runtime/agent.py` 装配模型、工具和中间件。`harness/runtime/state.py` 区分持久图状态与本次运行的服务依赖，`harness/models/chat.py` 将框架调用转交统一模型网关，`harness/runtime/control.py` 检查预算、权限及中途补充消息。知识、材料、记忆和学习的工具再调用各自的业务服务。

阅读某项功能时，先看它的服务和工具，再按需看对应路由、持久化模型及根目录 `tests/` 中的集成测试。例如正式知识正文的检索、编辑和发布主要在 `knowledge/`，共用检索基础在 `retrieval/`；不要把会话历史和知识正文都当作同一种记忆。

依赖约定：路由负责 HTTP 转换，业务校验留在服务；应用入口负责装配；领域工具与领域服务放在一起，通用工具许可留在 Harness。`harness/` 目前仍引用本应用的运行服务和业务依赖，并不是可以独立发布的 SDK。本轮没有为了目录分层而重写依赖注入或改变运行流程。

Python 锁文件、迁移和跨模块集成测试仍在仓库根目录。部署可编辑的 Skills 和配置仍在根 `skills/`、`config/`；`harness/skills/` 保存的是加载程序。资源路径通过 `core/resources.py` 定位，不依赖调用文件的层级。发布包继续包含技能、配置、提示和固定 tokenizer。

这次组织方式参考 DeerFlow 的应用接入与 Harness 分区、Harness 内按能力建包，依据及迁移范围见[决策 0026](../docs/decisions/0026-source-layout.md)。这是现有代码重组，没有复制 DeerFlow 的应用或新增框架依赖。数据库表、检查点字段、工具名、HTTP 地址与以下入口保持不变：

```sh
uv run scripts/dev.py
python -m uvicorn knowledgeops_agent.api:app
python -m knowledgeops_agent.worker
```

详细运行机制仍在[开发实现说明](../docs/architecture.md)，不由这份目录导航替代。
