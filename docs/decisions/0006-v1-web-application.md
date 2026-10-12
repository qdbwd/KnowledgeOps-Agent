# 0006：批准完整首版 Web 应用与独立环境

- 状态：已接受，2026-10-08 用户批准并授权长期目标执行。
- 依据：[确认计划快照](../exports/KnowledgeOps-Agent-首版Web应用实施计划-2026-10-08.md)，包括自主执行补充。

## 决策

交付整个前后端应用：create_agent / LangGraph、FastAPI、React/Vite、PostgreSQL 17，以及 dev/api/worker/web/postgres 独立容器。正文存为数据库内 Markdown，原始材料存独立卷；正文、版本、索引、审计与幂等回执事务提交。

DDGS + HTTPX + Trafilatura 做网络研究，pypdf + pypdfium2 + Tesseract 做 PDF/OCR，jieba + PostgreSQL 做词法检索。最多两个研究子 Agent，不递归，无发布权，保留单 Agent 对照。

采用项目内评估：24 个固定案例至少 21 个通过，工程硬条件全通过；另有 6 任务 × 2 模式 × 3 次对照。真实模型首次最多 10 次、总共最多 600 次，调用前持久化预占。

## 取代关系

- 0001 独立仓库与不复制外部源码有效；旧 GraphRAG 首版集成/外部评估 Adapter 被取代，Agent-Evaluation-Framework 不接入。
- 0002 知识库所有权与隔离有效；文件正文存储改为 PostgreSQL，发布规则以当前架构为准。
- 0003/0004 保留练习与配置历史，不表示主项目继续等设计或必须采用练习实现。
- 0005 删除副本与不自动恢复有效；“存储待定”仅为历史。
- 旧 M0—M6 草案及 Web 可选整体被本次方案取代，真实 M0 实现历史保留。

## 执行取舍

Ruling: 在用户指定目录的 feat/knowledgeops-v1 分支实施，不另建 worktree。已有未提交计划与配置且项目独立，避免遗漏；代价是没有额外 checkout 隔离，不得清理无关修改。

Ruling: 使用现有 progress、development-issues、decisions 作为执行账本，不增加技能默认的平行进度副本。用户要求统一长期文档并禁止提交，账本不能当可删除的临时文件；代价是这些入口必须持续记录任务、取舍与证据。

常规工程问题自主推进，重大范围/产品/权限/核心技术/费用变化再询问。禁止未经授权提交/推送、公开部署和删除正常数据。
