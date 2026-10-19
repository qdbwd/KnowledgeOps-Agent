"""真实存储串联详情和材料阶段，保护可达内容并保持重试/继续聊天。"""

import hashlib

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import select
from test_lifecycle_details import checkpoint_case as _checkpoint_case
from test_lifecycle_details import detail_case as _detail_case
from test_lifecycle_details import drain
from test_lifecycle_details import ledger as _ledger
from test_lifecycle_details import redis_case as _redis_case
from test_redis_runtime import save_messages

from knowledgeops_agent.app.lifecycle.access import DetailsExpired
from knowledgeops_agent.app.lifecycle.materials import RunRetentionCleaner
from knowledgeops_agent.harness.context.history import _store_records, visible_records
from knowledgeops_agent.harness.runtime.resources import content_hash
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.files import FileStore

detail_case, ledger, checkpoint_case, redis_case = _detail_case, _ledger, _checkpoint_case, _redis_case


def finish(retained, identity):
    for _ in range(150):
        result = retained.jobs.get(identity)
        if result["status"] in {"completed", "failed"}:
            return result
        assert retained.step(identity=identity)
    pytest.fail("retained stages did not converge")


def artifact(session, store, run_id, content, kind="tool_result"):
    digest = hashlib.sha256(content.encode()).hexdigest()
    key = f"artifacts/{run_id}/{digest}.txt"
    store.put(key, content)
    row = db.RunArtifact(run_id=run_id, kind=kind, content="", content_hash=digest, content_storage_key=key)
    session.add(row)
    session.flush()
    return row


def test_full_root_cleanup_keeps_assets_history_protocol_and_retry_identity(detail_case, tmp_path):
    details, runs, run, chat, _, _, _, _, _ = detail_case
    store = FileStore(tmp_path)
    details.sessions.configure(info={"file_store": store})
    retained = RunRetentionCleaner(details)
    with details.sessions.begin() as session:
        row = session.get(db.Run, run["id"])
        key = row.request_key
        stale = artifact(session, store, run["id"], "应回收正文")
        used = artifact(session, store, run["id"], "当前笔记还要使用")
        workspace = artifact(session, store, run["id"], "用户工作文件引用")
        shared = artifact(session, store, run["id"], "当前笔记还要使用", "other_kind")
        dependent = artifact(session, store, run["id"], f"进一步见 /runs/{run['id']}/artifacts/{used.id}")
        snapshot = session.get(db.ContinuationSnapshot, session.get(db.Conversation, chat["id"]).latest_snapshot_id)
        scope = "fixture-scope"
        sources = visible_records([HumanMessage("旧索引原话"), HumanMessage("当前索引原话")])
        _store_records(session, scope, sources)
        for batch, source in zip(["old", "kept"], sources, strict=True):
            session.add(db.TaskHistoryBatch(scope=scope, id=batch, run_id=run["id"], source_ids=[source["id"]]))
        snapshot.content = {**snapshot.content, "artifact_ids": [stale.id, used.id, workspace.id, shared.id, dependent.id],
                            "task_notes": {"artifact_id": dependent.id}, "task_history": {"scope": scope, "batches": ["kept"]}}
        snapshot.content_hash = content_hash(snapshot.content)
        store.replace("outputs/kept.md", f"输出原文参照 /runs/{run['id']}/artifacts/{workspace.id}".encode())
        identities = [stale.id, used.id, workspace.id, shared.id, dependent.id]
        stale_path, shared_path = store.path(stale.content_storage_key), store.path(used.content_storage_key)
    job = details.admit(run["id"])
    assert drain(details, job["id"])["phase"] == "retained_data"
    result = finish(retained, job["id"])
    assert result["status"] == "completed", result
    assert not stale_path.exists() and shared_path.read_text() == "当前笔记还要使用"
    with details.sessions() as session:
        assert [session.get(db.RunArtifact, identity).expired_at is not None for identity in identities] == [True, False, True, True, False]
        assert session.get(db.RunArtifact, identities[2]).promoted_version_id is not None
        assert session.get(db.TaskHistoryRecord, (scope, sources[0]["id"])) is None
        assert session.get(db.TaskHistoryRecord, (scope, sources[1]["id"])) is not None
        assert session.get(db.Run, run["id"]).input_data == {}
    assert runs.get_run(run["id"])["lifecycle_state"] == "expired"
    assert runs.enqueue(chat["id"], "原任务", request_key=key)["id"] == run["id"]
    with pytest.raises(ValueError, match="different request"):
        runs.enqueue(chat["id"], "不同任务", request_key=key)
    with pytest.raises(DetailsExpired):
        runs.read_artifact(identities[0], root_id=run["id"])
    assert runs.read_artifact(identities[1], root_id=run["id"])["text"] == "当前笔记还要使用"
    next_run = runs.enqueue(chat["id"], "继续聊天", request_key=db.new_id())
    assert runs.claim("next", run_id=next_run["id"], generation=next_run["generation"])
    assert runs.read_artifact(identities[2], root_id=next_run["id"])["text"] == "用户工作文件引用"
    assert runs.messages(chat["id"])[1]["content"] == "用户看到的最终回答"
    assert store.path("outputs/kept.md").read_text() == f"输出原文参照 /runs/{run['id']}/artifacts/{identities[2]}"


