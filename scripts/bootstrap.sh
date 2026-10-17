#!/bin/sh
# 容器内初始化入口。失败即停止，Compose 不会在依赖或迁移失败时启动应用。
set -eu
cd /workspace
if [ "${1:-production}" = "development" ]; then
    uv sync --locked --group dev
    (cd frontend && npm ci)
fi
export MODEL_CALLS_ENABLED=false
alembic upgrade head
python scripts/migrate_accounting.py --lifecycle
python scripts/migrate_workspace.py --legacy-source-root /data/sources --enable-new-tools
