"""真实PG和目录验证包管理事务；模拟进程退出保留日志供新实例恢复。"""

import io
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from knowledgeops_agent.harness.skills.management import SkillManagement
from knowledgeops_agent.harness.tools.policy import ToolPolicyService
from knowledgeops_agent.persistence.models import ExtensionOperation, SkillPackageState


def files(body="initial", *, extra=True):
    result = [("SKILL.md", f"---\nname: example\ndescription: Example instructions\n---\n{body}\n".encode())]
    if extra:
        result.extend([("scripts/main.py", b"print('ready')"), ("assets/input.bin", b"\xff\x00")])
    return result


@pytest.fixture
def manager(business_sessions, tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv("SKILLS_DIRECTORY", str(root))
    return SkillManagement(business_sessions, root)


def install(manager, body="initial", expected_revision=0, **kwargs):
    staged = manager.stage(files(body, **kwargs), expected_revision=expected_revision)
    return manager.commit(staged["id"])


def test_import_preview_does_not_load_or_execute_and_commit_is_idempotent(manager):
    preview = manager.stage(files())
    assert preview["status"] == "ready"
    assert not (manager.root / "example").exists()
    assert preview["preview"]["runtime_status"] == "not_tested"
    installed = manager.commit(preview["id"])
    assert installed == manager.commit(preview["id"])
    assert installed["revision"] == 1
    assert (manager.root / "example/scripts/main.py").read_bytes() == b"print('ready')"
    assert not ToolPolicyService(manager.sessions).get()["skill_enabled"]["example"]
    with zipfile.ZipFile(io.BytesIO(manager.export("example"))) as archive:
        assert archive.read("example/assets/input.bin") == b"\xff\x00"
    with manager.sessions() as session:
        assert session.get(SkillPackageState, "example").content_hash == preview["preview"]["content_hash"]
        assert session.get(ExtensionOperation, preview["id"]).status == "succeeded"


def test_update_keeps_policy_and_rejects_stale_revision(manager):
    install(manager)
    policy = ToolPolicyService(manager.sessions)
    policy.update(expected_revision=policy.get()["revision"], skill_enabled={"example": True})
    before = policy.get()
    update = manager.stage(files("new", extra=False), expected_revision=1)
    stale = manager.stage(files("stale"), expected_revision=1)
    assert manager.commit(update["id"])["revision"] == 2
    assert not (manager.root / "example/scripts/main.py").exists()
    assert policy.get() == before
    with pytest.raises(ValueError, match="修订"):
        manager.commit(stale["id"])
    assert "new" in (manager.root / "example/SKILL.md").read_text()


class SimulatedExit(BaseException):
    pass


@pytest.mark.parametrize("point", ["old_moved", "new_moved", "database_committed"])
def test_interrupted_replacement_recovers_from_database_truth(manager, monkeypatch, point):
    install(manager)
    op = manager.stage(files("replacement"), expected_revision=1)

    def interrupt(event):
        if event == point:
            raise SimulatedExit()

    monkeypatch.setattr(manager, "_checkpoint", interrupt)
    with pytest.raises(SimulatedExit):
        manager.commit(op["id"])
    restarted = SkillManagement(manager.sessions, manager.root)
    restarted.recover()
    expected = "replacement" if point == "database_committed" else "initial"
    assert expected in (manager.root / "example/SKILL.md").read_text()
    assert not list((manager.root / ".management/journal").glob("*.json"))
    assert restarted.commit(op["id"])["revision"] == 2
    assert "replacement" in (manager.root / "example/SKILL.md").read_text()


def test_two_concurrent_updates_cannot_both_replace_one_revision(manager):
    install(manager)
    updates = [manager.stage(files(body), expected_revision=1)["id"] for body in ["one", "two"]]

    def apply(identity):
        try:
            return manager.commit(identity)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, updates))
    assert len([result for result in results if result is not None]) == 1
    with manager.sessions() as session:
        assert session.get(SkillPackageState, "example").revision == 2


def test_remove_advances_revocation_and_reimport_does_not_restore_old_permission(manager):
    install(manager)
    policy = ToolPolicyService(manager.sessions)
    policy.update(expected_revision=policy.get()["revision"], skill_enabled={"example": True})
    removed = manager.remove("example", expected_revision=1)
    assert removed["removed"] and removed["revision"] == 2
    assert not (manager.root / "example").exists()
    assert policy.get()["skill_epochs"]["example"] == 1
    assert not policy.get()["skill_enabled"]["example"]
    install(manager, expected_revision=2)
    assert policy.get()["skill_epochs"]["example"] == 1
    assert not policy.get()["skill_enabled"]["example"]


def test_existing_packages_are_adopted_without_rewriting_or_enabling(manager):
    target = manager.root / "example"
    target.mkdir()
    original = files(extra=False)[0][1]
    (target / "SKILL.md").write_bytes(original)
    assert manager.adopt_existing() == ["example"]
    assert manager.adopt_existing() == []
    assert (target / "SKILL.md").read_bytes() == original
    assert ToolPolicyService(manager.sessions).get().get("skill_enabled", {}) == {}
    manager.remove("example", expected_revision=1)
    assert manager.adopt_existing() == []


