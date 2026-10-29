#!/bin/sh
# 容器内初始化入口。失败即停止，Compose 不会在依赖或迁移失败时启动应用。
set -eu
cd /workspace
if [ "${1:-production}" = "development" ]; then
    uv sync --locked --group dev
    (cd frontend && npm ci)
fi
export MODEL_CALLS_ENABLED=false
if [ "${1:-production}" != "guarded" ]; then
    exec python -m knowledgeops_agent.persistence.maintenance -- sh scripts/bootstrap.sh guarded
fi
alembic upgrade head
python scripts/initialize_skills.py
python scripts/migrate_accounting.py --lifecycle
python scripts/migrate_workspace.py --legacy-source-root /data/sources --enable-new-tools
