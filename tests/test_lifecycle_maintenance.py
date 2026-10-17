"""每日纳管和及时接续分开，关闭扫描不丢已接受作业；多进程通过PG租约协作。"""

from datetime import timedelta

import pytest
from sqlalchemy import select
from test_lifecycle_details import checkpoint_case as _checkpoint_case
from test_lifecycle_details import detail_case as _detail_case
from test_lifecycle_details import ledger as _ledger
from test_lifecycle_details import redis_case as _redis_case
from test_lifecycle_materials import artifact

from knowledgeops_agent.app.lifecycle.maintenance import SCAN_STATE, LifecycleMaintenance
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.files import FileStore

detail_case, ledger, checkpoint_case, redis_case = _detail_case, _ledger, _checkpoint_case, _redis_case


def test_daily_scanner_resumes_all_stages_and_reclaims_material_after_last_reference_disappears(detail_case, tmp_path):
    details, runs, run, _, clock, _, _, _, _ = detail_case
    store = FileStore(tmp_path)
    details.sessions.configure(info={"file_store": store})
    maintenance = LifecycleMaintenance(details)
    with details.sessions.begin() as session:
        item = artifact(session, store, run["id"], "被文件引用")
        identity, path = item.id, store.path(item.content_storage_key)
    store.replace("work/reference.md", identity.encode())
    for _ in range(150):
        result = maintenance.tick()
        assert not (result.get("scan") or {}).get("error"), result
        if runs.get_run(run["id"])["lifecycle_state"] == "expired":
            break
    else:
        pytest.fail("maintenance did not finish the due root")
    assert path.exists()
    with details.sessions() as session:
        state = session.get(db.MaintenanceState, SCAN_STATE)
        assert state.next_scan_at > clock[0]
        job = session.scalar(select(db.CleanupJob).where(db.CleanupJob.target_id == run["id"]))
        job_id = job.id
    store.path("work/reference.md").unlink()
    assert maintenance.tick()["scan"] is None
    clock[0] += timedelta(days=1, seconds=1)
    for _ in range(100):
        # 模拟API重启：接续信息在PG，不依赖上一个协调器的内存。
        result = LifecycleMaintenance(details).tick()
        assert not (result.get("scan") or {}).get("error"), result
        if runs.get_run(run["id"])["lifecycle_state"] == "expired" and not path.exists():
            break
    else:
        pytest.fail("reference release did not trigger reconciliation")
    assert maintenance.jobs.get(job_id)["status"] == "completed"


def test_reconcile_refreshes_checkpoint_pins_and_finds_unbatched_history(detail_case, tmp_path):
    from langchain_core.messages import HumanMessage
    from test_lifecycle_details import drain
    from test_lifecycle_materials import finish
    from test_redis_runtime import save_messages

    from knowledgeops_agent.harness.context.history import _digest, _store_records, visible_records

    details, runs, run, chat, _, saver, _, _, _ = detail_case
    store = FileStore(tmp_path)
    details.sessions.configure(info={"file_store": store})
    maintenance = LifecycleMaintenance(details)
    with details.sessions.begin() as session:
        item = artifact(session, store, run["id"], "仅当前检查点使用")
        identity = item.id
    live = runs.enqueue(chat["id"], "继续", request_key=db.new_id())
    runs.claim("next", run_id=live["id"], generation=live["generation"])
    config = save_messages(saver, {"configurable": {"thread_id": live["id"], "checkpoint_ns": ""}}, identity, 1)
    job = details.admit(run["id"])
    drain(details, job["id"])
    assert finish(maintenance.retained, job["id"])["status"] == "completed"
    with details.sessions() as session:
        assert session.get(db.RunArtifact, identity).expired_at is None
    save_messages(saver, config, "已无需旧材料", 2)
    scope = _digest(["conversation", chat["id"]])
    record = visible_records([HumanMessage("已查询但未归档的旧原话")])[0]
    with details.sessions.begin() as session:
        _store_records(session, scope, [record])
    maintenance._reconcile(run["id"])
    assert finish(maintenance.retained, job["id"])["status"] == "completed"
    with details.sessions() as session:
        assert session.get(db.RunArtifact, identity).expired_at is not None
        assert session.get(db.TaskHistoryRecord, (scope, record["id"])) is None


def test_disabling_scan_preserves_new_data_but_continues_already_accepted_cleanup(detail_case):
    details, runs, run, _, _, _, _, _, _ = detail_case
    maintenance = LifecycleMaintenance(details, enabled=False)
    assert maintenance.tick() == {"job": None}
    with details.sessions() as session:
        assert session.get(db.MaintenanceState, SCAN_STATE) is None
    job = details.admit(run["id"])
    for _ in range(150):
        result = maintenance.tick()
        if result["job"] and result["job"]["status"] == "completed":
            break
    else:
        pytest.fail("accepted cleanup was stranded by disabling scans")
    assert maintenance.jobs.get(job["id"])["status"] == "completed"
    assert runs.get_run(run["id"])["lifecycle_state"] == "expired"


def test_scanner_lease_fences_two_instances_and_expiry_allows_recovery(detail_case):
    details, _, _, _, clock, _, _, _, _ = detail_case
    first, second = LifecycleMaintenance(details), LifecycleMaintenance(details)
    token, _ = first._claim_scan()
    assert second._claim_scan() is None
    clock[0] += timedelta(seconds=details.jobs.lease_seconds + 1)
    successor, cursor = second._claim_scan()
    assert successor != token and cursor == {"phase": "adopt"}


def test_real_saver_factory_obeys_injected_database_schema(business_sessions):
    from types import SimpleNamespace

    from knowledgeops_agent.app.runs.worker import open_checkpointer
    services = SimpleNamespace(engine=business_sessions.kw["bind"], checkpoint_cache=None)
    with business_sessions() as session:
        expected = session.connection().exec_driver_sql("SELECT current_schema()").scalar_one()
    with open_checkpointer(services) as saver:
        row = saver.conn.execute("SELECT current_schema() AS name").fetchone()
        assert row["name"] == expected and expected.startswith("test_")
        assert saver.get_tuple({"configurable": {"thread_id": "not-created"}}) is None


def test_temporary_scanner_uses_grace_and_durable_owner_state(detail_case, tmp_path):
    from knowledgeops_agent.persistence.temporary import ManagedTemporary

    details, _, run, _, clock, _, _, _, _ = detail_case
    store = FileStore(tmp_path)
    details.sessions.configure(info={"file_store": store})
    upload = ManagedTemporary(tmp_path, kind="upload")
    upload.close(remove=False)
    capture = ManagedTemporary(tmp_path, kind="command-output", owner_id=run["id"])
    capture.close(remove=False)
    maintenance = LifecycleMaintenance(details)
    with details.sessions.begin() as session:
        session.add(db.MaintenanceState(name=SCAN_STATE, cursor={"phase": "temporary"}, next_scan_at=clock[0], updated_at=clock[0]))
    result = maintenance.scan_step()
    assert result["removed"] == 1 and result["protected"] == 1
    assert not upload.path.exists() and capture.path.exists()
    with details.sessions.begin() as session:
        session.get(db.Run, run["id"]).lifecycle_state = "expired"
        state = session.get(db.MaintenanceState, SCAN_STATE)
        state.cursor, state.next_scan_at = {"phase": "temporary"}, clock[0]
    assert maintenance.scan_step()["removed"] == 1
    assert not capture.path.exists()
