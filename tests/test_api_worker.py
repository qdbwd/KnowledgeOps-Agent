"""HTTP 合同和后台运行结果；真实数据库，模型关闭或使用明确的协议替身。"""

import importlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from knowledgeops_agent.app.runs.service import RunService
from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.harness.models.gateway import ModelGateway, ModelLedger, prepare_accounting
from knowledgeops_agent.knowledge.service import KnowledgeService
from knowledgeops_agent.sources.service import SourceService


@pytest.fixture
def services(business_sessions, tmp_path):
    module = importlib.import_module("knowledgeops_agent.app.services")
    engine = business_sessions.kw["bind"]
    prepare_accounting(engine)
    ledger = ModelLedger(engine, scope="api-offline-" + uuid4().hex)
    return module.Services(
        settings=Settings(
            _env_file=None,
            model_calls_enabled=False,
            workspace_root=None,
            source_storage_root=tmp_path,
        ),
        engine=engine,
        sessions=business_sessions,
        jobs=RunService(business_sessions),
        knowledge=KnowledgeService(business_sessions),
        sources=SourceService(business_sessions, tmp_path),
        gateway=ModelGateway(ledger, enabled=False, client=None),
    )


@pytest.fixture
def client(services):
    api = importlib.import_module("knowledgeops_agent.app.application")
    with TestClient(api.create_app(services=services, publish_jobs=False)) as client:
        yield client


def enqueue(client, *, mode="preview"):
    conversation = client.post("/api/conversations", json={"title": "验收"}).json()
    body = {"content": "整理材料", "request_key": str(uuid4()), "publication_mode": mode}
    response = client.post(f"/api/conversations/{conversation['id']}/messages", json=body)
    assert response.status_code == 202
    return conversation, response.json(), body


def test_api_startup_refuses_existing_backup_window(services):
    from knowledgeops_agent.app.application import create_app
    from knowledgeops_agent.persistence.maintenance import MaintenanceBusy, maintenance_lease

    with maintenance_lease(services.engine, exclusive=True):
        with pytest.raises(MaintenanceBusy), TestClient(create_app(services=services, publish_jobs=False)):
            pytest.fail('API startup passed the maintenance barrier')


def test_api_lease_loss_refuses_new_requests(client, services):
    from sqlalchemy import text

    with services.engine.begin() as connection:
        connection.execute(text('SELECT pg_terminate_backend(:pid)'), {'pid': services.maintenance.backend_pid})
    response = client.post('/api/conversations', json={'title': '不得创建'})
    assert response.status_code == 503
    assert response.json()['code'] == 'maintenance_lost'
    assert services.jobs.conversations() == []


def test_development_reports_are_not_exposed_as_product_endpoints(client):
    response = client.get("/api/evaluations")
    assert response.status_code == 404
    assert client.post("/api/evaluations", json={}).status_code == 404
    assert client.get("/api/evaluations/missing").status_code == 404


def test_local_api_rejects_unknown_host_and_cross_origin_browser_requests(client):
    assert client.get("/api/conversations", headers={"Host": "attacker.invalid"}).status_code == 400
    assert (
        client.get("/api/conversations", headers={"Origin": "http://attacker.invalid"}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/conversations", json={}, headers={"Origin": "https://testserver"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/conversations", json={}, headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/conversations", json={}, headers={"Origin": "http://testserver"}
        ).status_code
        == 201
    )


def test_body_only_http_edit_preserves_metadata(client, services):
    from test_knowledge_service import metadata_article

    original = metadata_article(services.knowledge, services.sources)
    response = client.post(
        f"/api/knowledge/{original['document_id']}/edits",
        json={
            "request_key": "metadata-edit",
            "base_version_id": original["version_id"],
            "title": original["title"],
            "body": original["body"] + "补充说明。",
        },
    )
    assert response.status_code == 200
    changed = services.knowledge.get_document(original["document_id"])
    for name in ("summary", "category", "tags", "relations"):
        assert changed[name] == original[name]


def test_message_is_durable_and_duplicate_http_request_has_one_run(client, services):
    conversation, run, body = enqueue(client)
    replay = client.post(f"/api/conversations/{conversation['id']}/messages", json=body)
    assert replay.json()["id"] == run["id"]
    assert len(services.jobs.messages(conversation["id"])) == 1
    assert client.get(f"/api/runs/{run['id']}").json()["status"] == "queued"
    assert services.gateway.ledger.records() == []
    body["content"] = "不能通过同一键创建不同请求"
    assert (
        client.post(f"/api/conversations/{conversation['id']}/messages", json=body).status_code
        == 409
    )


