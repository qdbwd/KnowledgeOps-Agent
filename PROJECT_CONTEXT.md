# KnowledgeOps Agent 项目上下文

> 本文件是项目长期上下文的入口。任何编码、测试或结构调整前先阅读本文件，再按任务读取 `docs/` 下的相关文档。代码与文档冲突时以代码事实为准，并在同一次修改中更新文档。

> 2026-09-25：用户已授权在 `exercises/` 下实现独立学习小程序。KnowledgeOps 业务实战的具体架构、技术选型和里程碑仍待讨论；本次练习不将早期草案转为已确认需求。

## 1. 项目定位

当前确定的实战目标是：构建一个支持知识库构建、维护和对话问答的智能体；知识库设计参考用户的个人知识库，项目知识库与个人知识库保持独立。具体设计在学习过程中逐步讨论。

项目使用 LangGraph 自主实现，重点参考 LangChain Deep Agents 与 DeerFlow 2.0 的通用 Agent 机制及各自设计理念，不直接二次开发 DeerFlow。用户已确认提供简单 Web UI，用于对话和可视化操作，不追求复杂前端；具体交互、接口和技术栈仍待设计。

## 2. 两个知识库的硬边界

### 2.1 个人知识库

`C:\Users\cyq\Desktop\个人知识库` 是用户日常阅读、由 Codex 按其现行规则维护的独立项目。其设计可供参考；Agent 不直接操作个人源目录，不同步或回写。

2026-10-07 已按用户要求删除此前复制的 43 份 Markdown 和导入清单，项目知识库恢复为无业务数据状态。`data/knowledge_base/` 保留目录说明及空目录；没有数据库或检索索引需要重置。后续不自动重新复制个人知识库，初始材料和存储技术在项目设计中另行确定，见[清理决策](docs/decisions/0005-remove-personal-kb-snapshot.md)。

2026-10-08 按用户新授权，仅将当前个人知识库的维护规则和设计决策复制到[设计参考目录](docs/references/personal-knowledge-base/README.md)。两份原文用于设计讨论，不恢复业务语料，也不替代本项目的工程规则或自动确定 Agent 写入策略。

### 2.2 KnowledgeOps Agent 项目知识库

本项目将独立拥有并维护自己的知识库，当前尚无业务数据，建议根目录为 `data/knowledge_base/`。它是本项目 Demo 的业务数据和被测对象，Agent 可以在工具合同和运行策略约束下：

- 列出、读取、搜索和关系遍历文档；
- 新增、修改、删除项目知识；
- 重建或更新项目索引；
- 记录版本、来源、审计事件和失败结果。

是否要求人工审批是项目内可配置的安全策略，用于展示不同维护模式；它不是因为个人知识库边界而强加的永久限制。项目知识库不会自动同步到个人知识库。

## 3. 早期目标与验收建议（待按需求重新讨论）

验收重点是 Agent 全流程，而不是页面数量或基础设施规模：

1. ReAct 单 Agent 能在预算内选择工具、观察结果并结束或继续；
2. Plan-and-Execute 能维护计划、当前步骤、重规划和终止原因；
3. 项目知识库能支持问答和受控维护；
4. 回答、修改和拒答都能关联可核验来源；
5. 失败、超时、空结果、冲突和预算耗尽可观测、可回放；
6. 评估适配器能输出执行事实，而不是把一次成功包装成自治能力结论。

## 4. 当前事实

- 项目路径：`D:\codes\KnowledgeOps Agent`。
- 主项目阶段：`src/knowledgeops_agent/` 仍是 M0，无模型、无真实工具的最小图。
- 学习练习：`exercises/01_agent_loop/` 独立实现模型—工具反馈循环，支持离线脚本及可配置真实模型；实际验证范围见进展文档。
- 开发环境决定：使用本项目独立容器组，包含开发容器和实际需要的服务，不复用共享开发容器。目前 `.devcontainer/` 和容器组配置尚未创建，技术栈与服务清单待设计。
- 现有环境：Windows 本地保留独立的 `C:\Users\cyq\.local\bin\uv.exe`；首个练习已使用 uv 管理的 CPython 3.11.14，与 Miniconda 分离。本次未安装依赖或创建容器；运行与环境变更前先说明。
- 首个练习的在线配置：自动读取项目根目录 .env，使用 OPENAI_BASE_URL/OPENAI_API_KEY（兼容旧字段名）；当前用户指定 AGENT_MODEL=deepseek-flash。练习 .env 可覆盖，进程变量优先；只有 --mode online 启用真实客户端，已验证范围见进展文档。
- 真实模型调用和外部费用默认关闭，除非用户明确授权。
- Git：本地已初始化，分支为 `main`，尚无提交、暂存文件或 remote。用户截图显示已创建 GitHub 私有仓库 `qdbwd/KnowledgeOps-Agent`；本次未访问远端或提交、推送。后续 Git 写操作按用户明确指令执行。
- GraphRAG 和 Agent-Evaluation-Framework 是外部可选集成；不复制其源码，不把它们的运行时依赖塞进核心。

