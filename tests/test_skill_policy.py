"""技能用户开关持久化及撤回；不通过UI绕过部署兼容性。"""

import json

from test_api_worker import client as _client
from test_api_worker import services as _services

from knowledgeops_agent.harness.runtime.resources import ResourceService, load_skills
from knowledgeops_agent.harness.tools.policy import ToolPolicyService

client = _client
services = _services


def test_switch_persists_and_reenable_does_not_revive_old_snapshot(client, services):
    service = ToolPolicyService(services.sessions)
    original = {"skills": load_skills(), "tool_policy": service.get()}
    path = "/skills/guided-learning/SKILL.md"
    resources = ResourceService(services.sessions)
    assert resources.skill_available(original, path)
    response = client.patch(
        "/api/tools/policy",
        json={
            "expected_revision": 0,
            "skill_enabled": {"guided-learning": False},
        },
    )
    assert response.status_code == 200, response.text
    rows = client.get("/api/skills").json()
    skill = next(row for row in rows if row["name"] == "guided-learning")
    assert skill["available"] is True and skill["enabled"] is False
    assert not any(row["resource"] == path for row in load_skills(service.get()))
    assert not resources.skill_available(original, path)
    assert (
        client.patch(
            "/api/tools/policy",
            json={
                "expected_revision": 0,
                "skill_enabled": {"guided-learning": True},
            },
        ).status_code
        == 409
    )
    enabled = service.update(expected_revision=1, skill_enabled={"guided-learning": True})
    assert not resources.skill_available(original, path)
    assert resources.skill_available({"skills": load_skills(enabled), "tool_policy": enabled}, path)
    assert service.history()[-1]["skill_enabled"]["guided-learning"] is True


def test_deployment_block_cannot_be_enabled_and_unknown_packages_fail(
    client, tmp_path, monkeypatch
):
    directory = tmp_path / "skills"
    (directory / "unavailable").mkdir(parents=True)
    (directory / "unavailable/SKILL.md").write_text(
        "---\nname: unavailable\ndescription: requires external service\n---\ntext"
    )
    (directory / "availability.json").write_text(
        json.dumps(
            {
                "version": 1,
                "disabled": {"unavailable": "requires external service"},
            }
        )
    )
    monkeypatch.setenv("SKILLS_DIRECTORY", str(directory))
    for name in ["unavailable", "missing"]:
        assert (
            client.patch(
                "/api/tools/policy",
                json={
                    "expected_revision": 0,
                    "skill_enabled": {name: True},
                },
            ).status_code
            == 409
        )
    assert load_skills() == []


def test_user_disabled_package_resources_are_not_loaded(tmp_path, monkeypatch):
    directory = tmp_path / "skills"
    (directory / "example").mkdir(parents=True)
    (directory / "example/SKILL.md").write_text(
        "---\nname: example\ndescription: disabled example\n---\ntext"
    )
    (directory / "example/large.bin").write_bytes(b"x" * (1024 * 1024 + 1))
    monkeypatch.setenv("SKILLS_DIRECTORY", str(directory))
    # 关闭的包不会消耗根运行的资源配额，也不因无用附属文件过大阻断冻结。
    assert load_skills({"skill_enabled": {"example": False}}) == []
