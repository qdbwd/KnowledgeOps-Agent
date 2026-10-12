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

from knowledgeops_agent.api import create_app
from knowledgeops_agent.config import Settings
from knowledgeops_agent.db import Run, RunEvent
from knowledgeops_agent.evaluation import ReportService
from knowledgeops_agent.jobs import RunService
from knowledgeops_agent.knowledge import KnowledgeService
from knowledgeops_agent.model_gateway import ModelGateway, ModelLedger, prepare_accounting
from knowledgeops_agent.services import Services
from knowledgeops_agent.sources import SourceService


def main():
    admin = create_engine(os.environ["TEST_DATABASE_URL"])
    if admin.url.database != "knowledgeops_test":
        raise RuntimeError("browser tests require the isolated knowledgeops_test database")
    schema = "browser_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(admin.url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as connection:
            config = Config("/workspace/alembic.ini")
            config.set_main_option("script_location", "/workspace/migrations")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        prepare_accounting(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        with TemporaryDirectory(prefix="knowledgeops-browser-") as storage:
            from knowledgeops_agent.file_store import FileStore

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

            from knowledgeops_agent.worker import execute_run

            offline_provider, offline_saver = LearningProvider(), InMemorySaver()

            @app.post("/api/__offline/execute/{run_id}")
            def execute_offline(run_id: str):
                # 只有测试API拥有此入口。用生产worker/图执行一个指定运行，不访问真实供应商。
                services.gateway.client, services.gateway.enabled = offline_provider, True
                try:
                    run = jobs.get_run(run_id)
                    return execute_run(
                        run_id,
                        run["generation"],
                        services=services,
                        checkpointer=offline_saver,
                        worker_id="browser-offline",
                    )
                finally:
                    services.gateway.enabled = False

            uvicorn.run(
                app,
                host="127.0.0.1",
                port=8001,
                log_level="warning",
            )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


if __name__ == "__main__":
    main()
