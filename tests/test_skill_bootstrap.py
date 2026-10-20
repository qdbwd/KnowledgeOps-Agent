"""初始模板只用于全新安装；重启、更新或用户删除不得复活技能。"""

from pathlib import Path

import pytest

from knowledgeops_agent.harness.skills.bootstrap import initialize_skills
from knowledgeops_agent.harness.skills.management import SkillManagement
from knowledgeops_agent.harness.tools.policy import ToolPolicyService
from knowledgeops_agent.persistence.models import SkillPackageState


def template(root: Path, name="reader", body="original"):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Read materials\n---\n{body}")
    (directory / "script.py").write_text("raise RuntimeError('must not execute')")
    return directory


def test_first_install_seeds_once_and_does_not_restore_removed_package(business_sessions, tmp_path, monkeypatch):
    seeds, root = tmp_path / "seeds", tmp_path / "skills"
    template(seeds)
    monkeypatch.setenv("SKILLS_DIRECTORY", str(root))
    result = initialize_skills(business_sessions, root, seeds)
    assert result["mode"] == "seed"
    manager = SkillManagement(business_sessions, root)
    assert manager.catalog()[0]["managed"] and manager.catalog()[0]["enabled"]
    manager.remove("reader", expected_revision=1)
    template(seeds, "new-template")
    initialize_skills(business_sessions, root, seeds)
    assert not (root / "reader").exists() and not (root / "new-template").exists()


def test_existing_installation_adopts_without_overwrite_or_policy_change(business_sessions, tmp_path, monkeypatch):
    seeds, root = tmp_path / "seeds", tmp_path / "skills"
    template(seeds)
    template(seeds, "extra")
    installed = template(root, body="user adapted")
    original = (installed / "SKILL.md").read_bytes()
    monkeypatch.setenv("SKILLS_DIRECTORY", str(root))
    policy = ToolPolicyService(business_sessions)
    policy.update(expected_revision=policy.get()["revision"], skill_enabled={"reader": False})
    before = policy.get()
    assert initialize_skills(business_sessions, root, seeds)["mode"] == "adopt"
    assert (installed / "SKILL.md").read_bytes() == original
    assert not (root / "extra").exists() and policy.get() == before
    assert SkillManagement(business_sessions, root).catalog()[0]["managed"]


def test_empty_directory_with_removed_identity_is_not_a_fresh_install(business_sessions, tmp_path):
    seeds, root = tmp_path / "seeds", tmp_path / "skills"
    template(seeds)
    with business_sessions.begin() as session:
        session.add(SkillPackageState(name="reader", content_hash="a" * 64, revision=2, removed=True))
    assert initialize_skills(business_sessions, root, seeds)["mode"] == "adopt"
    assert not (root / "reader").exists()


def test_partial_initialization_retries_without_replacing_completed_packages(business_sessions, tmp_path, monkeypatch):
    seeds, root = tmp_path / "seeds", tmp_path / "skills"
    template(seeds, "first")
    template(seeds, "second")
    original = SkillManagement.commit

    def fail_second(self, identity):
        if self.operation(identity)["target"] == "second":
            raise OSError("simulated interruption")
        return original(self, identity)

    monkeypatch.setattr(SkillManagement, "commit", fail_second)
    with pytest.raises(OSError):
        initialize_skills(business_sessions, root, seeds)
    assert (root / "first/SKILL.md").is_file()
    monkeypatch.setattr(SkillManagement, "commit", original)
    initialize_skills(business_sessions, root, seeds)
    catalog = SkillManagement(business_sessions, root).catalog()
    assert {row["name"] for row in catalog} == {"first", "second"}
    assert {row["revision"] for row in catalog} == {1}