def test_active_checkpoint_and_actual_read_pin_old_material_without_widening_access(detail_case, tmp_path):
    details, runs, run, chat, _, saver, _, _, _ = detail_case
    store = FileStore(tmp_path)
    details.sessions.configure(info={"file_store": store})
    retained = RunRetentionCleaner(details)
    with details.sessions.begin() as session:
        first = artifact(session, store, run["id"], "状态材料")
        second = artifact(session, store, run["id"], "读过后暂留")
        snap = session.get(db.ContinuationSnapshot, session.get(db.Conversation, chat["id"]).latest_snapshot_id)
        snap.content = {**snap.content, "artifact_ids": [first.id, second.id]}
        snap.content_hash = content_hash(snap.content)
        first_id, second_id = first.id, second.id
    job = details.admit(run["id"])
    drain(details, job["id"])
    next_run = runs.enqueue(chat["id"], "续接", request_key=db.new_id())
    runs.claim("next", run_id=next_run["id"], generation=next_run["generation"])
    save_messages(saver, {"configurable": {"thread_id": next_run["id"], "checkpoint_ns": ""}}, f"/runs/{run['id']}/artifacts/{first_id}", 1)
    assert runs.read_artifact(second_id, root_id=next_run["id"])["text"] == "读过后暂留"
    assert finish(retained, job["id"])["status"] == "completed"
    assert runs.read_artifact(first_id, root_id=next_run["id"])["text"] == "状态材料"
    with details.sessions() as session:
        assert session.get(db.ArtifactRetentionReference, (first_id, "checkpoint", next_run["id"]))
        assert session.get(db.ArtifactRetentionReference, (second_id, "run", next_run["id"]))


def test_nested_checkpoint_and_pending_write_protect_materials_and_unbatched_history(detail_case, tmp_path):
    from knowledgeops_agent.harness.context.history import _digest

    details, runs, run, chat, _, saver, _, _, _ = detail_case
    store = FileStore(tmp_path)
    details.sessions.configure(info={"file_store": store})
    retained = RunRetentionCleaner(details)
    scope = _digest(["conversation", chat["id"]])
    with details.sessions.begin() as session:
        first = artifact(session, store, run["id"], "子图仍使用的正文")
        second = artifact(session, store, run["id"], "尚未合并的并行结果")
        ids = first.id, second.id
        records = visible_records([HumanMessage("未归档且已无引用"), HumanMessage("子图待写消息", id="pending")])
        _store_records(session, scope, records)
    job = details.admit(run["id"])
    drain(details, job["id"])
    live = runs.enqueue(chat["id"], "继续", request_key=db.new_id())
    runs.claim("next", run_id=live["id"], generation=live["generation"])
    config = {"configurable": {"thread_id": live["id"], "checkpoint_ns": "tools:reader"}}
    checkpoint = save_messages(saver, config, f"/runs/{run['id']}/artifacts/{ids[0]}", 1)
    saver.put_writes(checkpoint, [("messages", [HumanMessage("子图待写消息", id="pending")]),
                                 ("task_notes", {"pending": {"artifact_id": ids[1]}})], "reader")
    # 多个主图检查点让子图落在后续分页，不能只检查第一页或空namespace。
    config = {"configurable": {"thread_id": live["id"], "checkpoint_ns": ""}}
    for version in range(1, 5):
        config = save_messages(saver, config, "主图没有材料链接", version)
    result = finish(retained, job["id"])
    assert result["status"] == "completed", result
    with details.sessions() as session:
        assert all(session.get(db.RunArtifact, identity).expired_at is None for identity in ids)
        assert session.get(db.TaskHistoryRecord, (scope, records[0]["id"])) is None
        assert session.get(db.TaskHistoryRecord, (scope, records[1]["id"])) is not None


