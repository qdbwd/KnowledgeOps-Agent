"""D01使用真实配置目录、task工具和恢复链路；只有供应商传输为离线替身。"""

import json
import re
from contextlib import contextmanager
from copy import deepcopy

from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_api_worker import services as _services
from test_runtime import DraftProvider

from evaluation.checks import evaluate
from evaluation.optimization import delegation_case, result_data

services = _services


class ConfiguredReadersProvider(DraftProvider):
    """只解析实际任务和工具回包中的ID，不预置来源、证据或子运行UUID。"""

    def __init__(self):
        pass

    def stream(self, messages, **kwargs):
        task = next(m.content for m in messages if isinstance(m, HumanMessage))
        observations = [m for m in messages if isinstance(m, ToolMessage)]
        if "ROOT_ONLY_AUDIT" in task:
            if observations:
                reports = [json.loads(m.content) for m in observations if m.name == "task"]
                yield AIMessageChunk(
                    content="两份研究结论：" + " ".join(row["answer"] for row in reports)
                )
                return
            pairs = re.findall(r"(retry-reader|version-reader).*?source_id=([a-f0-9-]+)", task)
            if len(pairs) != 2:
                yield AIMessageChunk(content="本任务未提供两项可配置角色与材料映射。")
                return
            yield AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": "task",
                        "args": json.dumps(
                            {
                                "subagent_type": role,
                                "description": f"只核对分配材料 source_id={source_id}，返回结论与引用。",
                            }
                        ),
                        "id": f"read-with-{role}",
                        "index": index,
                    }
                    for index, (role, source_id) in enumerate(pairs)
                ],
            )
            return
        assert "ROOT_ONLY_AUDIT" not in str(messages)
        if not observations:
            match = re.search(r"source_id=([a-f0-9-]+)", task)
            assert match is not None
            name, args = "inspect_material", {"source_id": match.group(1)}
        elif observations[-1].name == "inspect_material":
            material = json.loads(observations[-1].content)
            name, args = "read_source", {"segment_id": material["segments"][0]["id"]}
        else:
            evidence = json.loads(observations[-1].content)
            yield AIMessageChunk(content="核对结论依据：" + evidence["citation"])
            return
        yield AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": name,
                    "args": json.dumps(args),
                    "id": f"child-{name}",
                    "index": 0,
                }
            ],
        )


def test_configured_d01_loses_receipt_once_then_reuses_completed_children(services, monkeypatch):
    import knowledgeops_agent.app.runs.worker as worker

    saver = InMemorySaver()

    @contextmanager
    def isolated_checkpointer(_):
        yield saver

    monkeypatch.setattr(worker, "open_checkpointer", isolated_checkpointer)
    services.gateway.enabled = True
    services.gateway.client = ConfiguredReadersProvider()
    turns, checks, interventions = delegation_case(services)
    assert len(turns) == 2
    failed, completed = turns
    assert failed["run"]["status"] == "failed"
    assert "ConnectionError" in failed["run"]["stop_reason"]
    assert completed["run"]["status"] == "completed"
    phase = interventions[0]
    assert phase["fault_injected"] is True
    assert len(phase["call_ids"]) == len(set(phase["call_ids"])) == 2
    assert {row["name"] for row in phase["configured_roles"]} == {"retry-reader", "version-reader"}
    for role in phase["configured_roles"]:
        assert set(role["tools"]) == {"inspect_material", "read_source", "read_tool_result"}
        assert role["skills"] == ["evidence-check"]
        assert role["enabled"] is True
    children = [row["run"] for row in completed["children"]]
    assert {row["id"] for row in children} == set(phase["child_ids"])
    assert {row["input"]["subagent"]["name"] for row in children} == {
        "retry-reader",
        "version-reader",
    }
    records = [row for row in completed["models"] if row["run_id"] != completed["run"]["id"]]
    assert phase["child_requests"] == records
    assert phase["child_request_ids"] == [row["id"] for row in records]
    assert len(records) == 6
    assert len(completed["models"]) == len(failed["models"]) + 1
    assert all(checks.values()), checks
    result = result_data(
        services,
        "D01",
        "配置角色故障恢复",
        "delegation",
        1,
        turns,
        checks,
        interventions,
        0,
    )
    assert all(result["hard_checks"].values()), result["hard_checks"]
    assert all(result["checks"].values()), result["checks"]


def test_business_child_recursion_check_rejects_standard_task(services):
    root = {
        "id": "fixture-root",
        "status": "completed",
        "outcome": {"answer": ""},
        "budgets": {"model_requests": 5},
    }
    result = {
        "run": root,
        "drafts": [],
        "agent_documents": [],
        "documents": [],
        "fixture": {"before": []},
        "events": [],
        "models": [],
        "phases": {},
        "children": [{"events": [{"kind": "tool_started", "data": {"name": "task"}}]}],
    }
    _, hard, _ = evaluate(services, {"scenario": "insufficient"}, result)
    assert hard["children_read_only"] is False


def test_restricted_tool_case_keeps_main_and_grouped_child_requests_separate(services, monkeypatch):
    """T01复用D01的真实图/恢复链；严格检查最终请求，不靠模型是否碰巧调用来判权限。"""
    import knowledgeops_agent.app.runs.worker as worker
    from evaluation import optimization

    saver = InMemorySaver()

    @contextmanager
    def isolated_checkpointer(_):
        yield saver

    monkeypatch.setattr(worker, "open_checkpointer", isolated_checkpointer)
    services.gateway.enabled = True
    services.gateway.client = ConfiguredReadersProvider()
    turns, checks, interventions = optimization.tool_policy_case(services)
    assert optimization.schedule("tools") == [("T01", "restricted", 1)]
    assert all(checks.values()), checks
    assert turns[-1]["run"]["budgets"]["model_requests"] == 12
    for role in interventions[0]["configured_roles"]:
        assert role["tools"] == ["read_tool_result"]
        assert role["tool_groups"] == ["materials_read"]
        assert set(role["resolved_tools"]) == {
            "read_tool_result",
            "list_materials",
            "list_evidence",
            "inspect_material",
            "read_source",
        }
    result = result_data(
        services,
        "T01",
        "受限工具的配置委派",
        "restricted",
        1,
        turns,
        checks,
        interventions,
        0,
    )
    assert all(result["hard_checks"].values()), result["hard_checks"]
    # 伪造一项越界Schema和一项实际越界执行，检查器必须分别拒绝，不能只验正例。
    tampered = deepcopy(turns[-1])
    root_id = tampered["run"]["id"]
    request = next(
        row["request"]
        for row in tampered["models"]
        if row["run_id"] == root_id and row["request"]["tools"]
    )
    request["tools"].append({"type": "function", "function": {"name": "publish_changes"}})
    tampered["children"][0]["events"].append({"kind": "tool_started", "data": {"name": "task"}})
    invalid = optimization.tool_policy_checks(tampered)
    assert invalid["main_request_tool_set"] is False
    assert invalid["executed_tools_within_policy"] is False
