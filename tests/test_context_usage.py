"""上下文占用来自当前会话主Agent的单次输入，不是累计消费或子任务用量。"""

from test_api_worker import client as _client
from test_api_worker import services as _services

client = _client
services = _services


def make_run(services):
    conversation = services.jobs.create_conversation()
    run = services.jobs.enqueue(conversation["id"], "任务", request_key=conversation["id"])
    return conversation, run


def record(services, run, *, purpose="agent", actual=None, legacy=False, run_id=None):
    capacity = services.gateway.capacity.measure({"messages": [{"role": "user", "content": "hi"}]})
    if legacy:
        capacity["method"] = "utf8_bytes_plus_framing_v1"
    reservation = services.gateway.ledger.reserve(
        run["id"], run_id or run["id"], purpose, {"capacity": capacity}
    )
    services.gateway.ledger.finish(
        reservation,
        response={"content": "ok"},
        usage={"input_tokens": actual, "output_tokens": 10, "total_tokens": actual + 10}
        if actual is not None
        else None,
    )


def test_usage_unknown_until_first_model_input_and_unknown_conversation_is_404(client, services):
    conversation, _ = make_run(services)
    response = client.get(f"/api/conversations/{conversation['id']}/context-usage")
    assert response.status_code == 200
    assert response.json()["input_tokens"] is None
    assert response.json()["percentage"] is None
    assert response.json()["model_window"] == 1048576
    assert client.get("/api/conversations/missing/context-usage").status_code == 404


def test_usage_uses_latest_root_agent_input_excluding_other_work(client, services):
    conversation, run = make_run(services)
    record(services, run, actual=131072)
    record(services, run, purpose="context_summary", actual=100)
    record(services, run, purpose="agent", actual=200, run_id="child")
    _, other = make_run(services)
    record(services, other, actual=900000)
    before = len(services.gateway.ledger.records())
    result = client.get(f"/api/conversations/{conversation['id']}/context-usage").json()
    assert result["input_tokens"] == 131072
    assert result["percentage"] == 50
    assert result["method"] == "provider_usage"
    assert result["basis"] == "last_model_input"
    assert result["run_id"] == run["id"]
    assert result["output_reserve"] == 8192
    assert result["working_window"] == 262144
    assert result["model_window"] == 1048576
    assert result["summary_trigger_tokens"] == 235929
    assert len(services.gateway.ledger.records()) == before


def test_usage_shows_token_estimate_but_never_labels_old_bytes_as_tokens(client, services):
    conversation, run = make_run(services)
    record(services, run)
    endpoint = f"/api/conversations/{conversation['id']}/context-usage"
    estimate = client.get(endpoint).json()
    assert estimate["method"] == "deepseek_v41_tokenizer_v1"
    assert estimate["input_tokens"] > 0 and estimate["estimated"] is True
    record(services, run, legacy=True)
    legacy = client.get(endpoint).json()
    assert legacy["input_tokens"] is None
    assert legacy["percentage"] is None
    assert legacy["unavailable_reason"] == "legacy_byte_estimate"


def test_usage_after_completed_turn_prefers_matching_compaction_estimate(client, services):
    conversation, run = make_run(services)
    record(services, run, actual=240000)
    latest = services.gateway.ledger.latest_agent_request(run["id"])
    after = services.gateway.capacity.measure({"messages": [{"role": "user", "content": "摘要"}]})
    services.jobs.append_event(
        run["id"],
        "context_compacted",
        {
            "trigger": "after_turn",
            "based_on_request_id": latest["id"],
            "after": after,
        },
    )
    endpoint = f"/api/conversations/{conversation['id']}/context-usage"
    assert client.get(endpoint).json()["basis"] == "last_model_input"
    services.jobs.claim("offline-worker", run_id=run["id"], generation=1)
    services.jobs.finish(run["id"], "offline-worker", outcome={"answer": "已完成"})
    current = client.get(endpoint).json()
    assert current["basis"] == "after_turn_compaction_estimate"
    assert current["estimated"] is True
    assert current["input_tokens"] == after["input_tokens"]
    record(services, run, actual=1000)
    assert client.get(endpoint).json()["basis"] == "last_model_input"
