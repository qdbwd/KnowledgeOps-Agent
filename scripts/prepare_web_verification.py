"""建立真实 Web 联调的隔离数据空间，不重置业务数据或收费调用账本。

创建后输出不含密钥的 Compose 覆盖文件。API/worker 使用测试库的新 schema
和项目 .local 下独立原件目录；账本沿用原独立数据库，收费请求始终累计。
"""

import argparse
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="web-verification", help="新验证清单名；不会覆盖旧记录")
    parser.add_argument("--online", action="store_true", help="仅显式指定时开启已授权模型请求")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9-]{1,64}", args.name):
        parser.error("name must contain lowercase letters, digits or hyphens")
    root = Path("/workspace")
    local = root / ".local"
    local.mkdir(exist_ok=True)
    override = local / f"{args.name}.compose.yaml"
    if override.exists():
        raise RuntimeError(
            "verification workspace already exists; inspect its manifest before reusing"
        )
    schema = "verify_" + uuid4().hex
    admin = create_engine(os.environ["TEST_DATABASE_URL"])
    if admin.url.database != "knowledgeops_test":
        raise RuntimeError("TEST_DATABASE_URL must point to knowledgeops_test")
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(admin.url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as connection:
        config = Config(str(root / "alembic.ini"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    directory = local / schema / "sources"
    directory.mkdir(parents=True)
    # 保留 Compose 变量引用，不把 POSTGRES_PASSWORD 的真实值写入产物。
    url = (
        "postgresql+psycopg://knowledgeops:${POSTGRES_PASSWORD}@postgres:5432/knowledgeops_test?options=-csearch_path%3D"
        + schema
    )
    service = (
        "    environment:\n"
        f"      DATABASE_URL: {json.dumps(url)}\n"
        f'      MODEL_CALLS_ENABLED: "{str(args.online).lower()}"\n'
        "    volumes:\n"
        f'      - "./.local/{schema}/sources:/data/sources"\n'
    )
    override.write_text("services:\n  api:\n" + service + "  worker:\n" + service, encoding="utf-8")
    manifest = {
        "schema": schema,
        "database": "knowledgeops_test",
        "source_directory": str(directory),
        "override": str(override),
        "model_accounting": "original project accounting database",
    }
    (local / f"{args.name}.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    engine.dispose()
    admin.dispose()
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
