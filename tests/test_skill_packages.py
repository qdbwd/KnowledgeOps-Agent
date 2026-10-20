"""真实文件/命令验证技能包；不安装依赖、不联网、不请求收费模型。"""

import json

import pytest

from knowledgeops_agent.harness.runtime.resources import (
    load_skills,
    skill_catalog,
)
from knowledgeops_agent.harness.sandbox.workspace import WorkspaceBackend
from knowledgeops_agent.harness.skills.packages import is_skill_entry, snapshot_files
from knowledgeops_agent.harness.skills.resources import FrozenSkillBackend, build_skill_middleware


def package(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    skill = root / "example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: example\ndescription: example\n---\nRun scripts/run.py")
    monkeypatch.setenv("SKILLS_DIRECTORY", str(root))
    return root, skill


def test_nested_fixture_is_support_data_not_registered_skill(tmp_path, monkeypatch):
    _, skill = package(tmp_path, monkeypatch)
    fixture = skill / "evals/fixtures/blocked/SKILL.md"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("---\nname: missing-description\n---\nNot a runtime skill")
    rows = load_skills()
    assert [row["name"] for row in rows if is_skill_entry(row)] == ["example"]
    assert len(rows) == 2
    assert [row["name"] for row in skill_catalog()] == ["example"]
    _, middleware = build_skill_middleware(snapshot_files(rows))
    # SDK 只看到顶层一个包；嵌套样例留作显式补读材料。
    from deepagents.middleware.skills import _list_skills
    discovered = _list_skills(FrozenSkillBackend(snapshot_files(rows)), "/skills/")
    assert [item["name"] for item in discovered] == ["example"]
    assert len(middleware) == 2


def test_scripts_assets_and_templates_are_frozen_and_execute_without_new_tool(tmp_path, monkeypatch):
    _, skill = package(tmp_path, monkeypatch)
    (skill / "scripts").mkdir()
    (skill / "assets").mkdir()
    script = skill / "scripts/run.py"
    script.write_text(
        "from pathlib import Path\n"
        "p = Path(__file__).parent.parent / 'assets/data.bin'\n"
        "Path('outputs/result.txt').write_text(p.read_bytes().hex())\n"
        "print('original-script')\n"
    )
    (skill / "assets/data.bin").write_bytes(b"\xff\x00\x81")
    frozen = FrozenSkillBackend(snapshot_files(load_skills()))
    assert frozen.read("/skills/example/SKILL.md").error is None
    script.write_text("raise RuntimeError('new deployment must not run')")
    store = WorkspaceBackend(tmp_path / "workspace", skills=frozen)
    result = store.execute('python "$SKILLS_DIR/example/scripts/run.py"; printf "%s" "$SKILLS_DIR"')
    assert result.exit_code == 0
    assert "original-script" in result.output
    assert (store.cwd / "outputs/result.txt").read_text() == "ff0081"
    from pathlib import Path
    assert not Path(result.output.splitlines()[-1]).exists()
    assert "original-script" in str(frozen.read("/skills/example/scripts/run.py"))
    assert frozen.read("/skills/example/assets/data.bin").error


def test_disabled_package_is_visible_to_operator_but_not_agent(tmp_path, monkeypatch):
    root, _ = package(tmp_path, monkeypatch)
    (root / "availability.json").write_text(json.dumps({
        "version": 1, "disabled": {"example": "requires a separate service"},
    }))
    assert load_skills() == []
    assert skill_catalog()[0]["enabled"] is False
    assert skill_catalog()[0]["unavailable_reason"] == "requires a separate service"


def test_unimplemented_skill_tool_declaration_is_not_silently_ignored(tmp_path, monkeypatch):
    _, skill = package(tmp_path, monkeypatch)
    path = skill / "SKILL.md"
    path.write_text(path.read_text().replace("description: example", "description: example\nallowed-tools: [read_file]"))
    row = skill_catalog()[0]
    assert row["enabled"] is False
    assert "allowed-tools" in row["unavailable_reason"]
    assert load_skills() == []


@pytest.mark.parametrize("directory_link", [False, True])
def test_package_cannot_capture_external_file_or_linked_directory(tmp_path, monkeypatch, directory_link):
    _, skill = package(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.py").write_text("private")
    target = outside if directory_link else outside / "data.py"
    (skill / "escape").symlink_to(target, target_is_directory=directory_link)
    assert load_skills() == []
    row = skill_catalog()[0]
    assert not row["enabled"] and "技能资源" in row["unavailable_reason"]


def test_revoked_entry_also_excludes_its_scripts_from_execution(tmp_path, monkeypatch):
    _, skill = package(tmp_path, monkeypatch)
    (skill / "run.py").write_text("print('must not run')")
    allowed = True
    frozen = FrozenSkillBackend(
        snapshot_files(load_skills()),
        available=lambda path: allowed or not path.endswith("/SKILL.md"),
    )
    allowed = False
    with frozen.execution_directory() as directory:
        assert not (directory / "example/run.py").exists()
