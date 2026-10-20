"""技能管理不能改变已冻结的执行文件，也不能把目录准备误认为实际使用。"""

from pathlib import Path

import pytest
from sqlalchemy import delete
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.harness.runtime.resources import ResourceService, ResourceUnavailable
from knowledgeops_agent.harness.skills.management import SkillManagement
from knowledgeops_agent.harness.skills.resources import FrozenSkillBackend
from knowledgeops_agent.harness.tools.policy import ToolPolicyService
from knowledgeops_agent.persistence.models import RunEvent

runtime_services = _runtime_services


def skill_tree(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    for name in ("used", "unrelated"):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}\n---\nRead helper.py", encoding="utf-8"
        )
        (folder / "helper.py").write_text("print('frozen')", encoding="utf-8")
    monkeypatch.setenv("SKILLS_DIRECTORY", str(root))
    return root


def disable(sessions, name):
    policy = ToolPolicyService(sessions)
    return policy.update(expected_revision=policy.get()["revision"], skill_enabled={name: False})


def test_only_successfully_read_packages_are_materialized():
    prepared = []
    backend = FrozenSkillBackend(
        {"/skills/used/SKILL.md": "instructions", "/skills/used/script.py": "print(1)",
         "/skills/unrelated/SKILL.md": "other"},
        on_materialize=prepared.append,
    )
    backend.download_files(["/skills/used/SKILL.md"])
    with backend.execution_directory() as path:
        assert list(path.iterdir()) == []
    assert prepared == []  # 目录解析与普通 Shell 命令都不等于使用技能。
    assert backend.read("/skills/used/SKILL.md").error is None
    with backend.execution_directory() as path:
        assert (path / "used/script.py").read_text() == "print(1)"
        assert not (path / "unrelated").exists()
        temporary = path
    assert not temporary.exists()
    assert {row["path"] for row in prepared} == {
        "/skills/used/SKILL.md", "/skills/used/script.py",
    }


def test_updated_package_can_remove_a_file_without_breaking_its_frozen_version(
    runtime_services, tmp_path, monkeypatch,
):
    root = skill_tree(tmp_path, monkeypatch)
    _, context, _ = setup_run(runtime_services)
    service = ResourceService(context.jobs.sessions)
    manifest = service.ensure(context)
    resource = "/skills/used/helper.py"
    (root / "used/helper.py").unlink()
    assert service.skill_available(manifest, resource)
    frozen = next(row for row in manifest["skills"] if row["resource"] == resource)
    assert frozen["content"] == "print('frozen')"
    disable(context.jobs.sessions, "used")
    assert not service.skill_available(manifest, resource)


def test_actual_usage_survives_event_cleanup_and_backend_reconstruction(
    runtime_services, tmp_path, monkeypatch,
):
    skill_tree(tmp_path, monkeypatch)
    _, context, _ = setup_run(runtime_services)
    service = ResourceService(context.jobs.sessions)
    manifest = service.ensure(context)
    entry = next(row for row in manifest["skills"] if row["resource"] == "/skills/used/SKILL.md")
    receipt = {"path": entry["resource"], "sha256": entry["sha256"], "offset": 0, "end": 4}
    service.record_skill_read(context, manifest, receipt)
    service.record_skill_read(context, manifest, receipt)  # 重读只保留一份最小依赖。
    with context.jobs.sessions.begin() as session:
        session.execute(delete(RunEvent).where(RunEvent.run_id == context.run_id))
    assert service.loaded_skill_names(context.run_id, manifest) == {"used"}
    disable(context.jobs.sessions, "unrelated")
    service.validate(context, manifest)
    disable(context.jobs.sessions, "used")
    with pytest.raises(ResourceUnavailable, match="技能.*撤回"):
        service.validate(context, manifest)


def test_preparation_events_do_not_revoke_new_tasks_but_legacy_keeps_its_contract(
    runtime_services, tmp_path, monkeypatch,
):
    skill_tree(tmp_path, monkeypatch)
    _, context, _ = setup_run(runtime_services)
    service = ResourceService(context.jobs.sessions)
    manifest = service.ensure(context)
    context.jobs.append_event(context.run_id, "skill_materialized", {"path": "/skills/unrelated/SKILL.md"})
    disable(context.jobs.sessions, "unrelated")
    service.validate(context, manifest)
    legacy = {key: value for key, value in manifest.items() if key != "skill_usage_version"}
    with pytest.raises(ResourceUnavailable, match="技能.*撤回"):
        service.validate(context, legacy)


def test_restored_loaded_skill_can_execute_its_frozen_script(tmp_path):
    backend = FrozenSkillBackend(
        {"/skills/example/SKILL.md": "run helper.py", "/skills/example/helper.py": "print('ok')"},
        loaded_skills={"example"},
    )
    with backend.execution_directory() as path:
        assert (path / "example/helper.py").read_text() == "print('ok')"
    assert not Path(path).exists()


def test_managed_update_keeps_frozen_files_and_removal_revokes_used_task(runtime_services, tmp_path, monkeypatch):
    root = skill_tree(tmp_path, monkeypatch)
    _, context, _ = setup_run(runtime_services)
    manager = SkillManagement(context.jobs.sessions, root)
    manager.adopt_existing()
    service = ResourceService(context.jobs.sessions)
    manifest = service.ensure(context)
    entry = next(row for row in manifest["skills"] if row["resource"] == "/skills/used/SKILL.md")
    service.record_skill_read(context, manifest, {"path": entry["resource"], "sha256": entry["sha256"]})
    staged = manager.stage([("SKILL.md", b"---\nname: used\ndescription: new\n---\nnew instructions")], expected_revision=1)
    manager.commit(staged["id"])
    assert service.skill_available(manifest, "/skills/used/helper.py")
    service.validate(context, manifest)
    manager.remove("used", expected_revision=2)
    with pytest.raises(ResourceUnavailable, match="技能.*撤回"):
        service.validate(context, manifest)
    assert next(row["content"] for row in manifest["skills"] if row["resource"].endswith("used/helper.py")) == "print('frozen')"


def test_new_root_recovers_an_interrupted_skill_update_before_freezing(runtime_services, tmp_path, monkeypatch):
    root = skill_tree(tmp_path, monkeypatch)
    _, context, _ = setup_run(runtime_services)
    manager = SkillManagement(context.jobs.sessions, root)
    manager.adopt_existing()
    staged = manager.stage([("SKILL.md", b"---\nname: used\ndescription: new\n---\nnew instructions")], expected_revision=1)

    def fail(event):
        if event == "new_moved":
            raise KeyboardInterrupt()

    monkeypatch.setattr(manager, "_checkpoint", fail)
    with pytest.raises(KeyboardInterrupt):
        manager.commit(staged["id"])
    manifest = ResourceService(context.jobs.sessions).ensure(context)
    assert any(row["resource"] == "/skills/used/helper.py" for row in manifest["skills"])
    assert (root / "used/helper.py").exists()
