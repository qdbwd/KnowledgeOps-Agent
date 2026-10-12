"""备份恢复的确定性种子与核验，不调用模型、不覆写正常数据库。

seed 只允许空的专用备份测试库；verify 在新恢复库中核对原件哈希、引用、
历史、索引与检查点，并验证旧运行被标中断。恢复的费用账本只读检查。
"""

import argparse
import hashlib
import json
import re
import tarfile
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from knowledgeops_agent.config import Settings
from knowledgeops_agent.db import (
    Base,
    Evidence,
    KnowledgeVersion,
    LearningProfileRevision,
    Run,
    RunArtifact,
    SourceVersion,
)
from knowledgeops_agent.evaluation import ReportService
from knowledgeops_agent.model_gateway import call_links, requests, roots
from knowledgeops_agent.services import create_services
from knowledgeops_agent.worker import open_checkpointer


def settings_for(database: str, storage: Path, workspace: Path | None = None):
    if not re.fullmatch(r"knowledgeops_(backupfixture|restore)_[0-9a-f]{12}", database):
        raise ValueError("only a dedicated backup fixture or restore database is allowed")
    base = Settings(model_calls_enabled=False)
    url = make_url(base.database_url).set(database=database)
    return Settings(
        **{
            **base.model_dump(),
            "database_url": url.render_as_string(hide_password=False),
            "source_storage_root": storage,
            "workspace_root": workspace,
        }
    )


def seed(settings):
    from alembic import command
    from alembic.config import Config
    from langchain_core.messages import HumanMessage
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import MessagesState

    services = create_services(settings)
    try:
        with services.engine.begin() as connection:
            if connection.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
            ).scalar_one():
                raise ValueError("seed requires an empty database")
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        source = services.sources.import_text(
            "备份恢复验证材料", "独立卷保存原件，旧版本引用不可变。模型上限40次。"
        )
        chat = services.jobs.create_conversation("备份恢复固定演示")
        run = services.jobs.enqueue(
            chat["id"],
            "确定性的恢复验证种子（不调用模型）",
            request_key=str(uuid4()),
            publication_mode="preview",
        )
        services.jobs.claim("backup-fixture", run_id=run["id"], generation=run["generation"])
        # 新增控制数据也必须经过真实备份恢复；配置停用，不启动外部程序或模型。
        from knowledgeops_agent.mcp_registry import MCPRegistry
        from knowledgeops_agent.preferences import PreferenceService
        from knowledgeops_agent.run_resources import prepare_resources
        from knowledgeops_agent.runtime import RunContext

        PreferenceService(services.sessions).create(
            "先说明用途，再举例。", scope="global", topic=""
        )
        # 新学习配置正文在工作区，索引/来源在PG；必须作为一组恢复。
        # 只生成业务数据，不调度自动任务或使用模型。
        if settings.workspace_root is not None:
            from knowledgeops_agent.learning_profiles import LearningProfileService
            from knowledgeops_agent.learning_tasks import LearningTaskService

            task = LearningTaskService(services.sessions).create(
                conversation_id=chat["id"],
                request_key="backup-learning",
                title="备份中的学习任务",
                goal="恢复后仍能读取人工角色配置及其旧版本",
                units=[{"key": "one", "title": "事务", "goal": "给出并发示例"}],
            )
            profiles = LearningProfileService(services.sessions)
            for task_id in (None, task["id"]):
                profile = profiles.get(task_id=task_id)
                profiles.update(
                    profile["id"],
                    expected_revision=profile["revision"],
                    content="先展示具体场景，再解释设计取舍；保留人工正文。",
                )
        MCPRegistry(services.sessions).create(
            "备份中的停用连接", {"transport": "http", "url": "http://127.0.0.1:9999/mcp"}
        )
        prepare_resources(
            RunContext(
                run_id=run["id"],
                root_id=run["id"],
                worker_id="backup-fixture",
                jobs=services.jobs,
                knowledge=services.knowledge,
                sources=services.sources,
                gateway=services.gateway,
            )
        )
        evidence = services.sources.read_segment(source["segments"][0]["id"], root_id=run["id"])
        draft = services.knowledge.create_draft(
            run["id"],
            [
                {
                    "action": "create",
                    "title": "备份知识",
                    "body": "模型上限40次。" + evidence["citation"],
                }
            ],
        )
        services.knowledge.approve(draft["id"])
        receipt = services.knowledge.publish(draft["id"], idempotency_key="backup-fixture-create")
        document = services.knowledge.get_document(receipt["document_ids"][0])
        new = services.knowledge.create_draft(
            run["id"],
            [
                {
                    "action": "update",
                    "document_id": document["id"],
                    "base_version_id": document["version_id"],
                    "title": document["title"],
                    "body": document["body"] + "\n恢复后保留历史引用。",
                }
            ],
        )
        services.knowledge.approve(new["id"])
        services.knowledge.publish(new["id"], idempotency_key="backup-fixture-update")
        services.jobs.save_artifact(run["id"], "context_original", "备份恢复原始运行材料")
        with open_checkpointer(services) as saver:
            builder = StateGraph(MessagesState)
            builder.add_node("fixture", lambda state: {})
            builder.add_edge(START, "fixture")
            builder.add_edge("fixture", END)
            graph = builder.compile(checkpointer=saver)
            graph.invoke(
                {"messages": [HumanMessage("确定性检查点种子")]},
                {"configurable": {"thread_id": run["checkpoint_id"]}},
            )
        print(
            json.dumps(
                {
                    "database": services.engine.url.database,
                    "source": source["version_id"],
                    "run": run["id"],
                    "document": document["id"],
                    "model_calls": 0,
                }
            )
        )
    finally:
        services.close()


