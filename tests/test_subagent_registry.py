"""复用事务/不可变修订的角色目录边界，不启动模型或真实业务运行。"""

import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


@pytest.fixture
def registry(business_sessions):
    module = importlib.import_module("knowledgeops_agent.subagent_registry")
    return module.SubagentRegistry(business_sessions)


def fields(**changes):
    return {
        "name": "condition-reviewer",
        "display_name": "条件核对",
        "description": "核对任务所给原文的条件。",
        "system_prompt": "只读原文并列出支持与不支持的结论。",
        "tools": ["read_source", "read_tool_result"],
        "skills": ["evidence-check"],
        "enabled": True,
        **changes,
    }


def test_default_snapshot_builtin_and_disabled_custom_are_separate(registry):
    builtin = registry.snapshot()[0]
    assert builtin == registry.get("builtin-researcher")
    assert builtin["name"] == "researcher" and builtin["builtin"] is True
    assert registry.validate_binding(builtin) == builtin
    created = registry.create(**fields(enabled=False))
    assert len(registry.list()) == 2
    assert registry.snapshot() == [builtin]
    assert registry.history(created["id"]) == [created]
    with pytest.raises(PermissionError):
        registry.update("builtin-researcher", expected_revision=1, system_prompt="替换")
    with pytest.raises(ValueError):
        registry.create(**fields(name="researcher"))


@pytest.mark.parametrize("change", ["add", "remove"])
def test_builtin_binding_survives_live_catalog_changes(registry, monkeypatch, change):
    """目录增删不能给旧内置角色扩权，也不能使未改变的冻结修订失效。"""
    module = importlib.import_module("knowledgeops_agent.subagent_registry")
    catalog = importlib.import_module("knowledgeops_agent.tool_catalog")
    frozen = registry.get("builtin-researcher")
    skill_rows = module.load_skills()
    if change == "add":
        monkeypatch.setattr(
            catalog,
            "STATIC_TOOLS",
            (
                *catalog.STATIC_TOOLS,
                catalog.ToolDefinition("new_read_tool", "后来增加的只读工具", "materials_read"),
            ),
        )
        skill_rows.append(
            {
                "resource": "/skills/new-skill/SKILL.md",
                "name": "new-skill",
                "content": "---\nname: new-skill\ndescription: 后来增加的技能\n---\n正文",
            }
        )
    else:
        monkeypatch.setattr(
            catalog,
            "STATIC_TOOLS",
            tuple(item for item in catalog.STATIC_TOOLS if item.name != "read_source"),
        )
        skill_rows = [
            row for row in skill_rows if not row["resource"].startswith("/skills/evidence-check/")
        ]
    monkeypatch.setattr(module, "load_skills", lambda: skill_rows)
    live = registry.options()
    if change == "add":
        assert "new_read_tool" in {row["name"] for row in live["tools"]}
        assert "new-skill" in {row["name"] for row in live["skills"]}
        # 展示和创建必须使用同一实时目录，不能展示可选项却仍按旧副本拒绝。
        created = registry.create(**fields(tools=["new_read_tool"], skills=[]))
        assert created["tools"] == ["new_read_tool"]
    else:
        assert "read_source" not in {row["name"] for row in live["tools"]}
        assert "evidence-check" not in {row["name"] for row in live["skills"]}
        with pytest.raises(ValueError, match="未知|不允许"):
            registry.create(**fields(tools=["read_source"], skills=[]))

    assert registry.validate_binding(frozen) == frozen
    assert registry.get("builtin-researcher") == frozen
    assert registry.history("builtin-researcher") == [frozen]


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": 999},
        {"revision": True},
        {"name": "forged-researcher"},
        {"system_prompt": "改写原指令"},
        {"tools": ["publish_changes"]},
        {"skills": ["new-skill"]},
        {"authorization_epoch": 1},
        {"enabled": False},
        {"builtin": False},
    ],
)
def test_builtin_binding_rejects_unknown_revision_and_changed_fields(registry, changes):
    forged = {**registry.get("builtin-researcher"), **changes}
    with pytest.raises(PermissionError):
        registry.validate_binding(forged)


def test_builtin_catalog_result_cannot_mutate_another_binding(registry):
    frozen = registry.get("builtin-researcher")
    edited = registry.get("builtin-researcher")
    edited["tools"].clear()
    edited["skills"].append("unexpected-skill")
    assert registry.get("builtin-researcher") == frozen
    assert registry.validate_binding(frozen) == frozen


