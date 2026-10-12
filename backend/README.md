# 后端

应用源码位于 `src/knowledgeops_agent/`。容器中的 Python 包名仍是 `knowledgeops_agent`；目录调整不改变 API、工具名或运行快照。

- API 入口：`knowledgeops_agent.api:app`，由 Uvicorn 提供 HTTP/SSE。
- 后台入口：`python -m knowledgeops_agent.worker`，消费 Celery 任务并运行 Agent。
- 环境与依赖：仓库根 `pyproject.toml`、`uv.lock` 和开发环境 `.venv`，避免维护两套锁文件。
- 数据库迁移、集成测试和评估：仓库根 `migrations/`、`tests/`、`evaluation/`，覆盖整个应用的协作边界。
- 部署资源：仓库根 `config/`、`skills/`；用户数据在专用挂载工作区。

从仓库根运行 `make dev`，在 dev 容器执行 Python 检查。Docker 运行模式使用镜像内依赖；启动方法见根 README。不提供宿主机 Python 部署。
