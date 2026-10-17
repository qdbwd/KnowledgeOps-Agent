"""离线验收用的真实 Celery 入口：仅替换供应商，沿用生产 main/领取/检查点。"""

import os
import re
import sys
from functools import partial
from pathlib import Path

from celery.signals import worker_ready
from fixtures.execution_provider import ExecutionProvider
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from test_async_run_graph import ConcurrentProvider

from knowledgeops_agent.app.runs import worker
from knowledgeops_agent.app.runs.queue import create_queue
from knowledgeops_agent.app.runs.service import RunService
from knowledgeops_agent.app.services import Services
from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.harness.models.gateway import ModelGateway, ModelLedger
from knowledgeops_agent.harness.models.steps import _step_scope
from knowledgeops_agent.knowledge.service import KnowledgeService
from knowledgeops_agent.sources.service import SourceService


def main():
    schema, scope, directory, queue_name = sys.argv[1:]
    url = make_url(os.environ["TEST_DATABASE_URL"])
    broker = os.environ["TEST_REDIS_URL"]
    if url.database != "knowledgeops_test" or not re.fullmatch(r"test_[0-9a-f]{32}", schema) or not broker.endswith("/15"):
        raise RuntimeError("isolated PostgreSQL and Redis required")
    root = Path(directory)
    engine = create_engine(url.update_query_dict({"options": f"-csearch_path={schema}"}))
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, model_calls_enabled=False, celery_broker_url=broker,
                        workspace_root=root / "workspace", source_storage_root=root / "sources",
                        app_log_level="warning")
    services = Services(settings=settings, engine=engine, sessions=sessions, jobs=RunService(sessions),
                        knowledge=KnowledgeService(sessions), sources=SourceService(sessions, root / "sources"),
                        gateway=ModelGateway(ModelLedger(engine, scope=scope), enabled=True, client=None))

    class RoutedProvider:
        invoke = ExecutionProvider.invoke

        def __init__(self):
            self.providers = {}

        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            row = services.jobs.get_run(_step_scope.get()[0])
            parent = services.jobs.get_run(row["root_id"])
            if parent["id"] not in self.providers:
                self.providers[parent["id"]] = ConcurrentProvider(services, mode="implicit" if "重启" in parent["task"] else "async")
            provider = self.providers[parent["id"]]
            for chunk in provider.stream(messages, **kwargs):
                if row["id"] == row["root_id"] and len(provider.arrived) == 3:
                    services.jobs.append_event(parent["id"], "offline_overlap", {"arrived": sorted(provider.arrived)})
                yield chunk

    services.gateway.client = RoutedProvider()
    worker.get_settings = lambda: settings
    worker.create_services = lambda _: services
    worker.create_queue = partial(create_queue, queue_name=queue_name)
    worker_ready.connect(lambda **kwargs: (root / "ready").touch(), weak=False)
    worker.main()


if __name__ == "__main__":
    main()
