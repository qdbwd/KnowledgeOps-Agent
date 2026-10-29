"""在维护窗口将旧正文/产物/原件迁入挂载区；不调用模型或删除旧卷。"""

import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.harness.tools.catalog import tool_options
from knowledgeops_agent.harness.tools.policy import ToolPolicyService
from knowledgeops_agent.knowledge.files import refresh_working_files, workspace_lock
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.persistence.workspace import migrate_content, sync_knowledge_projection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-source-root", type=Path)
    parser.add_argument("--enable-new-tools", action="store_true")
    args = parser.parse_args()
    settings = Settings(model_calls_enabled=False)
    if settings.workspace_root is None:
        raise ValueError("WORKSPACE_ROOT must be configured")
    engine = create_engine(settings.database_url)
    sessions = sessionmaker(
        engine, expire_on_commit=False, info={"file_store": FileStore(settings.workspace_root)}
    )
    try:
        for name in (
            "work",
            "outputs",
            "knowledge/documents",
            "knowledge/versions",
            "uploads/originals",
            "artifacts",
        ):
            (settings.workspace_root / name).mkdir(parents=True, exist_ok=True)
        result = migrate_content(sessions, legacy_source_root=args.legacy_source_root)
        with workspace_lock(sessions):
            refresh_working_files(sessions)
        result["projection"] = sync_knowledge_projection(sessions)
        if args.enable_new_tools:
            policy = ToolPolicyService(sessions)
            current = policy.get()
            # 只初始化从未配置的新项；重复启动不复活用户关闭的工具。
            enabled = {
                row["id"]: True
                for row in tool_options()
                if row["condition"] == "workspace" and row["id"] not in current["enabled"]
            }
            if enabled:
                current = policy.update(expected_revision=current["revision"], enabled=enabled)
            result["tool_policy_revision"] = current["revision"]
        print(json.dumps(result, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    from knowledgeops_agent.persistence.maintenance import managed_main

    managed_main(main)
