# 0001：独立 Agent 仓库与外部系统边界

- 状态：部分被 [0006](0006-v1-web-application.md) 取代；独立项目边界保留，外部评估 Adapter 不再采用。
- 日期：2026-09-21

## 背景

项目需要把 LangGraph Agent、知识库能力、现有 GraphRAG 和 Agent 评估框架串成真实闭环，同时避免把多个项目耦合成一份难以复用和评估的代码。

## 决策

KnowledgeOps Agent 使用独立仓库。GraphRAG 作为可选只读工具；Agent-Evaluation-Framework 通过版本化 subprocess JSON Adapter 接入。首版不复制任何外部项目源码。

项目知识库的所有权和写入策略由本项目单独定义，详见决策 0002。

## 后果

- Agent 可以独立演进并作为真实被测系统；
- 外部 GraphRAG 和评估 Core 保持各自依赖与运行环境；
- 跨仓库调用需要稳定协议和契约测试；
- 外部项目的在线模型、远程发布或跨项目写入需要后续明确授权。
