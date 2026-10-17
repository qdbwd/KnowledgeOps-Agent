"""旧回执只能按实际响应迁移；不能用近似答案或空协议冒充可安全清理。"""

from datetime import UTC, datetime, timedelta

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.app.lifecycle.continuity import plan_protocol_backfill, restore_message
from knowledgeops_agent.app.lifecycle.service import LifecycleService
from knowledgeops_agent.harness.context.continuation import from_state
from knowledgeops_agent.persistence import models as db

runtime_services = _runtime_services


def test_citation_rendering_keeps_exact_protocol_without_hidden_text_in_chat(runtime_services):
    _, context, run = setup_run(runtime_services)
    source = context.sources.import_text("来源", "已经核对的事实")
    evidence = context.sources.read_segment(source["segments"][0]["id"], root_id=run["id"])
    identity = evidence["evidence_id"]
    from knowledgeops_agent.knowledge.citations import render_answer_citations

    original = f"已经核对的事实 [citation:{identity}]"
    answer = render_answer_citations(original, {identity})
    state = {"messages": [HumanMessage("原问题"), AIMessage(original, additional_kwargs={"reasoning_content": "真实附属协议"})]}
    context.jobs.finish(run["id"], context.worker_id, outcome={"answer": answer}, continuation=from_state(context, state))
    with context.jobs.sessions() as session:
        message = session.scalar(select(db.ChatMessage).where(db.ChatMessage.run_id == run["id"], db.ChatMessage.role == "assistant"))
        restored = restore_message(message.content, message.protocol_payload)
        assert restored.content == answer
        assert restored.additional_kwargs["reasoning_content"] == "真实附属协议"
    assert "真实附属协议" not in str(context.jobs.messages(run["conversation_id"]))


def test_legacy_backfill_is_readonly_until_applied_and_requires_exact_same_run_response(runtime_services):
    _, context, run = setup_run(runtime_services)
    context.jobs.finish(run["id"], context.worker_id, outcome={"answer": "旧答案"})
    original = AIMessage("旧答案", additional_kwargs={"reasoning_content": "旧协议"})
    records = [{"id": "known", "run_id": run["id"], "purpose": "agent", "status": "succeeded", "response": original.model_dump(mode="json")}]
    with context.jobs.sessions.begin() as session:
        root = session.get(db.Run, run["id"])
        message = session.scalar(select(db.ChatMessage).where(db.ChatMessage.run_id == run["id"], db.ChatMessage.role == "assistant"))
        updates, blockers = plan_protocol_backfill(session, root, records)
        assert not blockers and message.id in updates
        assert message.protocol_payload is None
        message.protocol_payload = updates[message.id]
        updates2, blockers2 = plan_protocol_backfill(session, root, [])
        assert not updates2 and not blockers2
        assert restore_message(message.content, message.protocol_payload).additional_kwargs["reasoning_content"] == "旧协议"


def test_legacy_mismatch_stays_protected_instead_of_guessing_protocol(runtime_services):
    _, context, run = setup_run(runtime_services)
    context.jobs.finish(run["id"], context.worker_id, outcome={"answer": "答案"})
    record = {"id": "different", "run_id": run["id"], "purpose": "agent", "status": "succeeded", "response": AIMessage("相近但不同的答案").model_dump(mode="json")}
    with context.jobs.sessions() as session:
        updates, blockers = plan_protocol_backfill(session, session.get(db.Run, run["id"]), [record])
        assert not updates and any("protocol_unproven" in item for item in blockers)


def test_final_model_settlement_starts_full_retention_after_unknown_is_resolved(runtime_services):
    _, context, run = setup_run(runtime_services)
    context.jobs.finish(run["id"], context.worker_id, outcome={})
    clock = [datetime.now(UTC)]
    service = LifecycleService(context.jobs.sessions, context.gateway.ledger, now=lambda: clock[0])
    reservation = context.gateway.ledger.reserve(run["id"], run["id"], "agent", [])
    service.refresh_retention()
    assert not service.preview_root(run["id"])["eligible"]
    clock[0] += timedelta(days=60)
    # 这里只写真实回执状态，不调用供应商。结算时刻由测试控制，模拟晚处理的未知操作。
    context.gateway.ledger.finish(reservation, error="确认未成功")
    from knowledgeops_agent.harness.models.gateway import requests

    with context.gateway.ledger.engine.begin() as connection:
        connection.execute(requests.update().where(requests.c.id == reservation).values(finished_at=clock[0]))
    service.refresh_retention()
    assert service.preview_root(run["id"])["details_expires_at"] == (clock[0] + timedelta(days=30)).isoformat()