def test_long_run_records_are_pageable_and_export_keeps_the_final_event(client, services):
    """长流式输出不能在第1000条静默截断，导出需覆盖其声明的事件快照。"""
    from knowledgeops_agent.persistence.models import Run, RunEvent

    _, run, _ = enqueue(client)
    services.jobs.claim("record-test", run_id=run["id"], generation=run["generation"])
    with services.sessions.begin() as session:
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
    services.jobs.finish(run["id"], "record-test", outcome={"answer": "最终答案"})
    seen, after = [], 0
    for _ in range(6):
        page = client.get(f"/api/runs/{run['id']}/records?after={after}").json()
        seen.extend(page["events"])
        after = page["event_page"]["next_after"]
        if after is None:
            break
    assert len(seen) == page["event_page"]["total"] == 1004
    assert [event["sequence"] for event in seen] == list(range(1, 1005))
    assert seen[-1]["kind"] == "finished"
    exported = client.get(f"/api/runs/{run['id']}/export").json()
    assert exported["events"] == seen
    assert exported["event_page"]["next_after"] is None


def test_web_import_is_a_durable_background_job_without_model_calls(client, services, monkeypatch):
    web = importlib.import_module("knowledgeops_agent.sources.web")
    worker = importlib.import_module("knowledgeops_agent.app.runs.worker")
    text = "后台导入网页会保存原件和解析后的定位片段。只保存材料，不会自动创建知识文章。" * 4
    monkeypatch.setattr(
        web.PublicWebReader,
        "fetch",
        lambda _self, url: web.WebSnapshot(url, text.encode(), "text/plain"),
    )
    body = {"url": "https://example.org/material", "request_key": str(uuid4())}
    response = client.post("/api/sources/url", json=body)
    assert response.status_code == 202
    run = response.json()
    assert run["kind"] == "material" and run["status"] == "queued"
    assert services.sources.list_sources() == []
    assert client.post("/api/sources/url", json=body).json()["id"] == run["id"]
    result = worker.execute_run(
        run["id"], run["generation"], services=services, worker_id="material-worker"
    )
    assert result["status"] == "completed"
    source = client.get(f"/api/sources/{result['outcome']['source_id']}").json()
    assert source["origin"] == body["url"] and source["status"] == "ready"
    assert services.gateway.ledger.records() == []
    assert services.knowledge.list_documents() == []
    assert (
        worker.execute_run(
            run["id"], run["generation"], services=services, worker_id="material-worker"
        )
        is None
    )


def test_pdf_upload_queues_parsing_and_persists_page_progress(client, services):
    import io

    from reportlab.pdfgen.canvas import Canvas

    content = io.BytesIO()
    pdf = Canvas(content)
    pdf.drawString(
        60, 740, "Persistent page evidence. The budget is 80 tool calls and 40 model calls."
    )
    pdf.save()
    response = client.post(
        "/api/sources/upload", files={"file": ("sample.pdf", content.getvalue(), "application/pdf")}
    )
    assert response.status_code == 202
    run = services.jobs.get_run(response.json()["job_id"])
    worker = importlib.import_module("knowledgeops_agent.app.runs.worker")
    result = worker.execute_run(
        run["id"], run["generation"], services=services, worker_id="pdf-worker"
    )
    assert result["status"] == "completed"
    source = client.get(f"/api/sources/{response.json()['source_id']}").json()
    assert source["segments"][0]["page"] == 1
    assert any(event["kind"] == "page_parsed" for event in services.jobs.events(run["id"]))
    assert services.gateway.ledger.records() == []


def test_user_can_review_publish_and_reopen_fixed_source(client, services):
    _, run, _ = enqueue(client)
    source = client.post(
        "/api/sources/text", json={"title": "来源", "content": "固定证据。"}
    ).json()
    evidence = services.sources.read_segment(source["segments"][0]["id"], root_id=run["id"])
    draft = services.knowledge.create_draft(
        run["id"],
        [
            {
                "action": "create",
                "title": "知识文章",
                "body": "固定证据。" + evidence["citation"],
            }
        ],
    )
    response = client.post(
        f"/api/changesets/{draft['id']}/publish",
        json={
            "expected_revision": 1,
            "confirm_impact": False,
        },
    )
    assert response.status_code == 200
    doc = client.get(f"/api/knowledge/{response.json()['document_ids'][0]}").json()
    assert doc["citation_ids"] == [evidence["id"]]
    assert client.get(f"/api/evidence/{evidence['id']}").json()["text"] == "固定证据。"
    assert client.get("/api/knowledge/export").headers["content-type"] == "application/zip"