def test_unlink_then_transaction_failure_retries_without_touching_user_files(detail_case, tmp_path, monkeypatch):
    details, _, run, _, _, _, _, _, _ = detail_case
    store = FileStore(tmp_path)
    details.sessions.configure(info={"file_store": store})
    retained = RunRetentionCleaner(details)
    with details.sessions.begin() as session:
        row = artifact(session, store, run["id"], "无引用材料")
        identity, path = row.id, store.path(row.content_storage_key)
    store.replace("work/note.txt", b"independent")
    job = details.admit(run["id"])
    drain(details, job["id"])
    import knowledgeops_agent.app.lifecycle.materials as module
    original = module.remove_artifact_file

    def fail_after_unlink(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("sensitive local path")

    monkeypatch.setattr(module, "remove_artifact_file", fail_after_unlink)
    result = finish(retained, job["id"])
    assert result["status"] == "failed" and result["phase"] == "retained_artifact_files"
    assert not path.exists() and result["last_error"] == "OSError"
    with details.sessions() as session:
        assert session.get(db.RunArtifact, identity).expired_at is not None
    monkeypatch.setattr(module, "remove_artifact_file", original)
    retained.jobs.retry(job["id"])
    assert finish(retained, job["id"])["status"] == "completed"
    assert store.path("work/note.txt").read_bytes() == b"independent"


def test_archived_directory_is_verified_then_removed_in_resumable_batches(detail_case, tmp_path, monkeypatch):
    details, _, run, _, _, _, _, _, _ = detail_case
    store = FileStore(tmp_path)
    details.sessions.configure(info={"file_store": store})
    retained = RunRetentionCleaner(details)
    from knowledgeops_agent.harness.sandbox.files import file_hash
    operation = db.new_id()
    archived = store.path(f"artifacts/file-operations/{operation}/deleted")
    for name in ["a.md", "inner/b.md", "inner/c.md"]:
        store.replace(f"artifacts/file-operations/{operation}/deleted/{name}", name.encode())
    with details.sessions.begin() as session:
        session.add(db.FileOperation(id=operation, root_id=run["id"], run_id=run["id"], tool_call_id="delete",
                    fingerprint="a" * 64, workspace=str(store.root), path="work/old", operation="delete",
                    authority_epoch=0, before_hash=file_hash(archived), status="succeeded"))
    job = details.admit(run["id"])
    drain(details, job["id"])
    for _ in range(100):
        status = retained.jobs.get(job["id"])
        if status["phase"] == "retained_file_tree":
            break
        assert status["status"] != "failed", status
        retained.step(identity=job["id"])
    else:
        pytest.fail("directory manifest was not committed")
    assert archived.is_dir() and len(status["cursor"]["entries"]) == 5
    import knowledgeops_agent.app.lifecycle.materials as module
    original = module.remove_operation_tree_entry

    def crash_after_one(*args):
        original(*args)
        raise OSError("crash after leaf removal")

    monkeypatch.setattr(module, "remove_operation_tree_entry", crash_after_one)
    failed = retained.step(identity=job["id"])
    assert failed["status"] == "failed" and failed["cursor"] == status["cursor"]
    monkeypatch.setattr(module, "remove_operation_tree_entry", original)
    retained.jobs.retry(job["id"])
    assert finish(retained, job["id"])["status"] == "completed"
    assert not archived.exists()
    with details.sessions() as session:
        assert session.get(db.FileOperation, operation) is None


def test_long_application_receipt_is_not_reclassified_as_model_response_after_expiry(detail_case, monkeypatch):
    from types import SimpleNamespace

    from langchain_core.messages import SystemMessage

    from knowledgeops_agent.harness.runtime.agent import initial_messages
    details, runs, run, chat, _, _, _, _, _ = detail_case
    receipt = "这是应用的确定性回执。" * 300
    with details.sessions.begin() as session:
        message = session.scalar(select(db.ChatMessage).where(db.ChatMessage.run_id == run["id"], db.ChatMessage.role == "assistant"))
        message.content, message.protocol_payload = receipt, None
        session.get(db.Run, run["id"]).outcome = {"answer_origin": "application_receipt", "answer": receipt}
        session.get(db.Conversation, chat["id"]).latest_snapshot_id = None
    retained = RunRetentionCleaner(details)
    job = details.admit(run["id"])
    drain(details, job["id"])
    assert finish(retained, job["id"])["status"] == "completed"
    followup = runs.enqueue(chat["id"], "继续", request_key=db.new_id())
    runs.claim("next", run_id=followup["id"], generation=1)

    def no_ledger(**_):
        pytest.fail("application receipt must not require model records")

    monkeypatch.setattr(details.ledger, "records", no_ledger)
    context = SimpleNamespace(jobs=runs, run_id=followup["id"], gateway=SimpleNamespace(ledger=details.ledger))
    messages = initial_messages(context)
    assert any(isinstance(message, SystemMessage) and receipt in message.content for message in messages)
