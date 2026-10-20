"""真实PG保存器、独立模型回执库与Redis验证执行详情清理的交接阶段。"""

from contextlib import nullcontext
from datetime import timedelta

import pytest
from langchain_core.messages import AIMessage, message_to_dict
from sqlalchemy import select
from test_model_gateway import ledger as _ledger
from test_redis_runtime import checkpoint_case as _checkpoint_case
from test_redis_runtime import redis_case as _redis_case
from test_redis_runtime import save_messages

from knowledgeops_agent.app.lifecycle.details import RunDetailCleaner
from knowledgeops_agent.app.runs.events import RedisEventBridge
from knowledgeops_agent.app.runs.service import RunService
from knowledgeops_agent.harness.models.gateway import ModelCallFailed
from knowledgeops_agent.persistence import models as db

ledger, checkpoint_case, redis_case = _ledger, _checkpoint_case, _redis_case


@pytest.fixture
def detail_case(business_sessions, ledger, checkpoint_case, redis_case):
    saver, plain, cache, _ = checkpoint_case
    url, client, prefix = redis_case
    bridge = RedisEventBridge(client, url, prefix)
    clock = [db.utcnow() + timedelta(days=1)]
    runs = RunService(business_sessions, now=lambda: clock[0], event_bridge=bridge)
    chat = runs.create_conversation("保留聊天")
    run = runs.enqueue(chat["id"], "原任务", request_key=db.new_id())
    runs.claim("fixture", run_id=run["id"], generation=run["generation"])
    final = AIMessage("用户看到的最终回答", additional_kwargs={"reasoning_content": "协议原文"})
    reservation = ledger.reserve(run["id"], run["id"], "agent", [{"role": "user", "content": "完整请求"}])
    ledger.finish(reservation, response=final.model_dump(mode="json"))
    runs.finish(run["id"], "fixture", outcome={"answer": final.text}, continuation={
        "messages": [message_to_dict(final)], "final_assistant_message": message_to_dict(final),
    })
    checkpoint = save_messages(saver, {"configurable": {"thread_id": run["id"], "checkpoint_ns": ""}}, "完整恢复信息", 1)
    saver.get_tuple(checkpoint)  # 真实建立Redis副本。
    cleaner = RunDetailCleaner(
        business_sessions, ledger, lambda: nullcontext(saver), now=lambda: clock[0],
        batch_size=2, event_bridge=bridge, checkpoint_cache=cache,
    )
    cleaner.eligibility.refresh_retention()
    clock[0] += timedelta(days=31)
    return cleaner, runs, run, chat, clock, saver, plain, bridge, cache


def drain(cleaner, identity):
    for _ in range(70):
        state = cleaner.jobs.get(identity)
        if state["phase"] == "retained_data" or state["status"] == "failed":
            return state
        assert cleaner.step(identity=identity) is not None
    pytest.fail("detail stages did not converge")


def test_regular_maintenance_reclaims_expired_skill_uploads(detail_case, tmp_path):
    from knowledgeops_agent.app.lifecycle.maintenance import LifecycleMaintenance
    from knowledgeops_agent.harness.skills.management import SkillManagement

    cleaner = detail_case[0]
    manager = SkillManagement(cleaner.sessions, tmp_path / "skills")
    staged = manager.stage([("SKILL.md", b"---\nname: example\ndescription: example\n---\nbody")])
    with cleaner.sessions.begin() as session:
        session.get(db.ExtensionOperation, staged["id"]).expires_at -= timedelta(days=2)
    maintenance = LifecycleMaintenance(cleaner, skill_manager=manager)
    assert maintenance.tick()["skills"]["expired"] == 1
    assert not manager.storage.operation_path("staging", staged["id"]).exists()


