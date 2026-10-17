"""主模型节点跨检查点提交窗口恢复：真实图/PG/账本，供应商为确定性替身。"""

from contextlib import contextmanager

import pytest
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from sqlalchemy import select
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.harness.models.chat import LedgerChatModel
from knowledgeops_agent.persistence.models import ModelStep

runtime_services = _runtime_services


class CountingProvider:
    def __init__(self):
        self.calls = 0

    def bind_tools(self, tools):
        return self

    def stream(self, messages, **kwargs):
        self.calls += 1
        yield AIMessageChunk(content="原请求已经完成。")


@contextmanager
def checkpoint_scope(context, persisted, memory):
    if not persisted:
        yield memory
        return
    engine = context.gateway.ledger.engine
    args, kwargs = engine.dialect.create_connect_args(engine.url)
    with engine.connect() as connection:
        schema = connection.exec_driver_sql("SELECT current_schema()").scalar_one()
    with Connection.connect(
        *args, **kwargs, options=f"-csearch_path={schema}", autocommit=True,
        prepare_threshold=0, row_factory=dict_row,
    ) as connection:
        saver = PostgresSaver(connection)
        saver.setup()
        yield saver


@pytest.mark.parametrize("persisted", [False, True])
def test_saved_response_replays_original_request_after_model_node_crash(runtime_services, monkeypatch, persisted):
    module, context, run = setup_run(runtime_services, budgets={"model_requests": 3})
    provider = CountingProvider()
    context.gateway.client = provider
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": run["checkpoint_id"]}}
    original = LedgerChatModel._generate
    crashed = []

    def generate(model, *args, **kwargs):
        result = original(model, *args, **kwargs)
        if not crashed:
            crashed.append(True)
            raise RuntimeError("response saved, model node checkpoint not committed")
        return result

    monkeypatch.setattr(LedgerChatModel, "_generate", generate)
    with checkpoint_scope(context, persisted, saver) as store:
        graph = module.build_agent(context, checkpointer=store)
        with pytest.raises(RuntimeError, match="checkpoint not committed"):
            graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert provider.calls == 1
    # 剩余预算变化会改变动态背景，甚至触发工具收束；恢复仍须取原请求/响应。
    with checkpoint_scope(context, persisted, saver) as store:
        reopened = module.build_agent(context, checkpointer=store)
        state = reopened.invoke(None, config, context=context)
    assert state["messages"][-1].content == "原请求已经完成。"
    assert provider.calls == 1
    assert len(context.gateway.ledger.records(root_id=run["id"])) == 1
    with context.jobs.sessions() as session:
        assert len(list(session.scalars(select(ModelStep)))) == 1


def test_uncertain_main_model_request_is_not_reissued(runtime_services):
    from knowledgeops_agent.harness.models.gateway import ModelCallUncertain

    module, context, run = setup_run(runtime_services)
    provider = CountingProvider()

    class LostProvider(CountingProvider):
        def stream(self, messages, **kwargs):
            raise SystemExit("process lost after provider request")

    context.gateway.client = LostProvider()
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": run["checkpoint_id"]}}
    graph = module.build_agent(context, checkpointer=saver)
    with pytest.raises(SystemExit):
        graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    context.gateway.client = provider
    context.jobs.recover_on_startup()
    resumed = context.jobs.resume(run["id"])
    context.jobs.claim(context.worker_id, run_id=run["id"], generation=resumed["generation"])
    with pytest.raises(ModelCallUncertain):
        module.build_agent(context, checkpointer=saver).invoke(None, config, context=context)
    assert provider.calls == 0


def test_failed_attempt_requires_explicit_resume_before_new_request(runtime_services):
    from knowledgeops_agent.harness.models.gateway import ModelCallFailed

    module, context, run = setup_run(runtime_services)
    provider = CountingProvider()

    class FailedProvider(CountingProvider):
        def stream(self, messages, **kwargs):
            raise ConnectionError("controlled failed attempt")

    context.gateway.client = FailedProvider()
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": run["checkpoint_id"]}}
    graph = module.build_agent(context, checkpointer=saver)
    with pytest.raises(ModelCallFailed):
        graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    context.gateway.client = provider
    with pytest.raises(ModelCallFailed):
        graph.invoke(None, config, context=context)
    assert provider.calls == 0
    context.jobs.recover_on_startup()
    resumed = context.jobs.resume(run["id"])
    context.jobs.claim(context.worker_id, run_id=run["id"], generation=resumed["generation"])
    result = module.build_agent(context, checkpointer=saver).invoke(None, config, context=context)
    assert result["messages"][-1].content == "原请求已经完成。"
    assert provider.calls == 1
    assert len(context.gateway.ledger.records(root_id=run["id"])) == 2
