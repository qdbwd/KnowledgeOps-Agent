"""备份恢复的确定性种子与核验，不调用模型、不覆写正常数据库。

seed 只允许空的专用备份测试库；verify 在新恢复库中核对原件哈希、引用、
历史、索引与检查点，并验证旧运行被标中断。回执副本只补兼容结构，
不封闭旧根、不改变预算，不连接正式账本进行迁移。
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

from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.persistence.backup import file_hash, verified_manifest
from knowledgeops_agent.persistence.models import (
    Base,
    DeliveredFile,
    Evidence,
    KnowledgeVersion,
    LearningProfileRevision,
    Run,
    RunArtifact,
    Source,
    SourceSegment,
    SourceVersion,
)


def settings_for(database: str, storage: Path, workspace: Path | None = None):
    if not re.fullmatch(r"knowledgeops_(backupfixture|restore)_[0-9a-f]{12}", database):
        raise ValueError("only a dedicated backup fixture or restore database is allowed")
    base = Settings(model_calls_enabled=False)
    url = make_url(base.database_url).set(database=database)
    return Settings(
        **{
            **base.model_dump(),
            "database_url": url.render_as_string(hide_password=False),
            "accounting_database_url": make_url(base.accounting_database_url).set(
                database=database + "_accounting"
            ).render_as_string(hide_password=False),
            "source_storage_root": storage,
            "workspace_root": workspace,
            # 恢复演练只核对持久数据，不向正常事件流/broker发送任何任务。
            "runtime_redis_url": "",
        }
    )


def seed_legacy(settings):
    """真实0030旧表种子，用于证明预览不要求先迁移，恢复后也不重提取正文。"""
    from alembic import command
    from alembic.config import Config

    from knowledgeops_agent.harness.models.gateway import prepare_accounting
    from knowledgeops_agent.persistence.files import FileStore

    engine = create_engine(settings.database_url)
    accounting = create_engine(settings.accounting_database_url)
    try:
        with engine.begin() as connection:
            if connection.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")).scalar_one():
                raise ValueError("seed requires an empty database")
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0030")
            body = "旧网页正文与引用保持不变。"
            raw = b"<html><body>legacy original for backup</body></html>"
            digest = hashlib.sha256(raw).hexdigest()
            store = FileStore(settings.source_storage_root)
            key = digest[:2] + "/" + digest
            store.put_bytes(key, raw)
            connection.execute(text("""INSERT INTO sources(id,identity_key,title,kind,origin,created_at)
                VALUES ('legacy-source','web:https://example.org/legacy','旧网页','web','https://example.org/legacy',now())"""))
            connection.execute(text("""INSERT INTO source_versions
                (id,source_id,number,content_hash,storage_key,mime_type,status,parser,created_at)
                VALUES ('legacy-version','legacy-source',1,:digest,:key,'text/html','ready','trafilatura-v2',now())"""),
                {"digest": digest, "key": key})
            connection.execute(text("""INSERT INTO source_segments
                (id,source_version_id,position,start_offset,end_offset,text,method,status)
                VALUES ('legacy-segment','legacy-version',0,0,:end,:body,'text','ready')"""), {"end": len(body), "body": body})
            connection.execute(text("""INSERT INTO evidence(id,root_id,source_segment_id,start_offset,end_offset,quote,created_at)
                VALUES ('legacy-evidence','legacy-root','legacy-segment',0,:end,:body,now())"""), {"end": len(body), "body": body})
        prepare_accounting(accounting)
        with accounting.begin() as connection:
            connection.execute(text("ALTER TABLE model_root_budgets DROP COLUMN closed_at"))
        print(json.dumps({"database": engine.url.database, "schema": "0030", "model_calls": 0}))
    finally:
        engine.dispose()
        accounting.dispose()


def seed(settings):
    from alembic import command
    from alembic.config import Config
    from langchain_core.messages import HumanMessage
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import MessagesState

    from knowledgeops_agent.app.runs.worker import open_checkpointer
    from knowledgeops_agent.app.services import create_services

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
        services.sources.import_text("无HTML网页", "# 固定网页正文\n\n备份应校验正文。",
                                     kind="web", origin="https://example.org/backup")
        legacy = services.sources.import_text("旧网页引用", "旧正文必须保持。", kind="file",
            raw_bytes=b"<html>old original</html>", mime_type="text/html", parser="trafilatura-v2")
        with services.sessions.begin() as session:
            session.get(Source, legacy["id"]).kind = "web"
            version = session.get(SourceVersion, legacy["version_id"])
            version.body_hash = version.body_format = version.original_hash = None
        chat = services.jobs.create_conversation("备份恢复固定演示")
        from knowledgeops_agent.harness.permissions.settings import ExecutionSettings

        ExecutionSettings(services.sessions).update(
            chat["id"], expected_revision=0, permission_mode="auto_review"
        )
        run = services.jobs.enqueue(
            chat["id"],
            "确定性的恢复验证种子（不调用模型）",
            request_key=str(uuid4()),
            publication_mode="preview",
        )
        services.jobs.claim("backup-fixture", run_id=run["id"], generation=run["generation"])
        # 新增控制数据也必须经过真实备份恢复；配置停用，不启动外部程序或模型。
        from knowledgeops_agent.harness.mcp.registry import MCPRegistry
        from knowledgeops_agent.harness.runtime.agent import RunContext
        from knowledgeops_agent.harness.runtime.resources import content_hash, prepare_resources
        from knowledgeops_agent.harness.subagents.registry import SubagentRegistry
        from knowledgeops_agent.harness.tools.groups import ToolGroupService
        from knowledgeops_agent.harness.tools.policy import ToolPolicyService
        from knowledgeops_agent.memory.categories import MemoryCategories
        from knowledgeops_agent.memory.service import PreferenceService

        category = MemoryCategories(services.sessions).create("恢复验证分类")
        memory = PreferenceService(services.sessions).create(
            "先说明用途，再举例。", scope="global", topic="", display_category_id=category["id"],
        )
        group = ToolGroupService(services.sessions).create(
            name="恢复验证工具组", tool_ids=["builtin:read_source"],
        )
        registry, policy = SubagentRegistry(services.sessions), ToolPolicyService(services.sessions)
        role = registry.create(
            name="backup-reader", display_name="备份中的阅读者", description="核对固定材料",
            system_prompt="只读取明确指定的材料并返回证据。", enabled=True,
            tools=[], tool_groups=[group["id"]], skills=[],
        )
        policy.update(expected_revision=policy.get()["revision"], main_tools=[], main_tool_groups=[group["id"]])
        # 新学习配置正文在工作区，索引/来源在PG；必须作为一组恢复。
        # 只生成业务数据，不调度自动任务或使用模型。
        if settings.workspace_root is not None:
            from knowledgeops_agent.learning.profiles import LearningProfileService
            from knowledgeops_agent.learning.tasks import LearningTaskService

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
        manifest = prepare_resources(
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
        # 冻结后再编辑当前配置，恢复必须同时保住新配置和旧任务使用的精确修订。
        current_role = registry.update(role["id"], expected_revision=1, display_name="备份后的新名称")
        current_policy = policy.update(expected_revision=manifest["tool_policy"]["revision"],
                                       enabled={"builtin:read_source": True})
        with services.sessions.begin() as session:
            row = session.get(Run, run["id"])
            row.input_data = {**row.input_data, "backup_increment": {
                "version": 2, "memory_id": memory["id"], "category_id": category["id"],
                "group_id": group["id"], "group_hash": content_hash(group), "role_id": role["id"],
                "current_role_hash": content_hash(current_role),
                "current_policy_hash": content_hash(current_policy),
                "frozen_role_hash": content_hash(role),
                "frozen_policy_hash": content_hash(manifest["tool_policy"]),
                "frozen_policy_revision": manifest["tool_policy"]["revision"],
            }}
        evidence = services.sources.read_segment(source["segments"][0]["id"], root_id=run["id"])
        legacy_evidence = services.sources.read_segment(legacy["segments"][0]["id"], root_id=run["id"])
        draft = services.knowledge.create_draft(
            run["id"],
            [
                {
                    "action": "create",
                    "title": "备份知识",
                    "body": "模型上限40次。" + evidence["citation"] + "\n旧正文必须保持。" + legacy_evidence["citation"],
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
        if settings.workspace_root is not None:
            from knowledgeops_agent.app.lifecycle.promotion import promote_material
            from knowledgeops_agent.sources.deliveries import DeliveryService

            delivered = settings.workspace_root / "outputs/backup.md"
            delivered.parent.mkdir(parents=True, exist_ok=True)
            delivered.write_text("# 固定交付\n恢复后仍是第一版", encoding="utf-8")
            DeliveryService(services.sessions, settings.workspace_root).present(
                ["outputs/backup.md"], run_id=run["id"], root_id=run["id"], call_id="backup-delivery")
            delivered.write_text("这是后续工作内容，不能改变旧交付。", encoding="utf-8")
            alias = services.jobs.save_artifact(run["id"], "tool_result", "被引用的内部文本转换为固定材料。")
            with services.sessions.begin() as session:
                promote_material(session, session.get(RunArtifact, alias))
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


def verify_citations(session) -> list[str]:
    """能查到Evidence身份不等于引用正确；逐项核对固定版本中的原位置和原文字节。"""
    checked = []
    for row in session.scalars(select(Evidence)):
        if row.source_segment_id:
            segment = session.get(SourceSegment, row.source_segment_id)
            if segment is None:
                raise ValueError("restored citation segment is missing")
            start, end = row.start_offset - segment.start_offset, row.end_offset - segment.start_offset
            body = segment.text
        else:
            version = session.get(KnowledgeVersion, row.knowledge_version_id)
            if version is None:
                raise ValueError("restored citation knowledge version is missing")
            body, start, end = version.body, row.start_offset, row.end_offset
        if not 0 <= start <= end <= len(body) or body[start:end] != row.quote:
            raise ValueError("restored citation does not match its saved body")
        checked.append(row.id)
    return checked


def inspect_html_fixture(settings, output, *, backup_folder=None):
    """隔离演练的只读清单/显式整理，不是正常库维护入口。"""
    from knowledgeops_agent.app.lifecycle.legacy_html import LegacyHtmlCleaner
    from knowledgeops_agent.persistence.files import FileStore
    from knowledgeops_agent.persistence.models import make_sessions

    sessions = make_sessions(settings.database_url, workspace_root=settings.workspace_root)
    sessions.configure(info={**sessions.kw["info"], "source_store": FileStore(settings.source_storage_root)})
    cleaner = LegacyHtmlCleaner(sessions)
    try:
        report = cleaner.preview()
        if backup_folder:
            if not sessions.kw["bind"].url.database.startswith("knowledgeops_backupfixture_"):
                raise ValueError("automatic fixture cleanup may only target a backupfixture database")
            jobs = []
            for item in report["items"]:
                job = cleaner.admit(item, backup_folder=backup_folder)
                for _ in range(2):
                    job = cleaner.step(identity=job["id"])
                    if not job or job["status"] == "failed":
                        raise RuntimeError("fixture HTML cleanup failed")
                if job["status"] != "completed":
                    raise RuntimeError("fixture HTML cleanup did not complete")
                jobs.append(job)
            report["jobs"] = jobs
            report["remaining"] = cleaner.preview()["items"]
        output.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
        print(json.dumps({"database": report["database"], "html_versions": len(report["items"]), "output": str(output)}))
    finally:
        sessions.kw["bind"].dispose()


def verify_increment_configuration(services, runs) -> dict[str, bool]:
    """实际恢复后通过业务读取/绑定校验核对新增配置；旧备份没有此夹具标记。"""
    from knowledgeops_agent.harness.permissions.settings import ExecutionSettings
    from knowledgeops_agent.harness.runtime.resources import content_hash
    from knowledgeops_agent.harness.subagents.registry import SubagentRegistry
    from knowledgeops_agent.harness.tools.groups import ToolGroupService
    from knowledgeops_agent.harness.tools.policy import ToolPolicyService
    from knowledgeops_agent.memory.categories import MemoryCategories
    from knowledgeops_agent.memory.service import PreferenceService
    from knowledgeops_agent.persistence.models import RunResourceSnapshot

    results = {}
    for run in runs:
        expected = run.input_data.get("backup_increment")
        if not expected:
            continue
        if expected.get("version") != 2:
            raise ValueError("unsupported backup fixture configuration version")
        with services.sessions() as session:
            snapshot = session.get(RunResourceSnapshot, run.id).content
        memory = PreferenceService(services.sessions).get(expected["memory_id"])
        category = next(item for item in MemoryCategories(services.sessions).list()
                        if item["id"] == expected["category_id"])
        group = next(item for item in ToolGroupService(services.sessions).list()
                     if item["id"] == expected["group_id"])
        registry, policy = SubagentRegistry(services.sessions), ToolPolicyService(services.sessions)
        old_role = next(item for item in snapshot["subagents"] if item["id"] == expected["role_id"])
        registry.validate_binding(old_role)
        allowed = policy.allowed_ids(snapshot["tool_policy"], is_root=True)
        results.update({
            "dynamic_memory_category": memory["display_category_id"] == category["id"]
                and category["name"] == "恢复验证分类" and category["memory_count"] == 1,
            "memory_content_unchanged": memory["content"] == "先说明用途，再举例。",
            "editable_tool_group": content_hash(group) == expected["group_hash"],
            "current_role": content_hash(registry.get(expected["role_id"])) == expected["current_role_hash"],
            "current_policy": content_hash(policy.get()) == expected["current_policy_hash"],
            "frozen_role": content_hash(old_role) == expected["frozen_role_hash"] and old_role["revision"] == 1,
            "frozen_policy": content_hash(snapshot["tool_policy"]) == expected["frozen_policy_hash"]
                and snapshot["tool_policy"]["revision"] == expected["frozen_policy_revision"]
                and policy.get()["revision"] == expected["frozen_policy_revision"] + 1
                and allowed == {"builtin:read_source"},
            "execution_mode": ExecutionSettings(services.sessions).get(run.conversation_id)["permission_mode"]
                == "auto_review",
        })
    return results


def verify(
    settings,
    accounting_database,
    archive: Path | None,
    output: Path,
    workspace_archive: Path | None = None,
    backup_manifest: Path | None = None,
):
    from knowledgeops_agent.app.runs.worker import open_checkpointer
    from knowledgeops_agent.app.services import create_services
    from knowledgeops_agent.diagnostics.reports import ReportService
    from knowledgeops_agent.harness.models.gateway import (
        call_links,
        requests,
        roots,
        upgrade_accounting_lifecycle,
    )

    manifest = verified_manifest(backup_manifest.parent) if backup_manifest else None
    if manifest:
        expected_archive = "workspace.tar.gz" if workspace_archive else "sources.tar.gz"
        if file_hash(workspace_archive or archive) != manifest["files"].get(expected_archive):
            raise ValueError("restore archive does not match the verified backup")
    if accounting_database != make_url(settings.database_url).database + "_accounting":
        raise ValueError("only the matching isolated accounting copy may be verified")
    if (not re.fullmatch(r"knowledgeops_restore_[0-9a-f]{12}_accounting", accounting_database)
        or make_url(settings.accounting_database_url).database != accounting_database):
        raise ValueError("restore verification requires the explicitly isolated accounting copy")
    restore_root = settings.workspace_root if workspace_archive else settings.source_storage_root
    if restore_root is None:
        raise ValueError("workspace archive requires an explicit isolated workspace root")
    restore_root.mkdir(parents=True, exist_ok=True)
    if any(restore_root.iterdir()):
        raise ValueError("restore source directory must be empty")
    # Python 3.12 data filter拒绝逃逸路径/危险链接，归档只能展开到新测试卷。
    with tarfile.open(workspace_archive or archive) as packed:
        packed.extractall(restore_root, filter="data")
    accounting_engine = create_engine(settings.accounting_database_url)
    try:
        accounting_migration = upgrade_accounting_lifecycle(accounting_engine)
    finally:
        accounting_engine.dispose()
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
            deliveries = list(session.scalars(select(DeliveredFile)))
            evidence_ids = verify_citations(session)
            runs = list(session.scalars(select(Run)))
            before_states = {row.id: {"status": row.status, "generation": row.generation} for row in runs}
            historical = list(session.scalars(select(KnowledgeVersion)))
            # 在 Session 关闭前读取全部文件正文，验证引用路径和内容哈希。
            for version in historical:
                _ = version.body
            for artifact in session.scalars(select(RunArtifact)):
                if not artifact.expired_at or artifact.promoted_version_id:
                    _ = artifact.content
            profile_files = 0
            for profile in session.scalars(select(LearningProfileRevision)):
                session.info["file_store"].read(profile.storage_key, profile.sha256)
                profile_files += 1
        file_checks, body_checks = [], []
        for version in versions:
            body = services.sources.full_content(version.id)
            body_checks.append({
                "version_id": version.id,
                "source_id": version.source_id,
                "sha256": hashlib.sha256(body["text"].encode("utf-8")).hexdigest(),
                "integrity": body["integrity"],
            })
            # 网页新版本没有原件；旧备份及上传文件仍逐字节校验，不能跳过缺失文件。
            if version.storage_key:
                data, _ = services.sources.original(version.id)
                file_checks.append({
                    "version_id": version.id,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "matches": hashlib.sha256(data).hexdigest() == (version.original_hash or version.content_hash),
                })
        citations = [services.sources.get_evidence(eid) for eid in evidence_ids]
        delivery_checks = []
        if deliveries:
            from knowledgeops_agent.sources.deliveries import DeliveryService

            if settings.workspace_root is None:
                raise ValueError("delivered files require a full workspace backup")
            delivery_service = DeliveryService(services.sessions, settings.workspace_root)
            for delivered in deliveries:
                delivery_service.read(delivered.id)
                delivery_checks.append(delivered.id)
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
        after_states = {row.id: services.jobs.get_run(row.id) for row in runs}
        stopped = {"running", "queued", "waiting_children", "waiting_input"}
        no_resume = all(
            after_states[identity]["generation"] == prior["generation"]
            and after_states[identity]["status"] not in stopped
            and (prior["status"] in stopped or after_states[identity]["status"] == prior["status"])
            for identity, prior in before_states.items()
        )
        account_url = make_url(settings.accounting_database_url).set(database=accounting_database)
        restored_account_engine = create_engine(account_url)
        try:
            # 恢复核对的是任务预算/回执/重放表，不再以累计用量为健康依据。
            with restored_account_engine.connect() as connection:
                for table in (roots, requests, call_links):
                    connection.execute(select(table).limit(1)).first()
        finally:
            restored_account_engine.dispose()
        increment_configuration = verify_increment_configuration(services, runs)
        checks = {
            "all_original_hashes": all(row["matches"] for row in file_checks),
            "all_saved_bodies_readable": len(body_checks) == len(versions),
            "all_delivered_files_readable": len(delivery_checks) == len(deliveries),
            "all_citations_resolve": len(citations) == tables["evidence"],
            "history_preserved": len(historical) >= len(current),
            "all_learning_profile_revisions_readable": profile_files
            == tables["learning_profile_revisions"],
            "index_rebuild_equivalent": before_search == after_search,
            "no_automatic_resume": no_resume,
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
        if increment_configuration:
            checks["increment_configuration_restored"] = all(increment_configuration.values())
        result = {
            "database": services.engine.url.database,
            "source_database": manifest["source_database"] if manifest else None,
            "backup_manifest_sha256": manifest["manifest_sha256"] if manifest else None,
            "checks": checks,
            "increment_configuration": increment_configuration,
            "tables": tables,
            "files": file_checks,
            "bodies": body_checks,
            "delivered_files": delivery_checks,
            "checkpoints_found": checkpoints,
            "learning_profile_files": profile_files,
            "interrupted_runs": interrupted,
            "run_recovery": [{"id": identity, "before": prior["status"], "after": after_states[identity]["status"],
                              "generation": after_states[identity]["generation"]} for identity, prior in before_states.items()],
            "index_count": index_count,
            "evaluation_reports": len(ReportService(services.sessions).list()),
            "model_calls": 0,
            "accounting_migration": accounting_migration,
        }
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        if not all(checks.values()):
            raise RuntimeError("restored data checks failed")
        print(json.dumps({"restored": result["database"], "checks": checks, "report": str(output)}))
    finally:
        services.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["seed", "seed-legacy", "verify", "inspect-html", "clean-html"])
    parser.add_argument("--database", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--accounting-database")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--workspace-archive", type=Path)
    parser.add_argument("--backup-manifest", type=Path)
    parser.add_argument("--backup-folder", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    settings = settings_for(args.database, args.source_root, args.workspace_root)
    if args.mode in {"inspect-html", "clean-html"}:
        if not args.output or (args.mode == "clean-html" and not args.backup_folder):
            parser.error("HTML fixture modes require output; clean-html also requires backup-folder")
        inspect_html_fixture(settings, args.output, backup_folder=args.backup_folder if args.mode == "clean-html" else None)
    elif args.mode == "seed-legacy":
        seed_legacy(settings)
    elif args.mode == "seed":
        seed(settings)
    else:
        verify(
            settings, args.accounting_database, args.archive, args.output, args.workspace_archive, args.backup_manifest
        )


if __name__ == "__main__":
    main()