def test_details_are_removed_but_chat_protocol_survives_and_job_is_not_prematurely_done(detail_case, ledger):
    cleaner, runs, run, chat, _, _, plain, bridge, cache = detail_case
    with cleaner.sessions.begin() as session:
        # 模拟旧消息，须从同Run的成功回执证明并迁出协议，预览不能写入。
        message = session.scalar(select(db.ChatMessage).where(db.ChatMessage.role == "assistant"))
        message.protocol_payload = None
        message_id = message.id
        session.add(db.RunSkillUse(run_id=run["id"], resource="/skills/example/SKILL.md", content_hash="a" * 64))
    preview = cleaner.preview(run["id"])
    assert preview["eligible"] and preview["protocol_backfill_count"] == 1
    with cleaner.sessions() as session:
        assert session.get(db.ChatMessage, message_id).protocol_payload is None
    admitted = cleaner.admit(run["id"])
    assert cleaner.admit(run["id"])["id"] == admitted["id"]
    assert not runs.get_run(run["id"])["resumable"]
    with pytest.raises(ValueError, match="清理"):
        runs.resume(run["id"])
    state = drain(cleaner, admitted["id"])
    assert state["phase"] == "retained_data" and state["status"] == "pending"
    assert cleaner.step(identity=admitted["id"]) is None  # 后续材料阶段尚未接续，不能冒报整个作业成功。
    assert ledger.records(root_id=run["id"]) == []
    assert plain.get_tuple({"configurable": {"thread_id": run["id"]}}) is None
    assert bridge.client.exists(bridge.key(run["id"])) == 0
    assert not list(cache.client.scan_iter(match=cache.thread_prefix(run["id"]) + "*"))
    assert runs.messages(chat["id"])[-1]["content"] == "用户看到的最终回答"
    with cleaner.sessions() as session:
        protocol = session.get(db.ChatMessage, message_id).protocol_payload
        assert protocol["message"]["data"]["additional_kwargs"]["reasoning_content"] == "协议原文"
        assert list(session.scalars(select(db.RunEvent))) == []
        assert list(session.scalars(select(db.RunSkillUse))) == []
    with pytest.raises(ModelCallFailed, match="expired"):
        ledger.reserve(run["id"], run["id"], "agent", [])
    assert runs.enqueue(chat["id"], "继续", request_key="after-cleanup")["status"] == "queued"


def test_checkpoint_failure_retries_same_stage_without_reopening_root(detail_case, ledger, monkeypatch):
    cleaner, runs, run, _, _, saver, plain, _, _ = detail_case
    job = cleaner.admit(run["id"])
    cleaner.step(identity=job["id"])
    original = saver.delete_thread

    def fail_after_delete(identity):
        original(identity)
        raise OSError("private path must not be copied")

    monkeypatch.setattr(saver, "delete_thread", fail_after_delete)
    failed = cleaner.step(identity=job["id"])
    assert failed["status"] == "failed" and failed["phase"] == "checkpoints"
    assert failed["last_error"] == "OSError"
    assert plain.get_tuple({"configurable": {"thread_id": run["id"]}}) is None
    assert len(ledger.records(root_id=run["id"])) == 1
    assert runs.get_run(run["id"])["lifecycle_state"] == "cleaning"
    monkeypatch.setattr(saver, "delete_thread", original)
    cleaner.jobs.retry(job["id"])
    assert drain(cleaner, job["id"])["phase"] == "retained_data"


def test_cache_failure_is_observable_and_cannot_be_claimed_as_complete(detail_case, monkeypatch):
    cleaner, _, run, _, _, _, _, bridge, _ = detail_case
    job = cleaner.admit(run["id"])
    original = bridge.delete_run
    monkeypatch.setattr(bridge, "delete_run", lambda _: (_ for _ in ()).throw(ConnectionError("cache down")))
    failed = drain(cleaner, job["id"])
    assert failed["status"] == "failed" and failed["phase"] == "caches"
    assert bridge.client.exists(bridge.key(run["id"]))
    monkeypatch.setattr(bridge, "delete_run", original)
    cleaner.jobs.retry(job["id"])
    assert drain(cleaner, job["id"])["phase"] == "retained_data"


def test_unprovable_legacy_protocol_blocks_admission_without_sealing_or_purging(detail_case, ledger):
    cleaner, runs, run, _, _, _, _, _, _ = detail_case
    with cleaner.sessions.begin() as session:
        message = session.scalar(select(db.ChatMessage).where(db.ChatMessage.role == "assistant"))
        message.protocol_payload, message.content = None, "没有对应回执的旧回答"
    assert any("protocol_unproven" in reason for reason in cleaner.preview(run["id"])["blockers"])
    with pytest.raises(ValueError, match="protocol_unproven"):
        cleaner.admit(run["id"])
    assert runs.get_run(run["id"])["lifecycle_state"] == "active"
    assert len(ledger.records(root_id=run["id"])) == 1
    with cleaner.sessions() as session:
        assert list(session.scalars(select(db.CleanupJob))) == []
