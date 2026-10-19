"""真实隔离PG验证会话配置与命令规则，不从用户记忆推断授权。"""

import pytest

from knowledgeops_agent.app.runs.service import RunService
from knowledgeops_agent.harness.permissions.settings import ExecutionSettings


def test_conversation_defaults_revision_and_no_cross_conversation_leak(business_sessions):
    jobs = RunService(business_sessions)
    first = jobs.create_conversation("one")["id"]
    second = jobs.create_conversation("two")["id"]
    settings = ExecutionSettings(business_sessions)
    assert settings.get(first) == {
        "permission_mode": "ask",
        "reasoning_effort": "default",
        "revision": 0,
    }
    value = settings.update(
        first, expected_revision=0, permission_mode="auto_review", reasoning_effort="high"
    )
    assert value["revision"] == 1
    assert settings.get(second)["permission_mode"] == "ask"
    with pytest.raises(ValueError, match="修订"):
        settings.update(first, expected_revision=0, permission_mode="ask")
    with pytest.raises(ValueError):
        settings.update(first, expected_revision=1, permission_mode="full_access")


def test_persisted_command_rule_scope_revision_and_deletion(business_sessions):
    jobs = RunService(business_sessions)
    first = jobs.create_conversation("one")["id"]
    second = jobs.create_conversation("two")["id"]
    service = ExecutionSettings(business_sessions)
    spec = {"argv": ["git", "status"], "match": "prefix", "cwd": "/data/ws", "effect": "allow"}
    local = service.create_rule(workspace="/data/ws", conversation_id=first, **spec)
    common = service.create_rule(workspace="/data/ws", conversation_id=None, **spec)
    assert {r["id"] for r in service.rules("/data/ws", first)} == {local["id"], common["id"]}
    assert [r["id"] for r in service.rules("/data/ws", second)] == [common["id"]]
    assert not service.rules("/data/other", first)
    changed = service.update_rule(
        local["id"], workspace="/data/ws", expected_revision=1, enabled=False
    )
    assert changed["revision"] == 2
    with pytest.raises(ValueError, match="修订"):
        service.update_rule(local["id"], workspace="/data/ws", expected_revision=1, enabled=True)
    service.delete_rule(common["id"], workspace="/data/ws", expected_revision=1)
    assert not service.rules("/data/ws", second)
    assert not service.rules("/data/ws", first)[0]["enabled"]


def test_cannot_create_broad_interpreter_allow_rule(business_sessions):
    service = ExecutionSettings(business_sessions)
    with pytest.raises(ValueError):
        service.create_rule(
            workspace="/data/ws",
            conversation_id=None,
            argv=["python"],
            match="prefix",
            cwd="/data/ws",
            effect="allow",
        )
    with pytest.raises(ValueError):
        service.create_rule(
            workspace="/data/ws",
            conversation_id=None,
            argv=["git"],
            match="prefix",
            cwd="/etc",
            effect="allow",
        )