def test_stale_review_rejected_and_cross_origin_write_blocked(client, services):
    _, run, _ = enqueue(client)
    draft = services.knowledge.create_draft(
        run["id"],
        [
            {
                "action": "create",
                "title": "初稿",
                "body": "正文",
            }
        ],
    )
    assert (
        client.put(
            f"/api/changesets/{draft['id']}",
            json={
                "expected_revision": 1,
                "operations": [{"action": "create", "title": "改稿", "body": "新正文"}],
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/changesets/{draft['id']}/publish",
            json={
                "expected_revision": 1,
            },
        ).status_code
        == 409
    )
    assert services.knowledge.list_documents() == []
    assert (
        client.post(
            "/api/conversations", json={}, headers={"Origin": "https://unrelated.example"}
        ).status_code
        == 403
    )


def test_sse_reconnect_cursor_skips_old_events_and_terminal_closes(client, services):
    _, run, _ = enqueue(client)
    services.jobs.claim("test-worker", run_id=run["id"], generation=run["generation"])
    services.jobs.finish(run["id"], "test-worker", outcome={"kind": "answered", "answer": "结果"})
    response = client.get(f"/api/runs/{run['id']}/events", headers={"Last-Event-ID": "1"})
    assert response.status_code == 200
    assert "id: 1\n" not in response.text
    assert '"kind": "started"' in response.text
    assert '"kind": "finished"' in response.text
    assert "event: closed" in response.text


def test_worker_persists_disabled_model_failure_and_charges_nothing(services):
    worker = importlib.import_module("knowledgeops_agent.app.runs.worker")
    conversation = services.jobs.create_conversation()
    run = services.jobs.enqueue(conversation["id"], "问题", request_key=str(uuid4()))
    result = worker.execute_run(
        run["id"],
        run["generation"],
        services=services,
        checkpointer=InMemorySaver(),
        worker_id="test",
    )
    assert result["status"] == "failed"
    assert "disabled" in result["stop_reason"]
    assert services.gateway.ledger.records() == []
    assert worker.execute_run(run["id"], run["generation"], services=services) is None


def test_resume_uses_same_run_and_explicit_budget_without_reset(client, services):
    _, run, _ = enqueue(client)
    services.jobs.claim("test", run_id=run["id"], generation=run["generation"])
    services.gateway.ledger.reserve(run["id"], run["id"], "agent", [], root_limit=1)
    services.jobs.finish(run["id"], "test", status="budget_exhausted", reason="limit")
    assert (
        client.post(
            f"/api/runs/{run['id']}/resume", json={"budgets": {"model_requests": 1}}
        ).status_code
        == 409
    )
    result = client.post(f"/api/runs/{run['id']}/resume", json={"budgets": {"model_requests": 2}})
    assert result.status_code == 202
    assert result.json()["checkpoint_id"] == run["checkpoint_id"]
    assert result.json()["generation"] == run["generation"] + 1
    assert services.gateway.ledger.summary(run["id"])["root_used"] == 1


def test_resume_without_project_cap_still_requires_root_budget(client, services):
    """无累计上限不是无任务预算；显式增加根预算后才允许恢复原运行。"""
    current = services.gateway.ledger
    services.gateway.ledger = ModelLedger(current.engine, scope=current.scope)
    _, run, _ = enqueue(client)
    services.jobs.claim("test", run_id=run["id"], generation=run["generation"])
    services.gateway.ledger.reserve(run["id"], run["id"], "agent", [], root_limit=1)
    services.jobs.finish(run["id"], "test", status="budget_exhausted", reason="limit")
    assert "usage" not in client.get("/api/settings").json()
    assert (
        client.post(
            f"/api/runs/{run['id']}/resume", json={"budgets": {"model_requests": 1}}
        ).status_code
        == 409
    )
    response = client.post(f"/api/runs/{run['id']}/resume", json={"budgets": {"model_requests": 2}})
    assert response.status_code == 202
    assert response.json()["checkpoint_id"] == run["checkpoint_id"]
    assert services.gateway.ledger.summary(run["id"])["root_used"] == 1


def test_text_upload_deduplicates_and_invalid_utf8_is_explained(client):
    first = client.post(
        "/api/sources/upload", files={"file": ("说明.md", "# 正文".encode(), "text/markdown")}
    )
    again = client.post(
        "/api/sources/upload", files={"file": ("说明.md", "# 正文".encode(), "text/markdown")}
    )
    assert first.status_code == 201
    assert first.json()["version_id"] == again.json()["version_id"]
    segment = first.json()["segments"][0]["id"]
    assert client.get(f"/api/source-segments/{segment}").json()["text"] == "# 正文"
    assert (
        client.post(
            "/api/sources/upload", files={"file": ("bad.txt", b"\xff", "text/plain")}
        ).status_code
        == 422
    )


def test_user_archive_and_restore_are_reviewed_versioned_changes(client, services):
    knowledge = services.knowledge
    draft = knowledge.create_draft(
        "seed", [{"action": "create", "title": "需归档文章", "body": "应保留的正文"}]
    )
    knowledge.approve(draft["id"])
    receipt = knowledge.publish(draft["id"], idempotency_key="seed")
    doc_id, version_id = receipt["document_ids"][0], receipt["version_ids"][0]
    request = {"action": "archive", "base_version_id": version_id, "request_key": "archive"}
    response = client.post(f"/api/knowledge/{doc_id}/actions", json=request)
    assert response.status_code == 201
    archive = response.json()
    assert knowledge.get_document(doc_id)["archived"] is False
    assert (
        client.post(f"/api/knowledge/{doc_id}/actions", json=request).json()["id"] == archive["id"]
    )
    assert client.post(
        f"/api/changesets/{archive['id']}/publish", json={"expected_revision": 1}
    ).is_success
    current = knowledge.get_document(doc_id)
    assert current["archived"] is True
    restored = client.post(
        f"/api/knowledge/{doc_id}/actions",
        json={
            "action": "restore",
            "base_version_id": current["version_id"],
            "restore_version_id": version_id,
            "request_key": "restore",
        },
    ).json()
    assert restored["after"][doc_id]["body"] == "应保留的正文"
    assert client.post(
        f"/api/changesets/{restored['id']}/publish", json={"expected_revision": 1}
    ).is_success
    assert knowledge.get_document(doc_id)["version"] == 3
    assert knowledge.get_document(doc_id)["archived"] is False
    assert knowledge.get_document(doc_id, version_id=version_id)["version"] == 1


@pytest.mark.parametrize("choice", [None, False, True])
def test_auxiliary_http_choice_is_durable_and_defaults_off(client, services, choice):
    conversation = client.post("/api/conversations", json={}).json()
    body = {"content": "核对材料", "request_key": str(uuid4())}
    if choice is not None:
        body["auxiliary"] = choice
    url = f"/api/conversations/{conversation['id']}/messages"
    response = client.post(url, json=body)
    assert response.status_code == 202
    run = response.json()
    assert run["input"]["auxiliary"] is (choice is True)
    assert client.get(f"/api/runs/{run['id']}").json()["input"]["auxiliary"] is (choice is True)
    assert client.post(url, json=body).json()["id"] == run["id"]
    body["auxiliary"] = choice is not True
    assert client.post(url, json=body).status_code == 409
    assert services.gateway.ledger.records() == []


def test_legacy_auxiliary_default_retry_does_not_rewrite_history(client, services):
    from knowledgeops_agent.persistence.models import Run

    conversation, run, body = enqueue(client)
    with services.sessions.begin() as session:
        row = session.get(Run, run["id"])
        legacy_input = {key: value for key, value in row.input_data.items() if key != "auxiliary"}
        row.input_data = legacy_input
    url = f"/api/conversations/{conversation['id']}/messages"
    replay = client.post(url, json=body)
    assert replay.status_code == 202
    assert replay.json()["id"] == run["id"]
    assert replay.json()["input"] == legacy_input
    assert services.jobs.get_run(run["id"])["input"] == legacy_input
    body["auxiliary"] = True
    assert client.post(url, json=body).status_code == 409
    assert services.gateway.ledger.records() == []
