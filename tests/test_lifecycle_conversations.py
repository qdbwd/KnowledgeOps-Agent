"""会话删除使用真实PG、回执库、保存器与文件；永久资产不属于聊天。"""

import json

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import select
from test_lifecycle_details import checkpoint_case as _checkpoint_case
from test_lifecycle_details import detail_case as _detail_case
from test_lifecycle_details import ledger as _ledger
from test_lifecycle_details import redis_case as _redis_case

from knowledgeops_agent.app.lifecycle.conversations import ConversationCleaner
from knowledgeops_agent.harness.context.history import _digest, _store_records, visible_records
from knowledgeops_agent.learning.tasks import LearningTaskService
from knowledgeops_agent.memory.service import MemoryService
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.sources.service import SourceService

detail_case, ledger, checkpoint_case, redis_case = _detail_case, _ledger, _checkpoint_case, _redis_case


@pytest.fixture
def conversation_case(detail_case, tmp_path):
    details, runs, run, chat, clock, saver, plain, bridge, cache = detail_case
    store = FileStore(tmp_path)
    details.sessions.configure(info={"file_store": store})
    cleaner = ConversationCleaner(details, runs)
    # 模拟应用维护完成旧引用目录回填；之后新增正文由在线事务追踪。
    for _ in range(100):
        if cleaner.retained.catalog.step(limit=100)["complete"]:
            break
    else:
        pytest.fail("reference catalog did not finish")
    return cleaner, runs, run, chat, store


def drain_conversation(cleaner, job_id):
    for _ in range(240):
        result = cleaner.jobs.get(job_id)
        if result["status"] in {"failed", "completed"}:
            return result
        cleaner.step(identity=job_id)
    pytest.fail("conversation deletion did not converge")


def test_preview_is_read_only_and_pending_or_unknown_work_blocks_delete(conversation_case):
    cleaner, runs, run, chat, _ = conversation_case
    preview = cleaner.preview(chat["id"])
    assert preview["eligible"] and preview["counts"]["messages"] == 2
    with cleaner.sessions() as session:
        assert session.get(db.Conversation, chat["id"]).deletion_state == "active"
        assert list(session.scalars(select(db.CleanupJob))) == []
    late = runs.enqueue(chat["id"], "新的执行", request_key=db.new_id())
    with pytest.raises(ValueError):
        cleaner.admit(chat["id"], expected_version=preview["version"])
    assert not cleaner.preview(chat["id"])["eligible"]
    runs.cancel(late["id"])
    with cleaner.sessions.begin() as session:
        session.add(db.CommandExecution(root_id=run["id"], run_id=run["id"], tool_call_id="unknown",
            fingerprint="a" * 64, request={}, authority_epoch=0, status="unknown"))
    protected = cleaner.preview(chat["id"])
    assert any("unknown" in reason for reason in protected["blockers"])
    with pytest.raises(ValueError):
        cleaner.admit(chat["id"], expected_version=protected["version"])
    assert len(runs.messages(chat["id"])) == 3


def test_delete_removes_chat_copies_but_keeps_sources_memory_and_learning(conversation_case):
    cleaner, runs, run, chat, store = conversation_case
    sessions = cleaner.sessions
    source = SourceService(sessions, store.root).import_text("保留资料", "独立资料的完整正文")
    source_before = SourceService(sessions, store.root).get_source(source["id"])
    tasks = LearningTaskService(sessions)
    task = tasks.create(conversation_id=chat["id"], request_key=db.new_id(), title="学习任务", goal="保留学习目标")
    store.replace("outputs/independent.md", "独立产物保持原样".encode())
    artifact = runs.save_artifact(run["id"], "tool_result", "仅用于这次聊天的内部材料")
    with sessions.begin() as session:
        message = session.scalar(select(db.ChatMessage).where(db.ChatMessage.role == "user"))
        memory = db.Preference(content="回答时多解释原理", scope="global", source_message_id=message.id,
            source={"conversation_id": chat["id"], "run_id": run["id"], "quote": "必须删除的原话证据"})
        session.add(memory)
        session.flush()
        MemoryService._save(session, memory)
        MemoryService._evidence(session, memory, message=message, quote="必须删除的原话证据")
        memory_id = memory.id
        scope = _digest(["conversation", chat["id"]])
        records = visible_records([HumanMessage("必须删除的历史副本")])
        _store_records(session, scope, records)
        session.add(db.TaskHistoryBatch(scope=scope, id="old", run_id=run["id"], source_ids=[records[0]["id"]]))
        # 删除会话不需要为其继续聊天恢复不完整的旧协议。
        for item in session.scalars(select(db.ChatMessage)):
            item.protocol_payload = None
    before = cleaner.preview(chat["id"])
    job = cleaner.admit(chat["id"], expected_version=before["version"])
    assert cleaner.admit(chat["id"], expected_version=before["version"])["id"] == job["id"]
    with pytest.raises(KeyError):
        runs.messages(chat["id"])
    with pytest.raises(KeyError):
        runs.enqueue(chat["id"], "不能重新写入", request_key=db.new_id())
    result = drain_conversation(cleaner, job["id"])
    assert result["status"] == "completed", result
    with sessions() as session:
        assert session.get(db.Conversation, chat["id"]).deletion_state == "deleted"
        assert list(session.scalars(select(db.ChatMessage))) == []
        assert list(session.scalars(select(db.ContinuationSnapshot))) == []
        assert list(session.scalars(select(db.TaskHistoryRecord))) == []
        assert list(session.scalars(select(db.TaskHistoryChunk))) == []
        assert list(session.scalars(select(db.RunEvent))) == []
        old_run = session.get(db.Run, run["id"])
        assert old_run.task == "" and old_run.input_data == {} and old_run.outcome == {}
        assert old_run.details_summary == {} and old_run.lifecycle_state == "deleted"
        assert session.get(db.RunArtifact, artifact).expired_at
        assert "必须删除的原话证据" not in json.dumps([r.content for r in session.scalars(select(db.PreferenceRevision))], ensure_ascii=False)
    memory_after = MemoryService(sessions).get(memory_id)
    assert memory_after["content"] == "回答时多解释原理" and memory_after["source_message_id"] is None
    assert all(not item["quote"] and item["message_id"] is None for item in memory_after["evidence"])
    task_after = tasks.get(task["id"])
    assert task_after["conversation_id"] is None and task_after["goal"] == "保留学习目标"
    assert not task_after["conversation_available"] and not task_after["auto_publish"]
    assert task_after["status"] == "paused"
    assert SourceService(sessions, store.root).get_source(source["id"]) == source_before
    assert store.path("outputs/independent.md").read_text() == "独立产物保持原样"
    assert cleaner.details.ledger.records(root_id=run["id"]) == []


