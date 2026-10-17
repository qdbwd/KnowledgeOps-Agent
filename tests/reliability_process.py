"""隔离子进程验收入口，只接受测试库 UUID schema 和明确的确定性供应商。"""

import json
import os
import re
import sys
from pathlib import Path

from fixtures.reliability_provider import QuestionsProvider
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.app.runs.service import RunService
from knowledgeops_agent.app.runs.worker import execute_run
from knowledgeops_agent.app.services import Services
from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.harness.models.gateway import ModelGateway, ModelLedger
from knowledgeops_agent.knowledge.service import KnowledgeService
from knowledgeops_agent.sources.service import SourceService


def main():
    schema, scope, path, run_id, generation, *modes = sys.argv[1:]
    url = make_url(os.environ["TEST_DATABASE_URL"])
    if url.database != "knowledgeops_test" or not re.fullmatch(r"test_[0-9a-f]{32}", schema):
        raise RuntimeError("isolated test schema required")
    engine = create_engine(url.update_query_dict({"options": f"-csearch_path={schema}"}))
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, model_calls_enabled=False,
                        workspace_root=Path(path) / "workspace", source_storage_root=Path(path))
    if modes == ["mcp"]:
        from fixtures.mcp_input_provider import InputProvider

        provider = InputProvider(Path(path) / "protocol.log")
    elif not modes:
        provider = QuestionsProvider()
    elif modes[0] in {"file-before", "file-after", "file-resume"}:
        from fixtures.reliability_provider import FileProvider

        from knowledgeops_agent.persistence.files import FileStore

        provider = FileProvider()
        original = FileStore.replace

        def replace(store, key, data):
            if key != "work/review.txt":
                return original(store, key, data)
            if modes[0] == "file-before":
                os._exit(73)
            original(store, key, data)
            with (Path(path) / "file-writes.log").open("a") as stream:
                stream.write("write\n")
            if modes[0] == "file-after":
                os._exit(73)

        FileStore.replace = replace
    else:
        raise ValueError("unknown isolated provider")
    services = Services(settings=settings, engine=engine, sessions=sessions, jobs=RunService(sessions),
                        knowledge=KnowledgeService(sessions), sources=SourceService(sessions, Path(path)),
                        gateway=ModelGateway(ModelLedger(engine, scope=scope), enabled=True, client=provider))
    try:
        result = execute_run(run_id, int(generation), services=services, worker_id="isolated-process")
        requests = provider.requests if isinstance(provider.requests, int) else len(provider.requests)
        print(json.dumps({"run": result, "model_requests": requests}, ensure_ascii=False))
    finally:
        services.close()


if __name__ == "__main__":
    main()
