# KnowledgeOps Agent 实施方案

> 本文是 `KnowledgeOps Agent` 项目的实施方案副本与工程化改写。它承接个人知识库中的实战任务背景，但以本项目代码和项目知识库为唯一实施对象；个人知识库不属于本项目运行时数据。

> 本文保留为早期草案，尚未作为开发计划确认，实施仍暂停。当前确定范围见项目上下文；下文细化能力、技术选型和步骤不构成已确认需求。

## 本文目录

- [1. 目标和范围](#1-目标和范围)
- [2. 核心架构](#2-核心架构)
- [3. 项目知识库设计](#3-项目知识库设计)
- [4. Agent 运行时设计](#4-agent-运行时设计)
- [5. 工具与副作用](#5-工具与副作用)
- [6. 问答、维护和评估闭环](#6-问答维护和评估闭环)
- [7. 分阶段实施](#7-分阶段实施)
- [8. 失败边界和验收](#8-失败边界和验收)
- [9. 暂不做的内容](#9-暂不做的内容)

## 1. 目标和范围

项目要做的是一个可实际运行的 KnowledgeOps Agent Demo，展示从用户任务到 Agent 执行、知识库操作、证据生成、结果输出和评估的完整链路。

项目范围包括：

- LangGraph 单 Agent 运行时；
- ReAct 和 Plan-and-Execute 两种基本编排；
- 项目自有 Markdown 知识库及其索引；
- 知识问答、文档审计、知识新增/修改/删除；
- 来源、片段、版本和运行轨迹；
- 预算、错误恢复、终止和可选人工审批；
- 面向 Agent-Evaluation-Framework 的事实型 Adapter。

此前复制的个人知识库初始材料已于 2026-10-07 按用户要求删除，项目知识库当前无业务数据。个人知识库设计仍可参考；运行时不直接操作或回写个人源目录。后续初始材料和最终存储方案尚未确定，不自动重新导入。

## 2. 核心架构

```text
CLI / JSONL / 未来的 HTTP 适配层
                 │
                 ▼
KnowledgeOps Agent Runtime
  状态 → 决策 → 工具 → 观察 → 校验 → 继续/终止
                 │
       ┌─────────┴─────────┐
       ▼                   ▼
项目知识库工具          评估与观测
读/搜/写/审计/索引       Trace / Trial / Adapter
       │
       ▼
项目自己的 data/knowledge_base/
```

Agent Runtime 不直接依赖 Web 框架。CLI、JSONL 和未来的 FastAPI 只负责把输入映射为运行请求、把状态事件映射为输出；核心状态和工具合同保持不变。

GraphRAG 是后续可选的只读工具，不能替代 Agent Runtime；普通文本/混合检索是知识工具的一种实现。评估框架通过版本化 subprocess JSON Adapter 接收运行事实，不反向导入通用 Core。

## 3. 项目知识库设计

### 3.1 所有权

`data/knowledge_base/` 是当前项目的规范数据目录。其文件、索引、版本和审计记录由本项目管理，与 `C:\Users\cyq\Desktop\个人知识库\outputs` 完全分离。

原有 43 份 Markdown 副本和导入清单已清理，当前目录仅保留说明文件及空目录。少量独立测试夹具仍是后续建议；项目正文、元数据和索引如何存储，需要在后续设计中确定，不能把之前的快照形式视为最终存储选型。见[清理决策](decisions/0005-remove-personal-kb-snapshot.md)。

### 3.2 最小文档元数据

```yaml
id: unique-document-id
version: 1
status: active
source: project-authored | imported | generated
updated_at: 2026-09-22T00:00:00Z
```

正文中的关系可先使用稳定的相对链接或结构化关系字段。索引保存文档 ID、版本、路径、片段定位和内容哈希，避免回答只返回不可追踪的相似度分数。

### 3.3 写入模型

项目支持两种策略：

- `direct`: 通过受控工具直接应用项目知识库变更，并记录 before/after、操作者、运行 ID 和验证结果；
- `approval`: 先生成结构化提案，人工接受、编辑或拒绝后再应用。

策略由运行配置决定。两种模式都不能越过路径范围、Schema 校验、版本冲突检查和审计记录。

## 4. Agent 运行时设计

### 4.1 最小状态

```text
request
messages / observations
plan
current_step
remaining_steps
budget
knowledge_context
citations
tool_history
pending_write
approval_state
errors
terminal_reason
final_output
```

不保存模型服务端隐藏思维链。保存用户可见计划、工具调用、观察结果、证据、错误和终止原因。

### 4.2 ReAct 基线

```text
接收任务
  → 分析下一步
  → 选择一个工具或直接回答
  → 执行工具
  → 观察结构化结果
  → 校验证据/预算/权限
  → 继续或终止
```

第一版可以使用脚本化决策器和假工具完成确定性测试，再替换为真实模型决策。核心循环必须先证明状态迁移、工具 Schema、重复调用限制和终态正确。

### 4.3 Plan-and-Execute

在 ReAct 运行时稳定后增加：

```text
生成计划
  → 执行当前步骤
  → 检查步骤产物
  → 标记完成/失败
  → 必要时重规划
  → 汇总结果并终止
```

计划必须有版本、当前步骤、完成条件和重规划原因，不能只是 Prompt 中的一段不可观测文本。

## 5. 工具与副作用

| 工具 | 作用 | 副作用 |
|---|---|---|
| `list_documents` | 列出项目知识库文档和元数据 | 无 |
| `read_document` | 读取正文、版本和关系 | 无 |
| `search_documents` | 关键词/混合检索并返回片段 | 无 |
| `traverse_relations` | 按关系和深度遍历项目文档 | 无 |
| `validate_citations` | 校验引用是否来自实际证据 | 无 |
| `create_document` | 新建项目知识 | 写项目知识库 |
| `update_document` | 更新项目知识并检查版本 | 写项目知识库 |
| `delete_document` | 软删除或受控删除 | 写项目知识库 |
| `rebuild_index` | 更新项目索引 | 写索引，不改变正文 |
| `query_graphrag` | 后续调用外部 GraphRAG | 外部只读 |

工具结果必须携带稳定文档 ID、版本、片段位置、来源和错误类型。工具失败、空结果、权限拒绝和内容冲突不能都压缩成普通空字符串。

## 6. 问答、维护和评估闭环

### 6.1 知识问答

```text
问题分类
  → 选择文本检索/关系遍历/可选 GraphRAG
  → 收集证据
  → 检查覆盖和冲突
  → 生成带引用回答或明确拒答
```

### 6.2 知识维护

```text
审计或用户更新请求
  → 定位目标文档
  → 读取当前版本
  → 生成结构化变更
  → 校验来源、Schema、链接和版本
  → direct 应用或等待 approval
  → 更新索引并记录审计
```

### 6.3 评估

Agent 输出执行事实：Transcript、Outcome、Artifact/TrajectoryBehavior/Outcome Subject、coverage 和 provider usage。评估分数、Hard Gate 和最终结论由评估框架负责，Agent Adapter 不自行判分。

首批评估切片：直接查找、跨文档关系、知识缺口、冲突审计、写入成功、版本冲突、工具超时、预算耗尽、重复调用和提示注入。

## 7. 分阶段实施

### M0：合同骨架（当前已完成）

状态、任务类型、终态、预算消耗和确定性 LangGraph 接线已存在；没有真实模型和工具。

### M1：最小 Agent Runtime

实现脚本化 ReAct 循环、工具注册、结构化观察、失败恢复、重复调用限制、轨迹记录和 CLI/JSONL 运行入口。此阶段先使用项目内测试夹具，不先接个人知识库，也不先做向量 RAG。

### M1.5：Plan-and-Execute

在相同 Runtime 上增加计划版本、当前步骤、完成条件和有限重规划。

### M2：项目知识库与 Native RAG

创建项目自己的 `data/knowledge_base/`，实现文档读取、关键词/混合检索、引用定位、写入工具、索引更新和维护审计。这里的 RAG 是 Agent 的知识工具，不是 Agent 框架本身。

### M3：人工审批与恢复

实现 checkpoint、写入前暂停、接受/编辑/拒绝/恢复，以及版本冲突和回滚测试。`direct` 模式仍保留审计记录。

### M4：GraphRAG 可选工具

只有在问题集证明普通检索不足时，才接入已有 GraphRAG 的隔离只读接口，并固定数据、模型、Top-k、预算和对照条件。

### M5：评估 Adapter 与在线冒烟

接入 Agent-Evaluation-Framework，先运行不调用真实模型的契约测试；经明确授权后再做低成本在线样本和人工校准。

### M6：可选 Web 包装

只有 CLI/JSONL 接口、核心状态、运行轨迹和恢复语义稳定后，才用 FastAPI 提供 HTTP/SSE；前端最后考虑。Web 层不得重新实现 Agent 逻辑。

## 8. 失败边界和验收

必须能区分并记录：证据不足、工具空结果、工具异常、非法工具参数、预算耗尽、计划失败、版本冲突、审批拒绝、引用失效和未观测。

首版门禁：

- 未经策略允许不能写项目知识库；
- 写入路径不能逃逸 `data/knowledge_base/`；
- 引用必须来自本次执行获得的证据；
- 相同工具和参数连续重复达到上限时必须终止或重规划；
- 模型/内容中的指令不能扩大工具权限；
- Agent 运行失败和评估器失败分开记录；
- 未有真实在线运行证据时，不声称 Agent 已具备生产自治能力。

## 9. 暂不做的内容

首版不做多 Agent、生产级前后端、Kubernetes、复杂长期记忆、自动 Prompt 优化、个人知识库同步和 GraphRAG 全量替换。它们不是永远禁止，而是等核心 Agent 闭环有可验证证据后再决定。

## 关联资料

- 项目上下文：[`PROJECT_CONTEXT.md`](../PROJECT_CONTEXT.md)
- 当前进展：[`progress.md`](progress.md)
- 架构边界：[`architecture.md`](architecture.md)
- 个人知识库原始任务方案（仅参考）：`C:\Users\cyq\Desktop\个人知识库\outputs\任务\可溯源知识研究与维护Agent实战\可溯源知识研究与维护Agent实战方案.md`
- Agent 工作流、RAG、GraphRAG 和评估专题均保留在个人知识库中作为参考资料，不是本项目运行时依赖。