def verify(
    settings,
    accounting_database,
    archive: Path | None,
    output: Path,
    workspace_archive: Path | None = None,
):
    if accounting_database != make_url(settings.database_url).database + "_accounting":
        raise ValueError("only the matching isolated accounting copy may be verified")
    restore_root = settings.workspace_root if workspace_archive else settings.source_storage_root
    if restore_root is None:
        raise ValueError("workspace archive requires an explicit isolated workspace root")
    restore_root.mkdir(parents=True, exist_ok=True)
    if any(restore_root.iterdir()):
        raise ValueError("restore source directory must be empty")
    # Python 3.12 data filter拒绝逃逸路径/危险链接，归档只能展开到新测试卷。
    with tarfile.open(workspace_archive or archive) as packed:
        packed.extractall(restore_root, filter="data")
    services = create_services(settings)
    try:
        # 旧格式备份可能来自早期表结构，先只升级这个明确隔离的恢复库。
        # 不能用今天的 ORM 直接读取尚未拥有新增字段的旧 schema。
        from alembic import command
        from alembic.config import Config

        with services.engine.begin() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        tables = {}
        with services.sessions() as session:
            for name, table in Base.metadata.tables.items():
                tables[name] = session.scalar(select(func.count()).select_from(table))
            versions = list(session.scalars(select(SourceVersion)))
            evidence_ids = list(session.scalars(select(Evidence.id)))
            runs = list(session.scalars(select(Run)))
            historical = list(session.scalars(select(KnowledgeVersion)))
            # 在 Session 关闭前读取全部文件正文，验证引用路径和内容哈希。
            for version in historical:
                _ = version.body
            for artifact in session.scalars(select(RunArtifact)):
                _ = artifact.content
            profile_files = 0
            for profile in session.scalars(select(LearningProfileRevision)):
                session.info["file_store"].read(profile.storage_key, profile.sha256)
                profile_files += 1
        file_checks = []
        for version in versions:
            data, _ = services.sources.original(version.id)
            file_checks.append(
                {
                    "version_id": version.id,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "matches": hashlib.sha256(data).hexdigest() == version.content_hash,
                }
            )
        citations = [services.sources.get_evidence(eid) for eid in evidence_ids]
        current = services.knowledge.list_documents(include_archived=True)
        before_search = {row["version_id"] for row in services.knowledge.search("模型上限")}
        index_count = services.knowledge.rebuild_index()
        after_search = {row["version_id"] for row in services.knowledge.search("模型上限")}
        with open_checkpointer(services) as saver:
            checkpoints = sum(
                saver.get_tuple({"configurable": {"thread_id": row.checkpoint_id}}) is not None
                for row in runs
            )
        interrupted = services.jobs.recover_on_startup()
        assert all(services.jobs.get_run(row.id)["status"] != "running" for row in runs)
        account_url = make_url(settings.accounting_database_url).set(database=accounting_database)
        restored_account_engine = create_engine(account_url)
        try:
            # 恢复核对的是任务预算/回执/重放表，不再以累计用量为健康依据。
            with restored_account_engine.connect() as connection:
                for table in (roots, requests, call_links):
                    connection.execute(select(table).limit(1)).first()
        finally:
            restored_account_engine.dispose()
        checks = {
            "all_original_hashes": all(row["matches"] for row in file_checks),
            "all_citations_resolve": len(citations) == tables["evidence"],
            "history_preserved": len(historical) >= len(current),
            "all_learning_profile_revisions_readable": profile_files
            == tables["learning_profile_revisions"],
            "index_rebuild_equivalent": before_search == after_search,
            "no_automatic_resume": interrupted >= 0,
            "model_receipts_readable": True,
            "optimization_controls_restored": not any(
                row.task == "确定性的恢复验证种子（不调用模型）" for row in runs
            )
            or all(
                tables.get(name, 0) > 0
                for name in (
                    "preferences",
                    "preference_revisions",
                    "mcp_connections",
                    "mcp_connection_revisions",
                    "run_resource_snapshots",
                )
            ),
        }
        result = {
            "database": services.engine.url.database,
            "checks": checks,
            "tables": tables,
            "files": file_checks,
            "checkpoints_found": checkpoints,
            "learning_profile_files": profile_files,
            "interrupted_runs": interrupted,
            "index_count": index_count,
            "evaluation_reports": len(ReportService(services.sessions).list()),
            "model_calls": 0,
        }
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        if not all(checks.values()):
            raise RuntimeError("restored data checks failed")
        print(json.dumps({"restored": result["database"], "checks": checks, "report": str(output)}))
    finally:
        services.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["seed", "verify"])
    parser.add_argument("--database", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--accounting-database")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--workspace-archive", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    settings = settings_for(args.database, args.source_root, args.workspace_root)
    if args.mode == "seed":
        seed(settings)
    else:
        verify(
            settings, args.accounting_database, args.archive, args.output, args.workspace_archive
        )


if __name__ == "__main__":
    main()
