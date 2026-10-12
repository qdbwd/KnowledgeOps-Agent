"""网关测试使用独立测试库；真实供应商替身不获得断言，业务账本才是断言对象。"""

from __future__ import annotations

import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from sqlalchemy import create_engine, text


@pytest.fixture
def ledger():
    module = importlib.import_module("knowledgeops_agent.model_gateway")
    # 每例独立命名空间，连接必须指向隔离测试库，绝不使用真实验收账本。
    url = os.environ["TEST_DATABASE_URL"]
    assert url.rsplit("/", 1)[-1] == "knowledgeops_test"
    # 独立schema避免复用此前验收遗留的非空累计上限表结构。
    schema = "model_gateway_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, pool_size=8, connect_args={"options": f"-csearch_path={schema}"})
    try:
        module.prepare_accounting(engine)
        yield module.ModelLedger(engine, scope=f"test-{uuid4().hex}")
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_reservations_survive_restart_and_only_enforce_root_limit(ledger):
    module = importlib.import_module("knowledgeops_agent.model_gateway")
    first = ledger.reserve("root-a", "run-a", "agent", [], root_limit=2)
    ledger.finish(first, response={"content": "ok"}, usage=None)
    reopened = module.ModelLedger(ledger.engine, scope=ledger.scope)
    reopened.reserve("root-a", "run-b", "research", [], root_limit=2)
    with pytest.raises(module.ModelBudgetExceeded, match="root"):
        reopened.reserve("root-a", "run-c", "agent", [], root_limit=2)
    reopened.reserve("root-b", "run-d", "agent", [], root_limit=2)
    reopened.reserve("root-c", "run-e", "judge", [], root_limit=2)
    records = reopened.records()
    assert len(records) == 4
    assert reopened.summary("root-a") == {"root_used": 2, "root_limit": 2}
    assert records[0]["usage"] is None  # 未观测到用量不伪造为零。
    assert records[0]["status"] == "succeeded"


def test_concurrent_children_cannot_exceed_shared_root_budget(ledger):
    module = importlib.import_module("knowledgeops_agent.model_gateway")

    def reserve(index):
        try:
            return ledger.reserve("root", f"child-{index}", "research", [], root_limit=2)
        except module.ModelBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        reservations = list(pool.map(reserve, range(6)))
    assert len([item for item in reservations if item]) == 2
    assert len(ledger.records()) == 2


def test_disabled_model_does_not_reserve_or_contact_provider(ledger):
    module = importlib.import_module("knowledgeops_agent.model_gateway")
    gateway = module.ModelGateway(ledger, enabled=False, client=None)
    with pytest.raises(module.ModelCallsDisabled):
        gateway.invoke([HumanMessage("hello")], root_id="root", run_id="run")
    assert ledger.records() == []


def test_provider_failure_is_charged_and_secret_is_redacted(ledger):
    module = importlib.import_module("knowledgeops_agent.model_gateway")
    secret = "fictional-key-never-log-this"

    class UnavailableProvider:
        def invoke(self, messages, **kwargs):
            raise ConnectionError(f"network failed; authorization={secret}")

    gateway = module.ModelGateway(
        ledger, enabled=True, client=UnavailableProvider(), sensitive_values=[secret]
    )
    with pytest.raises(module.ModelCallFailed) as error:
        gateway.invoke([HumanMessage("hello")], root_id="root", run_id="run")
    assert secret not in str(error.value)
    record = ledger.records()[0]
    assert record["status"] == "failed"
    assert secret not in record["error"]
    assert record["usage"] is None


def test_success_preserves_real_response_and_reported_usage(ledger):
    module = importlib.import_module("knowledgeops_agent.model_gateway")

    class Provider:
        def invoke(self, messages, **kwargs):
            assert messages[-1].content == "hello"
            return AIMessage(
                content="已读取",
                tool_calls=[{"name": "read_source", "args": {"id": "source-a"}, "id": "call-a"}],
                usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            )

    gateway = module.ModelGateway(ledger, enabled=True, client=Provider())
    response = gateway.invoke([HumanMessage("hello")], root_id="root", run_id="run")
    assert response.tool_calls[0]["args"] == {"id": "source-a"}
    record = ledger.records()[0]
    assert record["usage"]["total_tokens"] == 18
    assert record["request"]["messages"][0]["content"] == "hello"
    assert record["response"]["content"] == "已读取"
    assert record["duration_ms"] >= 0


def test_streaming_records_first_content_and_combines_response(ledger):
    module = importlib.import_module("knowledgeops_agent.model_gateway")

    class Provider:
        def stream(self, messages, **kwargs):
            yield AIMessageChunk(content="")
            yield AIMessageChunk(content="知识")
            yield AIMessageChunk(
                content="库",
                usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            )

    gateway = module.ModelGateway(ledger, enabled=True, client=Provider())
    response = gateway.invoke([HumanMessage("hello")], root_id="root", run_id="run", stream=True)
    assert response.content == "知识库"
    record = ledger.records()[0]
    assert 0 <= record["first_token_ms"] <= record["duration_ms"]
    assert record["usage"]["total_tokens"] == 5


def test_canary_uses_its_root_budget_without_a_lifetime_counter(ledger):
    module = importlib.import_module("knowledgeops_agent.model_gateway")
    canary = module.ModelLedger(ledger.engine, scope=f"canary-{uuid4().hex}")
    for index in range(10):
        canary.reserve("canary", f"attempt-{index}", "canary", [], root_limit=20)
    canary.reserve("canary", "attempt-11", "canary", [], root_limit=20)
    canary.reserve("normal", "normal-run", "agent", [], root_limit=2)
    assert len(canary.records()) == 12
    from sqlalchemy import select

    with canary.engine.connect() as connection:
        row = (
            connection.execute(select(module.quotas).where(module.quotas.c.scope == canary.scope))
            .mappings()
            .one()
        )
    assert row["used"] == row["canary_used"] == 0
    assert row["total_limit"] is None


def test_explicit_root_extension_preserves_spent_quota(ledger):
    module = importlib.import_module("knowledgeops_agent.model_gateway")
    ledger.reserve("root", "run", "agent", [], root_limit=1)
    # 仅换一个更大的调用参数不会隐式扩额；必须经过可信恢复入口。
    with pytest.raises(module.ModelBudgetExceeded):
        ledger.reserve("root", "run", "agent", [], root_limit=2)
    ledger.set_root_limit("root", 2)
    ledger.reserve("root", "run", "agent", [], root_limit=2)
    usage = ledger.summary("root")
    assert usage["root_used"] == 2
    assert "project_used" not in usage
    assert usage["root_limit"] == 2
    with pytest.raises(ValueError):
        ledger.set_root_limit("root", 1)


def test_request_evidence_contains_tools_and_stream_updates(ledger):
    module = importlib.import_module("knowledgeops_agent.model_gateway")
    from langchain_core.tools import tool

    @tool
    def lookup(query: str) -> str:
        """查询已保存知识。"""
        return query

    class Provider:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            yield AIMessageChunk(content="可见片段")

    fragments = []
    gateway = module.ModelGateway(ledger, enabled=True, client=Provider())
    gateway.invoke(
        [HumanMessage("查知识")],
        root_id="root",
        run_id="run",
        tools=[lookup],
        stream=True,
        on_chunk=fragments.append,
    )
    record = ledger.records()[0]
    assert record["request"]["tools"][0]["function"]["name"] == "lookup"
    assert fragments == ["可见片段"]