def test_builtin_new_revision_keeps_old_template_available(registry, monkeypatch):
    """模拟发布新版内置模板；新任务用新版，旧绑定和历史仍按原修订解释。"""
    module = importlib.import_module("knowledgeops_agent.subagent_registry")
    original = registry.get("builtin-researcher")
    replacement = {**deepcopy(original), "revision": 2, "system_prompt": "新版模板"}
    monkeypatch.setattr(module, "_BUILTIN_REVISIONS", {1: original, 2: replacement})
    assert registry.get("builtin-researcher") == replacement
    assert registry.validate_binding(original) == original
    assert registry.validate_binding(replacement) == replacement
    assert registry.history("builtin-researcher") == [original, replacement]
    with pytest.raises(PermissionError):
        registry.validate_binding({**replacement, "revision": 1})


def test_prompt_revision_does_not_revoke_old_binding_but_tampering_is_rejected(registry):
    first = registry.create(**fields())
    second = registry.update(
        first["id"],
        expected_revision=1,
        display_name="新显示名",
        system_prompt="新增解释要求。",
        tools=[*first["tools"], "list_materials"],
    )
    assert second["revision"] == 2 and second["authorization_epoch"] == 0
    assert registry.validate_binding(first) == first
    assert registry.snapshot()[-1] == second
    changed = deepcopy(first)
    changed["system_prompt"] = second["system_prompt"]
    with pytest.raises(PermissionError):
        registry.validate_binding(changed)
    changed = deepcopy(first)
    changed["tools"].append("publish_changes")
    with pytest.raises(PermissionError):
        registry.validate_binding(changed)
    assert registry.history(first["id"]) == [first, second]


@pytest.mark.parametrize(
    "change", [{"enabled": False}, {"deleted": True}, {"tools": ["read_source"]}, {"skills": []}]
)
def test_revocation_fences_old_binding_even_after_reenable_or_regrant(registry, change):
    first = registry.create(**fields())
    revised = registry.update(first["id"], expected_revision=1, **change)
    assert revised["authorization_epoch"] == 1
    with pytest.raises(PermissionError):
        registry.validate_binding(first)
    if not change.get("deleted"):
        current = registry.update(
            first["id"],
            expected_revision=2,
            enabled=True,
            tools=first["tools"],
            skills=first["skills"],
        )
        assert current["authorization_epoch"] == 1
        assert registry.validate_binding(current) == current
        with pytest.raises(PermissionError):
            registry.validate_binding(first)
    else:
        with pytest.raises(KeyError):
            registry.get(first["id"])
        assert registry.history(first["id"])[-1] == revised


def test_disabled_historical_revision_cannot_become_an_active_binding(registry):
    disabled = registry.create(**fields(enabled=False))
    registry.update(disabled["id"], expected_revision=1, enabled=True)
    with pytest.raises(PermissionError):
        registry.validate_binding(disabled)


def test_competing_updates_cannot_overwrite_the_same_revision(registry):
    first = registry.create(**fields())

    def update(label):
        try:
            return registry.update(first["id"], expected_revision=1, display_name=label)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["甲", "乙"]))
    assert sum(row is not None for row in results) == 1
    assert len(registry.history(first["id"])) == 2


def test_catalog_limit_is_atomic_and_disabled_roles_still_count(registry):
    for index in range(19):
        registry.create(**fields(name=f"role-{index}", enabled=False))

    def create(index):
        try:
            return registry.create(**fields(name=f"last-{index}", enabled=False))
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, [0, 1]))
    assert sum(row is not None for row in results) == 1
    assert len(registry.list()) == 21  # 20个自定义加内置。
    row = next(row for row in registry.list() if not row["builtin"])
    registry.update(row["id"], expected_revision=1, deleted=True)
    assert registry.create(**fields(name="replacement", enabled=False))


def test_0014_incremental_migration_preserves_existing_business_data():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "subagent_migration_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0013")
            connection.execute(
                text("""INSERT INTO sources
                (id,identity_key,title,kind,origin,created_at)
                VALUES ('old-source','old-identity','原材料','text','fixture',now())""")
            )
            before = dict(connection.execute(text("SELECT * FROM sources")).mappings().one())
            command.upgrade(config, "0014")
            command.upgrade(config, "0014")
            assert (
                dict(connection.execute(text("SELECT * FROM sources")).mappings().one()) == before
            )
            assert (
                connection.execute(text("SELECT count(*) FROM subagent_definitions")).scalar() == 0
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM subagent_definition_revisions")
                ).scalar()
                == 0
            )
            inspector = inspect(connection)
            # 本测试固定验证0014，而不是与未来迁移后的ORM混比；head合同另有测试。
            assert {
                col["name"] for col in inspector.get_columns("subagent_definitions", schema=schema)
            } == {
                "id",
                "name",
                "display_name",
                "description",
                "system_prompt",
                "tools",
                "skills",
                "revision",
                "authorization_epoch",
                "enabled",
                "deleted",
                "created_at",
            }
            assert {
                col["name"]
                for col in inspector.get_columns("subagent_definition_revisions", schema=schema)
            } == {
                "definition_id",
                "revision",
                "content",
                "created_at",
            }
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
