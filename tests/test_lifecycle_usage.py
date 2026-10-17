"""完整轨迹到期后仍能显示最后有效上下文大小，不恢复项目历史用量统计。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from test_api_worker import client as _client
from test_api_worker import services as _services
from test_context_usage import make_run, record

from knowledgeops_agent.harness.context.usage import context_usage
from knowledgeops_agent.harness.models.gateway import requests
from knowledgeops_agent.persistence.models import Run

client = _client
services = _services


def test_usage_survives_detail_removal_and_new_request_replaces_it(client, services):
    conversation, run = make_run(services)
    record(services, run, actual=10000)
    before = context_usage(services.sessions, services.gateway, conversation["id"], only_run_id=run["id"])
    with services.sessions.begin() as session:
        row = session.get(Run, run["id"])
        saved = {**before, "measured_at": before["measured_at"].isoformat()}
        row.details_summary = {"context_usage": saved}
        row.lifecycle_state = "expired"
    services.gateway.ledger.seal_root(run["id"])
    services.gateway.ledger.purge_root_details(run["id"])
    endpoint = f"/api/conversations/{conversation['id']}/context-usage"
    after = client.get(endpoint).json()
    assert after["input_tokens"] == 10000 and after["request_id"] == before["request_id"]
    following = services.jobs.enqueue(conversation["id"], "下一轮", request_key="usage-next")
    record(services, following, actual=20000)
    assert client.get(endpoint).json()["input_tokens"] == 20000


def test_new_root_low_ordinal_is_newer_than_old_root_high_ordinal(client, services):
    conversation, first = make_run(services)
    for _ in range(3):
        record(services, first, actual=90000)
    following = services.jobs.enqueue(conversation["id"], "下一轮", request_key="newer")
    record(services, following, actual=100)
    # 显式时间使断言不依赖机器是否在同一时钟粒度内完成。
    with services.gateway.ledger.engine.begin() as connection:
        connection.execute(requests.update().where(requests.c.root_id == first["id"]).values(created_at=datetime.now(UTC) - timedelta(days=1)))
        assert max(connection.scalars(select(requests.c.ordinal).where(requests.c.root_id == first["id"]))) == 3
    response = client.get(f"/api/conversations/{conversation['id']}/context-usage").json()
    assert response["run_id"] == following["id"] and response["input_tokens"] == 100
