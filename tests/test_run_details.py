"""摘要不读取完整诊断，过期与删除继续遵守原资产/访问合同。"""

import json

from test_api_worker import client as _client
from test_api_worker import enqueue
from test_api_worker import services as _services

from knowledgeops_agent.harness.runtime.resources import ResourceService
from knowledgeops_agent.persistence.models import Conversation, Run, new_id
from knowledgeops_agent.sources.deliveries import DeliveryService

client, services = _client, _services


def test_summary_projects_tool_state_without_loading_diagnostic_payloads(client, services, monkeypatch):
    _, run, _ = enqueue(client)
    jobs = services.jobs
    jobs.claim("summary", run_id=run["id"], generation=run["generation"])
    secret_detail = "完整内部工具结果" * 10000
    for name, identity, status in [("read_source", "read", "success"), ("execute", "shell", "error")]:
        jobs.append_event(run["id"], "tool_started", {"tool_call_id": identity, "name": name, "args": secret_detail})
        jobs.append_event(run["id"], "tool_finished", {"tool_call_id": identity, "name": name, "status": status, "content": secret_detail})

    def forbidden(*args, **kwargs):
        raise AssertionError("摘要不得读取完整诊断")

    monkeypatch.setattr(services.gateway.ledger, "records", forbidden)
    monkeypatch.setattr(jobs, "artifacts", forbidden)
    monkeypatch.setattr(jobs, "events", forbidden)
    monkeypatch.setattr(ResourceService, "read", forbidden)
    result = client.get(f"/api/runs/{run['id']}/summary")
    assert result.status_code == 200
    body = result.json()
    assert [(row["id"], row["status"]) for row in body["tools"]] == [("shell", "error"), ("read", "returned")]
    assert len(json.dumps(body, ensure_ascii=False)) < 2000
    assert "完整内部工具结果" not in result.text
    assert all(row["display_name"] != row["name"] for row in body["tools"])


def test_expired_summary_keeps_independent_files_and_live_drafts_but_no_internal_output(client, services, tmp_path):
    chat, run, _ = enqueue(client)
    services.settings.workspace_root = tmp_path
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs/result.md").write_text("# 固定交付", encoding="utf-8")
    delivered = DeliveryService(services.sessions, tmp_path).present(
        ["outputs/result.md"], run_id=run["id"], root_id=run["id"], call_id="deliver")[0]
    draft = services.knowledge.create_draft(run["id"], [{"action": "create", "title": "保留草稿", "body": "独立保存"}], independently_saved=True)
    with services.sessions.begin() as session:
        row = session.get(Run, run["id"])
        row.lifecycle_state, row.status = "expired", "completed"
        row.outcome = {"answer": "完成", "draft_ids": [draft["id"], new_id()]}
    response = client.get(f"/api/runs/{run['id']}/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["details_available"] is False and body["tools"] == []
    assert [item["id"] for item in body["drafts"]] == [draft["id"]]
    assert body["deliveries"][0]["id"] == delivered["id"]
    assert DeliveryService(services.sessions, tmp_path).read(delivered["id"])[1] == "# 固定交付".encode()
    with services.sessions.begin() as session:
        session.get(Conversation, chat["id"]).deletion_state = "deleting"
    assert client.get(f"/api/runs/{run['id']}/summary").status_code == 404
    assert DeliveryService(services.sessions, tmp_path).read(delivered["id"])[1] == "# 固定交付".encode()


def test_tool_activity_is_bounded_and_does_not_claim_unknown_attempts_completed(client, services):
    _, run, _ = enqueue(client)
    for index in range(54):
        services.jobs.append_event(run["id"], "tool_started", {"tool_call_id": str(index), "name": "read_source"})
    body = client.get(f"/api/runs/{run['id']}/summary").json()
    assert len(body["tools"]) == 50 and body["tools_truncated"]
    assert {item["status"] for item in body["tools"]} == {"unsettled"}
    assert body["tools"][0]["id"] == "53"