def test_delete_retries_checkpoint_failure_without_reopening_chat(conversation_case, monkeypatch):
    cleaner, runs, run, chat, _ = conversation_case
    with cleaner.details.saver() as saver:
        original = saver.delete_thread
        monkeypatch.setattr(saver, "delete_thread", lambda _: (_ for _ in ()).throw(OSError("private path")))
        preview = cleaner.preview(chat["id"])
        job = cleaner.admit(chat["id"], expected_version=preview["version"])
        failed = drain_conversation(cleaner, job["id"])
        assert failed["status"] == "failed" and failed["phase"] == "conversation_runs"
        assert "private path" not in str(failed)
        with pytest.raises(KeyError):
            runs.get_run(run["id"])
        monkeypatch.setattr(saver, "delete_thread", original)
        cleaner.retry(job["id"])
        assert drain_conversation(cleaner, job["id"])["status"] == "completed"


def test_memory_extraction_cannot_write_from_deleted_conversation(conversation_case):
    cleaner, _, _, chat, _ = conversation_case
    with cleaner.sessions() as session:
        message_id = session.scalar(select(db.ChatMessage.id).where(db.ChatMessage.role == "user"))
    preview = cleaner.preview(chat["id"])
    cleaner.admit(chat["id"], expected_version=preview["version"])
    with pytest.raises(KeyError):
        MemoryService(cleaner.sessions).extract(message_id, [], known=[])
    with cleaner.sessions() as session:
        assert list(session.scalars(select(db.PreferenceExtraction))) == []


def test_enqueue_and_delete_share_a_single_admission_boundary(conversation_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    cleaner, runs, _, chat, _ = conversation_case
    preview = cleaner.preview(chat["id"])
    barrier = Barrier(2)

    def submit():
        barrier.wait()
        try:
            return runs.enqueue(chat["id"], "并发新任务", request_key=db.new_id())
        except KeyError:
            return None

    def remove():
        barrier.wait()
        try:
            return cleaner.admit(chat["id"], expected_version=preview["version"])
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        incoming, deletion = pool.submit(submit), pool.submit(remove)
        submitted, deleted = incoming.result(timeout=15), deletion.result(timeout=15)
    assert bool(submitted) != bool(deleted)
    with cleaner.sessions() as session:
        if deleted:
            assert session.get(db.Conversation, chat["id"]).deletion_state == "deleting"
            assert list(session.scalars(select(db.Run).where(db.Run.status == "queued"))) == []
        else:
            assert session.get(db.Conversation, chat["id"]).deletion_state == "active"
            assert session.get(db.Run, submitted["id"]).status == "queued"


def test_published_body_draft_profile_and_search_survive_deletion(conversation_case):
    import hashlib

    from knowledgeops_agent.knowledge.service import KnowledgeService
    from knowledgeops_agent.learning.profiles import LearningProfileService

    cleaner, _, run, chat, store = conversation_case
    sources, knowledge = SourceService(cleaner.sessions, store.root / "uploads/originals"), KnowledgeService(cleaner.sessions)
    source = sources.import_text("清理所有权", "知识是独立资产，不随聊天清理。")
    evidence = sources.read_segment(source["segments"][0]["id"], root_id=run["id"])
    evidence_before = sources.get_evidence(evidence["id"])
    draft = knowledge.create_draft(run["id"], [{"action": "create", "title": "独立知识资产",
        "body": "# 独立知识资产\n知识拥有独立生命周期。" + evidence["citation"]}])
    published = knowledge.publish(draft["id"], idempotency_key="lifecycle-preserve", user_approval=True,
                                  expected_revision=draft["revision"])
    document = knowledge.get_document(published["document_ids"][0])
    draft_before = knowledge.get_draft(draft["id"])
    profile_service = LearningProfileService(cleaner.sessions)
    profile = profile_service.get()
    profile = profile_service.update(profile["id"], expected_revision=profile["revision"], content="先建立框架，再逐步讲原理。")
    files = {path.relative_to(store.root): hashlib.sha256(path.read_bytes()).hexdigest()
             for folder in ("knowledge", "uploads", "profiles") for path in (store.root / folder).rglob("*") if path.is_file()}
    preview = cleaner.preview(chat["id"])
    job = cleaner.admit(chat["id"], expected_version=preview["version"])
    result = drain_conversation(cleaner, job["id"])
    assert result["status"] == "completed", result
    assert knowledge.get_document(published["document_ids"][0]) == document
    assert knowledge.get_draft(draft["id"]) == draft_before
    assert profile_service.by_id(profile["id"]) == profile
    assert knowledge.search("独立知识资产")
    assert sources.get_evidence(evidence["id"]) == evidence_before
    assert files and all(hashlib.sha256((store.root / key).read_bytes()).hexdigest() == value for key, value in files.items())
