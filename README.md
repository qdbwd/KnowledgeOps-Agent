# KnowledgeOps Agent

目标是使用 LangGraph 自主实现支持知识库构建、维护和问答的 Agent。业务主代码目前为 M0 骨架；实现层学习使用 [独立练习小程序](exercises/README.md)，具体业务方案继续讨论。

## 项目边界

本项目独立管理自己的知识库：

```text
KnowledgeOps Agent/
├─ data/knowledge_base/     # 项目自己的知识库，可由 Agent 受控维护
├─ data/index/              # 项目知识库索引
├─ data/runs/               # 运行和审计产物
└─ src/knowledgeops_agent/  # Agent Runtime
```

`C:\Users\cyq\Desktop\个人知识库` 是另一个独立项目。本项目不把它作为默认运行时数据，不自动复制或修改它。个人知识库中的设计规则可以作为参考，但项目 Agent 的问答和维护对象是自己的 `data/knowledge_base/`。

GraphRAG 是后续可选的外部只读工具，Agent-Evaluation-Framework 通过版本化 subprocess JSON Adapter 接入。两者均不复制源码到本项目。

## 当前状态

业务主代码处于 **M0 合同骨架**：

- 已定义最小状态和终态；
- 已建立一个不调用模型、不调用工具的 LangGraph 合同图；
- 已建立确定性的步数、路径和策略测试；
- 尚未实现 ReAct 循环、Plan-and-Execute、项目知识库工具、真实检索、模型决策、checkpoint、GraphRAG 或评估 Adapter；
- 当前代码不能作为 Agent 质量或 autonomy 能力证据。

项目知识库当前无业务数据：2026-10-07 已按用户要求删除此前从个人知识库复制的 43 份 Markdown 和导入清单。个人知识库原件保留；后续不自动恢复副本，初始材料及存储方案待设计确定，见[清理决策](docs/decisions/0005-remove-personal-kb-snapshot.md)。

新增的 [01：完整单 Agent 闭环](exercises/01_agent_loop/README.md) 位于独立练习目录，包含工具反馈循环、错误恢复、预算和轨迹。它不代表业务主代码已进入 M1；验证范围见 [项目进展](docs/progress.md)。

## 早期目标草案（未确认，非练习实施计划）

1. M1：脚本化 ReAct Runtime、工具合同、观察、错误恢复、预算、轨迹和 CLI/JSONL；
2. M1.5：Plan-and-Execute 计划版本、当前步骤和重规划；
3. M2：项目知识库、文档工具、Native RAG、问答和受控维护；
4. M3：checkpoint、人工审批、版本冲突和回滚；
5. M4：可选 GraphRAG 工具；
6. M5：评估 Adapter；
7. M6：按需增加 FastAPI/SSE，作为薄适配层而不是重写 Agent。

## 开发环境（待配置）

用户已确认使用本项目独立容器组，包含开发容器与实际需要的服务，不复用其他项目的共享开发容器。当前尚未创建容器配置；技术栈、服务和对应的安装、启动、测试命令将在设计确认后补充。已有 Windows 练习环境的说明保留在练习文档中。

项目将提供用于对话和可视化操作的简单 Web UI；具体交互与实现方案仍待设计。

真实模型调用默认关闭，只有用户明确授权后才可进行在线冒烟。

## 长期文档

- [项目上下文](PROJECT_CONTEXT.md)：每次编码前先读；
- [实施方案](docs/implementation-plan.md)：目标、架构、阶段和验收；
- [当前进展](docs/progress.md)：已完成证据、下一步和测试结果；
- [架构边界](docs/architecture.md)：运行时、知识库和外部系统边界；
- [决策记录](docs/decisions/)：用户确认的长期设计取舍。
- [个人知识库设计参考](docs/references/personal-knowledge-base/README.md)：授权复制的两份规则原文，仅作设计资料，不作为业务数据或项目开发指令。