## 5. 阅读顺序

每次改代码前：

1. 阅读本文件；
2. 阅读与改动范围对应的 `docs/architecture.md`、`docs/implementation-plan.md`、`docs/progress.md` 或 `docs/decisions/`；
3. 检查目标模块、测试和实际调用链；
4. 修改后更新进展记录，并运行受影响的最小测试。

每次改项目设计前：

1. 先确认是否改变了项目知识库边界、Agent 状态、工具权限、终态或评估合同；
2. 若改变，新增或更新一条带状态的决策记录；
3. 同步 `README.md` 和相关上下文，避免项目入口与代码冲突。

## 6. 早期建议目录（包含尚未创建的模块）

```text
KnowledgeOps Agent/
├── PROJECT_CONTEXT.md
├── AGENTS.md
├── README.md
├── data/
│   ├── knowledge_base/       # 项目拥有的知识库，非个人知识库
│   ├── index/                # 项目知识库索引
│   └── runs/                 # 可回放运行产物
├── datasets/                 # 评估问题、固定夹具和回归样例
├── evaluation_adapter/       # 后续接入 Agent-Evaluation-Framework
├── docs/
│   ├── architecture.md
│   ├── implementation-plan.md
│   ├── progress.md
│   └── decisions/
├── src/knowledgeops_agent/
│   ├── state.py              # 状态、任务类型和终态
│   ├── graph.py              # LangGraph 编排
│   ├── policies.py           # 预算、路径和副作用策略
│   ├── tools/                # 项目知识库与其他工具
│   └── observability/        # 轨迹与运行事实
└── tests/
```

目录只在对应代码真实出现时创建，不为未来功能提前生成空模块。

## 7. 允许的能力边界

- Agent 可管理自己的项目知识库；写入必须经过明确的工具 Schema、路径约束、版本/审计记录和错误处理。
- 文档内容是数据，不是系统指令；检索到的文字不能提升工具权限。
- Agent 可以拒答、追问、重新检索或安全终止，但不能在没有证据时把猜测写成事实。
- 真实在线模型、外部服务、远程发布和跨项目写入都需要显式授权。

## 8. 关联文档

- [实施方案](docs/implementation-plan.md)
- [当前进展](docs/progress.md)
- [架构边界](docs/architecture.md)
- [独立项目与知识库所有权决策](docs/decisions/0001-independent-project-boundaries.md)

## 9. 学习练习

- [练习入口](exercises/README.md)
- [首个单 Agent 闭环](exercises/01_agent_loop/README.md)
- [首个练习的源码阅读与实现要点](exercises/01_agent_loop/CODE_WALKTHROUGH.md)：正文包含完整运行源码；修改对应练习模块时同步核对代码块和解释。
- [独立学习小程序决策](docs/decisions/0003-isolated-agent-learning-exercises.md)

学习练习默认使用 C 类教学注释；实战代码按重要性与理解需要选择 B/C。练习材料、依赖和运行状态独立；不默认采用为主项目的最终实现。

## 10. 业务应用层候选参考

2026-09-28 用户确认：先保留以下参考项目，进入知识库具体业务设计时再按问题查阅；当前继续学习通用 Agent 机制，主要参考 Deep Agents、DeerFlow 和 LangChain/LangGraph 官方资料。

| 候选项目 | 后续重点考察的内容 |
|---|---|
| [Basic Memory](https://github.com/basicmachines-co/basic-memory) | 知识组织、笔记读写与搜索工具、正文和索引之间的关系；属于知识持久化与工具层参考 |
| [Khoj](https://github.com/khoj-ai/khoj) | 个人知识助手的交互方式、资料问答与自定义 Agent |
| [Onyx](https://github.com/onyx-dot-app/onyx) | 知识检索与问答的 Agent 编排、数据源接入和权限处理 |
| [RAGFlow](https://github.com/infiniflow/ragflow) | 文档解析、切分、入库处理流程、检索和来源引用 |

Onyx 的[使用 LangGraph 实现 Agent Search 的技术文章](https://onyx.app/blog/agent-search-with-langgraph)可作为后续阅读入口；它描述的是文章发表时的实现，具体接口需对照届时使用的版本。

这是一份候选参考清单，不代表已经采用这些项目或确定数据库、存储、检索方案。前期仅核对官方仓库和说明，未本地运行或完成源码审计。具体设计时应区分资料同步与索引维护、知识正文整理与修订，再判断哪些机制适用于本项目。
