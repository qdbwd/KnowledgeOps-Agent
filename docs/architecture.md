# 架构边界

## 当前阶段

M0 只建立状态、确定性策略和 LangGraph 接线。当前图不会调用模型或工具，也不具备动态决策、恢复、checkpoint 或人工审批能力。业务方案继续讨论。独立学习代码见 `exercises/01_agent_loop/`，不把其教学选择作为主项目已确认架构。下文系统数据流与集成方向保留为早期草案。

## 系统数据流

```text
CLI / JSONL / 未来 HTTP 适配层
  → KnowledgeOps Agent Runtime
      → decide
      → execute project-knowledge tool
      → observe
      → validate evidence / budget / side effect
      → replan / ask user / answer / maintain knowledge
  → terminal outcome + trace
```

核心 Runtime 与 Web 传输无关。FastAPI、SSE 或前端只在后续作为薄适配层增加。

## 项目知识库

`data/knowledge_base/` 是当前项目拥有的知识库，也是 Agent 的业务数据和被测对象。Agent 可按配置读写、更新索引并留下审计记录。写入必须受路径范围、Schema、版本冲突、预算和审计策略约束。

项目知识库与 `C:\Users\cyq\Desktop\个人知识库` 完全分离。个人知识库不是当前 Agent 的默认数据源，本项目不自动读取、复制或回写它。

## 外部系统

- GraphRAG：后续作为隔离的可选只读工具接入；普通文本/混合检索保持基线；
- Agent-Evaluation-Framework：通过目标项目拥有的 subprocess Adapter 消费执行事实；
- 个人知识库：仅为设计参考，不是本项目运行时依赖。

## 评估事实

Adapter 后续应返回 Transcript、Outcome、Artifact/TrajectoryBehavior/Outcome Subject、capture coverage 和 provider 明确报告的 usage。评分、Hard Gate、Decision 和 Result Projection 不属于 Adapter。