def test_missing_metadata_and_unmanaged_changes_cannot_be_silently_committed(manager):
    preview = manager.stage([("SKILL.md", b"plain instructions")])
    with pytest.raises(ValueError, match="名称|用途"):
        manager.commit(preview["id"])
    normalized = manager.stage([("SKILL.md", b"plain instructions")], name="example", description="Readable")
    manager.commit(normalized["id"])
    op = manager.stage(files("replacement"), expected_revision=1)
    (manager.root / "example/SKILL.md").write_text("manual change after preview")
    with pytest.raises(ValueError, match="本地文件"):
        manager.commit(op["id"])
    assert (manager.root / "example/SKILL.md").read_text() == "manual change after preview"


def test_reviewed_local_edits_can_be_exported_and_replaced(manager):
    install(manager)
    target = manager.root / "example/SKILL.md"
    # 格式损坏也必须能导出原字节，再以完整上传包修复。
    target.write_bytes(b"---\nname: [broken yaml\n---\nmanual text")
    with zipfile.ZipFile(io.BytesIO(manager.export("example"))) as archive:
        assert archive.read("example/SKILL.md") == target.read_bytes()
    op = manager.stage(files("replacement"), expected_revision=1)
    assert op["preview"]["local_changes"] is True
    assert manager.operation(op["id"])["preview"]["local_changes"] is True
    assert manager.commit(op["id"])["revision"] == 2
    assert "replacement" in target.read_text()


def test_interrupted_replacement_restores_reviewed_invalid_local_bytes(manager, monkeypatch):
    install(manager)
    target = manager.root / "example/SKILL.md"
    target.write_bytes(b"---\nname: [broken yaml\n---")
    original = target.read_bytes()
    op = manager.stage(files("replacement"), expected_revision=1)

    def interrupt(event):
        if event == "new_moved":
            raise SimulatedExit()

    monkeypatch.setattr(manager, "_checkpoint", interrupt)
    with pytest.raises(SimulatedExit):
        manager.commit(op["id"])
    restarted = SkillManagement(manager.sessions, manager.root)
    restarted.recover()
    assert target.read_bytes() == original
    assert restarted.commit(op["id"])["revision"] == 2


def test_expired_uploads_only_remove_owned_staging(manager):
    from datetime import timedelta

    preview = manager.stage(files())
    unmanaged = manager.root / "keep.txt"
    unmanaged.write_text("keep")
    with manager.sessions.begin() as session:
        row = session.get(ExtensionOperation, preview["id"])
        row.expires_at -= timedelta(days=2)
    assert manager.expire_staging() == 1
    assert unmanaged.read_text() == "keep"
    with pytest.raises(ValueError, match="过期"):
        manager.commit(preview["id"])
    with manager.sessions() as session:
        assert list(session.scalars(select(SkillPackageState))) == []


def test_interrupted_upload_is_registered_before_writing_and_can_expire(manager, monkeypatch):
    from datetime import timedelta

    original = manager.storage.write_candidate

    def interrupt(identity, inspection):
        original(identity, inspection)
        raise SimulatedExit()

    monkeypatch.setattr(manager.storage, "write_candidate", interrupt)
    with pytest.raises(SimulatedExit):
        manager.stage(files())
    with manager.sessions.begin() as session:
        operation = session.scalar(select(ExtensionOperation))
        assert operation is not None and operation.status == "uploading"
        operation.expires_at -= timedelta(days=2)
        identity = operation.id
    assert manager.expire_staging() == 1
    assert not manager.storage.operation_path("staging", identity).exists()


def test_failed_journal_write_leaves_current_package_and_allows_retry(manager, monkeypatch):
    install(manager)
    operation = manager.stage(files("replacement"), expected_revision=1)
    from knowledgeops_agent.harness.skills import storage

    original = storage.json.dump

    def partial(value, stream, **kwargs):
        stream.write('{"partial":')
        raise OSError("simulated disk failure")

    monkeypatch.setattr(storage.json, "dump", partial)
    with pytest.raises(ValueError, match="写入失败"):
        manager.commit(operation["id"])
    assert "initial" in (manager.root / "example/SKILL.md").read_text()
    monkeypatch.setattr(storage.json, "dump", original)
    assert manager.commit(operation["id"])["revision"] == 2


def test_operation_history_expires_without_removing_current_identity(manager):
    from datetime import timedelta

    installed = install(manager)
    with manager.sessions.begin() as session:
        operation = session.scalar(select(ExtensionOperation))
        operation.updated_at -= timedelta(days=31)
    assert manager.maintain(expire=False) == {"expired": 0, "pruned": 0}
    assert manager.maintain() == {"expired": 0, "pruned": 1}
    assert manager.catalog()[0]["revision"] == installed["revision"]
    assert (manager.root / "example/SKILL.md").is_file()
