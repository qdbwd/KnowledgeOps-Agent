"""浏览器验收专用 API：独立临时 schema/原件目录，无真实模型、无 Redis 投递。

仅由 Playwright 启动。结束时只移除此进程创建的 UUID schema，不连接业务库
或验收账本。数据库结构使用正式 Alembic 迁移，页面与接口均使用生产实现。
"""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.app.application import create_app
from knowledgeops_agent.app.runs.service import RunService
from knowledgeops_agent.app.services import Services
from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.diagnostics.reports import ReportService
from knowledgeops_agent.harness.models.gateway import ModelGateway, ModelLedger, prepare_accounting
from knowledgeops_agent.knowledge.service import KnowledgeService
from knowledgeops_agent.persistence.models import Run, RunEvent
from knowledgeops_agent.sources.service import SourceService


def main():
    admin = create_engine(os.environ["TEST_DATABASE_URL"])
    if admin.url.database != "knowledgeops_test":
        raise RuntimeError("browser tests require the isolated knowledgeops_test database")
    schema = "browser_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(admin.url, connect_args={"options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=15000"})
    jobs = None
    try:
        with engine.begin() as connection:
            config = Config("/workspace/alembic.ini")
            config.set_main_option("script_location", "/workspace/migrations")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        prepare_accounting(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        with TemporaryDirectory(prefix="knowledgeops-browser-") as storage:
            from knowledgeops_agent.persistence.files import FileStore

            sessions.configure(info={"file_store": FileStore(Path(storage) / "workspace")})
            jobs, knowledge = RunService(sessions), KnowledgeService(sessions)
            sources = SourceService(sessions, Path(storage))
            settings = Settings(
                _env_file=None,
                model_calls_enabled=False,
                workspace_root=Path(storage) / "workspace",
                source_storage_root=Path(storage),
            )
            services = Services(
                settings,
                engine,
                sessions,
                jobs,
                knowledge,
                sources,
                ModelGateway(ModelLedger(engine, scope=schema), enabled=False, client=None),
            )
            outputs = settings.workspace_root / 'outputs'
            outputs.mkdir(parents=True, exist_ok=True)
            (outputs / 'reader-example.py').write_text('def example():\n    value = 1\n    return value\n', encoding='utf-8')
            (outputs / 'reader-note.md').write_text('# 完整文档预览\n\n这是可阅读的完整Markdown正文。', encoding='utf-8')
            from knowledgeops_agent.sources.deliveries import DeliveryService

            delivered = outputs / '已交付笔记.md'
            delivered.write_text('# 交付时的正文\n\n后续工作文件变化不影响这份结果。', encoding='utf-8')
            DeliveryService(sessions, settings.workspace_root).present(
                ['outputs/已交付笔记.md'], run_id='browser-delivery', root_id='browser-delivery', call_id='seed')
            delivered.write_text('后来修改的工作内容，不能出现在固定交付中。', encoding='utf-8')
            sources.import_text('网页阅读示例', '## 保存的网页正文\n\n这是抓取时的固定正文，不会随网站修改而改变。\n\n'
                                '```python\nresult = graph.invoke(inputs, config)\n```\n\n'
                                '| 状态 | 含义 |\n| --- | --- |\n| ready | 可以读取 |\n',
                                kind='web', origin='https://example.org/reader', body_format='markdown')
            source = sources.import_text(
                "预算规则原始材料",
                "根任务预算为 40 次请求。主 Agent 与研究子 Agent 共享该预算，重试也计入。",
            )
            conversation = jobs.create_conversation("固定浏览器验收")
            run = jobs.enqueue(
                conversation["id"],
                "整理共享预算规则，先预览",
                request_key=str(uuid4()),
                publication_mode="preview",
            )
            jobs.claim("browser-seed", run_id=run["id"], generation=run["generation"])
            evidence = sources.read_segment(source["segments"][0]["id"], root_id=run["id"])
            draft = knowledge.create_draft(
                run["id"],
                [
                    {
                        "action": "create",
                        "title": "Agent 共享预算规则",
                        "category": "Agent 工程",
                        "tags": ["预算", "运行控制"],
                        "body": "## 为什么需要共享预算\n\n主任务和子任务不能分别领取整套额度。根任务的 40 次请求由主 Agent、研究子 Agent 和重试共同消耗。"
                        + evidence["citation"]
                        + "\n\n例如主 Agent 使用 12 次，研究者使用 8 次，则剩余 20 次。",
                    }
                ],
            )
            # 长事件夹具覆盖分页/导出边界，不发出真实模型请求。
            with sessions.begin() as session:
                row = session.get(Run, run["id"])
                for _ in range(1001):
                    row.event_sequence += 1
                    session.add(
                        RunEvent(
                            run_id=row.id,
                            sequence=row.event_sequence,
                            kind="model_delta",
                            data={"text": "字"},
                        )
                    )
            jobs.finish(
                run["id"],
                "browser-seed",
                outcome={
                    "kind": "draft_ready",
                    "answer": "草稿已准备好，请审阅。",
                    "draft_ids": [draft["id"]],
                },
            )
            # 仅隔离浏览器夹具：确定性用量用于贯通账本、API和百分比展示，无模型请求。
            reservation = services.gateway.ledger.reserve(
                run["id"],
                run["id"],
                "agent",
                {
                    "capacity": services.gateway.capacity.measure(
                        {"messages": [{"role": "user", "content": "fixture"}]}
                    )
                },
            )
            services.gateway.ledger.finish(
                reservation,
                response={"content": "offline browser fixture"},
                usage={"input_tokens": 131072, "output_tokens": 10, "total_tokens": 131082},
            )
            jobs.save_artifact(run["id"], "context_original", "分页原文证据" * 1200)
            reports = ReportService(sessions)
            report = reports.create(
                {"kind": "business", "expected_count": 1, "model": "offline browser fixture"}
            )
            reports.append(
                report["id"],
                "K01-single-1",
                {
                    "case_id": "K01",
                    "title": "浏览器固定评估",
                    "mode": "single",
                    "attempt": 1,
                    "run": {
                        **run,
                        "outcome": {"answer": "原始回答：40次。" + evidence["citation"]},
                    },
                    "checks": {"fact": True},
                    "hard_checks": {"citation_resolves": True},
                    "citations": [sources.get_evidence(evidence["id"])],
                    "drafts": [],
                    "children": [],
                    "models": [],
                    "events": [],
                    "metrics": {"model_requests": 0},
                    "judge": {"score": 100, "supported": True},
                },
            )
            app = create_app(services=services, publish_jobs=False)
            from fixtures.learning_provider import LearningProvider
            from langgraph.checkpoint.memory import InMemorySaver

            from knowledgeops_agent.app.runs.worker import execute_run

            offline_provider, offline_saver = LearningProvider(), InMemorySaver()
            async_provider = None
            input_providers = {}

            @app.post("/api/__offline/memory-classifier/{mode}")
            def configure_memory_classifier(mode: str):
                # 仅测试服务器可配置替身；生产应用没有此路由，绝不创建供应商客户端。
                from langchain_core.messages import AIMessage

                if mode not in {"disabled", "success", "invalid"}:
                    raise ValueError("unknown offline memory classifier mode")

                class MemoryClassifier:
                    def invoke(self, messages, **kwargs):
                        return AIMessage(
                            '{"category_id":null,"new_category":"编程学习"}'
                            if mode == "success" else '{"category_id":"invalid","new_category":null}'
                        )

                services.gateway.client = MemoryClassifier()
                services.gateway.enabled = mode != "disabled"
                return {"mode": mode, "provider": "test-double"}

            @app.post("/api/__offline/memory-worker/{run_id}")
            def execute_memory_classifier(run_id: str):
                run = jobs.get_run(run_id)
                if run["kind"] != "memory_classification":
                    raise ValueError("offline memory worker requires a classification run")
                return execute_run(run_id, run["generation"], services=services,
                                   worker_id="offline-memory-classifier")

            @app.post("/api/__offline/assets-ready")
            def assets_ready_offline():
                for _ in range(100):
                    if services.lifecycle.retained.catalog.step(limit=100)["complete"]:
                        return {"ready": True}
                raise ValueError("offline reference catalog did not finish")

            @app.post("/api/__offline/asset-cleanup/{identity}")
            def advance_asset_offline(identity: str):
                # 只改变隔离夹具中的调度时钟，仍执行生产清理作业和文件操作。
                services.lifecycle.assets.get_job(identity)
                for _ in range(100):
                    row = services.lifecycle.assets.get_job(identity)
                    if row["status"] in {"completed", "failed"}:
                        return row
                    services.lifecycle.advance(identity)
                raise ValueError("asset cleanup did not converge")

            @app.post("/api/__offline/asset-seed")
            def seed_asset_offline():
                token = str(uuid4())
                material = sources.import_text("独立材料 " + token, "正文 " + token)
                protected = sources.import_text("有引用材料 " + token, "被引用正文 " + token)
                sources.import_text("引用者 " + token, f"[固定正文](/sources/{protected['id']}?version={protected['version_id']})")
                name = "可删除交付 " + token + ".md"
                (outputs / name).write_text("# 本次交付\n" + token, encoding="utf-8")
                delivery = DeliveryService(sessions, settings.workspace_root).present(["outputs/" + name],
                    run_id=token, root_id=token, call_id="seed")[0]
                assets_ready_offline()
                return {"source": material, "protected": protected, "delivery": delivery}

            @app.post("/api/__offline/material-alias-seed")
            def seed_material_alias_offline():
                chat = jobs.create_conversation("引用正文转换")
                run = jobs.enqueue(chat["id"], "研究输出全文", request_key=str(uuid4()))
                jobs.claim("material-seed", run_id=run["id"], generation=run["generation"])
                body = "完整引用正文\n" + "中间内容保持原样。\n" * 800 + "全文最后一行也能核对。"
                identity = jobs.save_artifact(run["id"], "tool_result", body)
                url = f"/runs/{run['id']}/artifacts/{identity}"
                sources.import_text("引用输出的独立资料", f"[完整正文]({url})")
                jobs.finish(run["id"], "material-seed", outcome={"kind": "prepared"})
                assets_ready_offline()
                return {"conversation_id": chat["id"], "url": url, "length": len(body)}

            @app.post("/api/__offline/lifecycle-seed")
            def seed_lifecycle_offline(temporary: bool = False):
                # 删除场景拥有自己的会话，不能破坏其他浏览器用例共用的种子。
                chat = jobs.create_conversation("生命周期删除夹具")
                seeded = jobs.enqueue(chat["id"], "保留独立知识草稿", request_key=str(uuid4()))
                jobs.claim("lifecycle-seed", run_id=seeded["id"], generation=seeded["generation"])
                independent = sources.import_text("生命周期独立资料", "删除聊天时，独立来源与草稿继续保留。")
                evidence = sources.read_segment(independent["segments"][0]["id"], root_id=seeded["id"])
                saved = knowledge.create_draft(seeded["id"], [{
                    "action": "create", "title": "生命周期保留规则", "category": "Agent 工程", "tags": [],
                    "body": "独立资料不随聊天删除。 " + evidence["citation"],
                }], independently_saved=True)
                jobs.finish(seeded["id"], "lifecycle-seed", outcome={
                    "answer": "草稿已经保存。", "answer_origin": "application_receipt", "draft_ids": [saved["id"]],
                })
                if temporary:
                    attachment = sources.import_text("会话临时附件", "无其他依赖的临时正文", conversation_id=chat["id"])
                    draft = knowledge.create_draft(seeded["id"], [{"action": "create", "title": "本次临时草稿", "body": "尚未独立保存"}])
                    chat = {**chat, "temporary_source_id": attachment["id"], "temporary_draft_id": draft["id"]}
                for _ in range(100):
                    if services.lifecycle.retained.catalog.step(limit=100)["complete"]:
                        break
                else:
                    raise ValueError("offline reference catalog did not finish")
                return chat

            @app.post("/api/__offline/task-details-seed")
            def seed_task_details_offline():
                from langchain_core.messages import AIMessage

                chat = jobs.create_conversation("任务摘要与按需诊断")
                seeded = jobs.enqueue(chat["id"], "整理一份可独立查看的笔记", request_key=str(uuid4()))
                jobs.claim("details-seed", run_id=seeded["id"], generation=seeded["generation"])
                path = "outputs/任务结果-" + seeded["id"] + ".md"
                (settings.workspace_root / path).write_text("# 已交付正文\n\n诊断过期后仍然可读。", encoding="utf-8")
                delivery = DeliveryService(sessions, settings.workspace_root).present(
                    [path], run_id=seeded["id"], root_id=seeded["id"], call_id="details-file")[0]
                saved = knowledge.create_draft(seeded["id"], [{"action": "create", "title": "独立保留的笔记草稿", "body": "用户可以继续编辑。"}], independently_saved=True)
                jobs.append_event(seeded["id"], "tool_started", {"name": "read_source", "tool_call_id": "details-read"})
                jobs.append_event(seeded["id"], "tool_finished", {"name": "read_source", "tool_call_id": "details-read", "status": "success", "content": "只在诊断显示的工具原文"})
                reservation = services.gateway.ledger.reserve(seeded["id"], seeded["id"], "agent", [{"role": "user", "content": "只在诊断显示的完整请求"}])
                services.gateway.ledger.finish(reservation, response=AIMessage("固定诊断夹具响应").model_dump(mode="json"))
                jobs.finish(seeded["id"], "details-seed", outcome={"answer": "笔记及草稿已经保存。", "answer_origin": "application_receipt", "draft_ids": [saved["id"]]})
                return {"run_id": seeded["id"], "conversation_id": chat["id"], "delivery": delivery}

            @app.post("/api/__offline/task-details/{run_id}/expire")
            def expire_task_details_offline(run_id: str):
                from datetime import timedelta

                from knowledgeops_agent.persistence.models import utcnow

                # 仅隔离验收推进到期时间，实际删除仍执行正式清理器和真实PG保存器。
                with sessions.begin() as session:
                    row = session.get(Run, run_id)
                    if row is None or row.status != "completed":
                        raise ValueError("fixture must be a completed task")
                    row.details_expires_at = utcnow() - timedelta(days=1)
                for _ in range(100):
                    if services.lifecycle.retained.catalog.step(limit=100)["complete"]:
                        break
                job = services.lifecycle.details.admit(run_id)
                for _ in range(300):
                    result = services.lifecycle.jobs.get(job["id"])
                    if result["status"] in {"completed", "failed"}:
                        return {"status": result["status"], "phase": result["phase"], "error": result["last_error"]}
                    services.lifecycle.advance(job["id"])
                raise ValueError("offline detail cleanup did not converge")

            @app.post("/api/__offline/lifecycle/{conversation_id}")
            def advance_lifecycle_offline(conversation_id: str):
                # 仅在隔离浏览器夹具里替代后台时钟；执行正式清理器及真实PG保存器。
                from sqlalchemy import select

                from knowledgeops_agent.persistence.models import CleanupJob

                with sessions() as session:
                    job_id = session.scalar(select(CleanupJob.id).where(
                        CleanupJob.target_kind == "conversation", CleanupJob.target_id == conversation_id))
                if job_id is None:
                    raise ValueError("accept deletion through the normal API first")
                for _ in range(300):
                    result = services.lifecycle.jobs.get(job_id)
                    if result["status"] in {"completed", "failed"}:
                        return {"status": result["status"], "phase": result["phase"], "error": result["last_error"]}
                    services.lifecycle.advance(job_id)
                raise ValueError("offline cleanup did not converge")

            @app.post("/api/__offline/mcp-input/{run_id}")
            def execute_mcp_input_offline(run_id: str):
                from test_mcp_input_continuation import configure

                run = jobs.get_run(run_id)
                if run_id not in input_providers:
                    temporary = Path(storage) / "mcp-input" / run_id
                    temporary.mkdir(parents=True)
                    input_providers[run_id] = configure(services, temporary / "protocol.log", url_mode=True)
                services.gateway.client, services.gateway.enabled = input_providers[run_id], True
                try:
                    return execute_run(run_id, run["generation"], services=services,
                                       checkpointer=offline_saver, worker_id="browser-mcp-input")
                finally:
                    services.gateway.enabled = False

            @app.post("/api/__offline/mcp-review/{run_id}")
            def execute_mcp_review_offline(run_id: str):
                from fixtures.execution_provider import MCPReviewProvider

                services.gateway.client, services.gateway.enabled = MCPReviewProvider(), True
                try:
                    run = jobs.get_run(run_id)
                    return execute_run(run_id, run["generation"], services=services,
                                       checkpointer=offline_saver, worker_id="browser-mcp-review")
                finally:
                    services.gateway.enabled = False

            @app.post("/api/__offline/async/{run_id}")
            def execute_async_offline(run_id: str):
                # 仅测试应用暴露：人工逐步投递，生产图/服务保存等待和结果通知。
                nonlocal async_provider
                from test_async_run_graph import ConcurrentProvider

                from knowledgeops_agent.harness.subagents.async_tools import ASYNC_TOOLS
                from knowledgeops_agent.harness.subagents.registry import SubagentRegistry
                from knowledgeops_agent.harness.tools.policy import ToolPolicyService

                if async_provider is None:
                    SubagentRegistry(services.sessions).create(name="offline-reader", display_name="测试读者",
                        description="独立核对边界", system_prompt="只报告本子任务结果。", tools=[], skills=[], enabled=True)
                    policy = ToolPolicyService(services.sessions)
                    policy.update(expected_revision=policy.get()["revision"],
                                  enabled={f"builtin:{tool.name}": True for tool in ASYNC_TOOLS})
                    async_provider = ConcurrentProvider(services, mode="implicit")
                services.gateway.client, services.gateway.enabled = async_provider, True
                try:
                    run = jobs.get_run(run_id)
                    return execute_run(run_id, run["generation"], services=services,
                                       checkpointer=offline_saver, worker_id="browser-async")
                finally:
                    services.gateway.enabled = False

            @app.post("/api/__offline/file-approval/{run_id}")
            def execute_file_approval_offline(run_id: str):
                from fixtures.reliability_provider import FileApprovalProvider
                from test_workspace_runtime import enable

                enable(services.sessions)
                services.gateway.client, services.gateway.enabled = FileApprovalProvider(), True
                try:
                    run = jobs.get_run(run_id)
                    return execute_run(run_id, run["generation"], services=services,
                                       checkpointer=offline_saver, worker_id="browser-file-approval")
                finally:
                    services.gateway.enabled = False

            @app.post("/api/__offline/file/{run_id}")
            def execute_file_offline(run_id: str):
                from unittest.mock import patch

                from fixtures.reliability_provider import FileProvider
                from sqlalchemy import select

                from knowledgeops_agent.persistence.files import FileStore
                from knowledgeops_agent.persistence.models import FileOperation

                services.gateway.client, services.gateway.enabled = FileProvider(), True
                try:
                    with services.sessions() as session:
                        receipt = session.scalar(select(FileOperation.id).where(FileOperation.root_id == run_id))
                    if receipt is None:
                        from knowledgeops_agent.harness.permissions.settings import (
                            ExecutionSettings,
                        )
                        from knowledgeops_agent.harness.tools.catalog import tool_options
                        from knowledgeops_agent.harness.tools.policy import ToolPolicyService

                        policy = ToolPolicyService(services.sessions)
                        policy.update(expected_revision=policy.get()["revision"],
                                      enabled={item["id"]: True for item in tool_options() if item["condition"] == "workspace"})
                        settings = ExecutionSettings(services.sessions)
                        conversation_id = jobs.get_run(run_id)["conversation_id"]
                        settings.update(conversation_id, expected_revision=settings.get(conversation_id)["revision"],
                                        permission_mode="auto_review")
                        target = services.settings.workspace_root / "work/review.txt"
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text("old")
                        original = FileStore.replace

                        def fail(store, key, data):
                            if key == "work/review.txt":
                                raise ConnectionError("offline file confirmation failure")
                            return original(store, key, data)

                        run = jobs.get_run(run_id)
                        with patch.object(FileStore, "replace", fail):
                            failed = execute_run(run_id, run["generation"], services=services,
                                                 checkpointer=offline_saver, worker_id="browser-file")
                        if failed["status"] != "failed":
                            return failed
                        jobs.resume(run_id)
                    run = jobs.get_run(run_id)
                    return execute_run(run_id, run["generation"], services=services,
                                       checkpointer=offline_saver, worker_id="browser-file")
                finally:
                    services.gateway.enabled = False

            @app.post("/api/__offline/question/{run_id}")
            def execute_question_offline(run_id: str):
                from fixtures.reliability_provider import QuestionsProvider

                services.gateway.client, services.gateway.enabled = QuestionsProvider(), True
                try:
                    run = jobs.get_run(run_id)
                    return execute_run(run_id, run["generation"], services=services,
                                       checkpointer=offline_saver, worker_id="browser-question")
                finally:
                    services.gateway.enabled = False

            @app.post("/api/__offline/execute/{run_id}")
            def execute_offline(run_id: str):
                # 只有测试API拥有此入口。用生产worker/图执行一个指定运行，不访问真实供应商。
                from fixtures.subagent_dispatch import execute_family

                services.gateway.client, services.gateway.enabled = offline_provider, True
                try:
                    run = jobs.get_run(run_id)
                    return execute_family(
                        run_id,
                        run["generation"],
                        services=services,
                        checkpointer=offline_saver,
                        worker_id="browser-offline",
                    )
                finally:
                    services.gateway.enabled = False

            @app.post("/api/__offline/execution/{run_id}")
            def execute_command_offline(run_id: str, lose_receipt: bool = False):
                from fixtures.execution_provider import ExecutionProvider

                from knowledgeops_agent.harness.permissions.receipts import CommandJournal
                from knowledgeops_agent.harness.tools.catalog import tool_options
                from knowledgeops_agent.harness.tools.policy import ToolPolicyService

                policy = ToolPolicyService(sessions)
                policy.update(
                    expected_revision=policy.get()["revision"],
                    enabled={
                        item["id"]: True
                        for item in tool_options()
                        if item["condition"] == "workspace"
                    },
                )
                services.gateway.client, services.gateway.enabled = ExecutionProvider(), True
                original = CommandJournal.finish
                if lose_receipt:

                    def fail_receipt(*args):
                        raise RuntimeError("offline fault after side effect")

                    CommandJournal.finish = fail_receipt
                try:
                    current = jobs.get_run(run_id)
                    return execute_run(
                        run_id,
                        current["generation"],
                        services=services,
                        checkpointer=offline_saver,
                        worker_id="browser-execution",
                    )
                finally:
                    CommandJournal.finish = original
                    services.gateway.enabled = False

            uvicorn.run(
                app,
                host="127.0.0.1",
                port=8001,
                log_level="warning",
            )
    finally:
        if jobs is not None:
            jobs.close_mcp_sessions()
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


if __name__ == "__main__":
    main()
