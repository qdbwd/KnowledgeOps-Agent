"""可编辑工具组必须接入真实策略、角色修订和旧任务撤回，不能只更新展示目录。"""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select
from test_api_worker import client as _client
from test_api_worker import services as _services
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.harness.subagents.registry import SubagentRegistry
from knowledgeops_agent.harness.tools.policy import ToolPolicyService

client = _client
services = _services
runtime_services = _runtime_services


def create_group(client, tools=None):
    response = client.post(
        "/api/tools/groups",
        json={
            "name": "资料核对",
            "description": "读取正文并核对知识版本",
            "tool_ids": tools if tools is not None else ["builtin:read_source"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_role(services, group, **changes):
    return SubagentRegistry(services.sessions).create(
        **{
            "name": "fact-checker",
            "display_name": "资料核对者",
            "description": "只读核对",
            "system_prompt": "只读材料并核对来源。",
            "tool_groups": [group["id"]],
            "tools": [],
            "skills": [],
            "enabled": True,
            **changes,
        }
    )


def preview(client, group, **changes):
    response = client.post(
        f"/api/tools/groups/{group['id']}/preview",
        json={
            "expected_revision": group["revision"],
            **changes,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def save(client, group, **changes):
    impact = preview(client, group, **changes)
    response = client.patch(
        f"/api/tools/groups/{group['id']}",
        json={
            "expected_revision": group["revision"],
            "preview_token": impact["preview_token"],
            **changes,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_groups_are_read_only_catalog_with_stable_ids_and_separate_mcp(client, services):
    from knowledgeops_agent.persistence.models import ToolGroup

    response = client.get("/api/tools/groups")
    assert response.status_code == 200
    groups = {row["id"]: row for row in response.json()}
    assert "builtin:read_source" in groups["materials_read"]["tool_ids"]
    assert groups["materials_read"]["description"]
    assert "mcp" not in groups
    with services.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ToolGroup)) == 0
    catalog = client.get("/api/tools/catalog").json()
    source = next(tool for tool in catalog["tools"] if tool["name"] == "read_source")
    assert source["display_name"] == "读取材料正文"
    assert source["id"] == "builtin:read_source"
    assert not services.gateway.ledger.records()


def test_group_growth_updates_new_config_without_expanding_frozen_bindings(client, services):
    group = create_group(client)
    roles = SubagentRegistry(services.sessions)
    role = create_role(services, group)
    policies = ToolPolicyService(services.sessions)
    frozen = policies.update(expected_revision=0, main_tools=[], main_tool_groups=[group["id"]])
    impact = preview(client, group, tool_ids=["builtin:read_source", "builtin:read_knowledge"])
    assert {row["id"] for row in impact["affected"]} == {"main", role["id"]}
    assert all(row["added"] == ["builtin:read_knowledge"] for row in impact["affected"])
    save(client, group, tool_ids=["builtin:read_source", "builtin:read_knowledge"])
    current = policies.get()
    assert current["revision"] == frozen["revision"] + 1
    assert set(current["resolved_main_tools"]) == {"builtin:read_source", "builtin:read_knowledge"}
    assert policies.allowed_ids(frozen, is_root=True) == {"builtin:read_source"}
    assert roles.validate_binding(role) == role
    assert roles.get(role["id"])["revision"] == role["revision"] + 1
    assert roles.get(role["id"])["authorization_epoch"] == role["authorization_epoch"]
    assert roles.history(role["id"])[0] == role
    assert policies.history()[1] == frozen
    options = client.get("/api/subagents/options").json()
    assert any(row["id"] == group["id"] for row in options["tool_groups"])


def test_removal_revokes_old_roles_and_main_even_after_readding(client, services):
    group = create_group(client, ["builtin:read_source", "builtin:read_knowledge"])
    role = create_role(services, group)
    roles = SubagentRegistry(services.sessions)
    policies = ToolPolicyService(services.sessions)
    original = policies.update(expected_revision=0, main_tools=[], main_tool_groups=[group["id"]])
    smaller = save(client, group, tool_ids=["builtin:read_knowledge"])
    save(client, smaller, tool_ids=["builtin:read_source", "builtin:read_knowledge"])
    assert "builtin:read_source" not in policies.allowed_ids(original, is_root=True)
    assert "builtin:read_knowledge" in policies.allowed_ids(original, is_root=True)
    assert "builtin:read_source" in policies.allowed_ids(original, is_root=False)
    with pytest.raises(PermissionError, match="撤回"):
        roles.validate_binding(role)


def test_delete_group_preserves_individual_tools_and_never_resurrects_default(client, services):
    group = next(row for row in client.get("/api/tools/groups").json() if row["id"] == "web")
    roles = SubagentRegistry(services.sessions)
    role = create_role(services, group, tools=["read_web"])
    policies = ToolPolicyService(services.sessions)
    policies.update(expected_revision=0, main_tools=["builtin:read_web"], main_tool_groups=["web"])
    impact = preview(client, group, deleted=True)
    response = client.request(
        "DELETE",
        "/api/tools/groups/web",
        json={
            "expected_revision": group["revision"],
            "preview_token": impact["preview_token"],
        },
    )
    assert response.status_code == 200, response.text
    assert policies.get()["resolved_main_tools"] == ["builtin:read_web"]
    assert policies.get()["main_tool_groups"] == []
    assert roles.get(role["id"])["resolved_tools"] == ["read_web"]
    assert roles.get(role["id"])["tool_groups"] == []
    assert "web" not in {row["id"] for row in client.get("/api/tools/groups").json()}
    assert "web" not in {row["id"] for row in client.get("/api/tools/catalog").json()["groups"]}


def test_stale_impact_cannot_overwrite_newly_affected_role(client, services):
    group = create_group(client)
    impact = preview(client, group, tool_ids=[])
    role = create_role(services, group)
    response = client.patch(
        f"/api/tools/groups/{group['id']}",
        json={
            "expected_revision": group["revision"],
            "preview_token": impact["preview_token"],
            "tool_ids": [],
        },
    )
    assert response.status_code == 409
    assert SubagentRegistry(services.sessions).get(role["id"]) == role
    assert client.get("/api/tools/policy").json()["revision"] == 0


def test_mixed_group_keeps_child_ceiling_and_unchanged_effective_rights(client, services):
    group = create_group(client)
    role = create_role(services, group)
    updated = save(
        client, group, tool_ids=["builtin:read_source", "middleware:execute", "middleware:task"]
    )
    registry = SubagentRegistry(services.sessions)
    assert registry.get(role["id"])["resolved_tools"] == ["read_source"]
    assert registry.validate_binding(role) == role
    options = client.get("/api/subagents/options").json()["tool_groups"]
    child_group = next(row for row in options if row["id"] == updated["id"])
    assert child_group["tool_ids"] == ["builtin:read_source"]
    assert child_group["excluded_tool_ids"] == ["middleware:execute", "middleware:task"]
    assert (
        client.patch(
            f"/api/subagents/{role['id']}",
            json={
                "expected_revision": registry.get(role["id"])["revision"],
                "tools": ["execute"],
            },
        ).status_code
        == 422
    )


@pytest.mark.parametrize("tools", [["mcp:server:tool"], ["builtin:missing"]])
def test_group_rejects_unknown_or_mcp_members(client, tools):
    assert (
        client.post(
            "/api/tools/groups",
            json={
                "name": "错误成员",
                "description": "",
                "tool_ids": tools,
            },
        ).status_code
        == 422
    )


def test_two_edits_from_same_preview_have_one_winner(client, services):
    from knowledgeops_agent.harness.tools.groups import ToolGroupService

    group = create_group(client)
    impact = preview(client, group, tool_ids=[])
    service = ToolGroupService(services.sessions)

    def change(_):
        try:
            return service.update(
                group["id"],
                expected_revision=group["revision"],
                preview_token=impact["preview_token"],
                tool_ids=[],
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(change, range(2)))
    assert sum(row is not None for row in results) == 1


def test_role_write_failure_rolls_back_group_policy_and_all_revisions(
    client, services, monkeypatch
):
    from knowledgeops_agent.harness.tools.groups import ToolGroupService

    group = create_group(client)
    role = create_role(services, group)
    policy_service = ToolPolicyService(services.sessions)
    policy = policy_service.update(
        expected_revision=0, main_tools=[], main_tool_groups=[group["id"]]
    )
    impact = preview(client, group, tool_ids=[])

    def fail(*_):
        raise RuntimeError("模拟角色历史写入失败")

    monkeypatch.setattr(SubagentRegistry, "_save", staticmethod(fail))
    with pytest.raises(RuntimeError, match="模拟"):
        ToolGroupService(services.sessions).update(
            group["id"],
            expected_revision=group["revision"],
            preview_token=impact["preview_token"],
            tool_ids=[],
        )
    assert (
        next(row for row in ToolGroupService(services.sessions).list() if row["id"] == group["id"])
        == group
    )
    assert policy_service.get() == policy
    assert SubagentRegistry(services.sessions).get(role["id"]) == role
    assert SubagentRegistry(services.sessions).history(role["id"]) == [role]


def test_group_shrink_after_model_choice_blocks_real_tool_handler(runtime_services):
    """真实图已提出调用后修改组，既不能执行读取，也不能因重新扩组复活旧许可。"""
    from knowledgeops_agent.harness.tools.groups import ToolGroupService

    module, context, run = setup_run(runtime_services)
    group_service = ToolGroupService(context.jobs.sessions)
    group = group_service.create(
        name="图中阅读", description="只读来源", tool_ids=["builtin:read_source"]
    )
    policy = ToolPolicyService(context.jobs.sessions)
    policy.update(expected_revision=0, main_tools=[], main_tool_groups=[group["id"]])
    segment = context.sources.get_source(run["input"]["source_ids"][0])["segments"][0]["id"]

    class Provider:
        def bind_tools(self, tools):
            self.names = {tool.name for tool in tools}
            return self

        def stream(self, messages, **kwargs):
            observations = [row for row in messages if isinstance(row, ToolMessage)]
            if observations:
                assert observations[-1].status == "error"
                assert observations[-1].tool_call_id == "group-read"
                yield AIMessageChunk("该组的读取能力已撤回，没有读取材料。")
                return
            assert self.names == {"read_source"}
            impact = group_service.preview(group["id"], expected_revision=1, tool_ids=[])
            group_service.update(
                group["id"], expected_revision=1, preview_token=impact["preview_token"], tool_ids=[]
            )
            yield AIMessageChunk(
                "",
                tool_call_chunks=[
                    {
                        "name": "read_source",
                        "args": json.dumps({"segment_id": segment}),
                        "id": "group-read",
                        "index": 0,
                    }
                ],
            )

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    graph.invoke(
        {"messages": module.initial_messages(context)},
        {"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    assert context.sources.evidence_catalog(context.root_id) == []
    assert context.resources["tool_policy"]["resolved_main_tools"] == ["builtin:read_source"]
