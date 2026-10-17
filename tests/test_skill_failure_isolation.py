"""可选技能失败不阻断普通图；损坏包及启停配置都不能扩大权限。"""

import json

import pytest
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.harness.runtime.resources import ResourceService, load_skills, skill_catalog

runtime_services = _runtime_services


def create_package(root, name):
    path = root / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: useful {name}\n---\nBody")
    return path


@pytest.mark.parametrize("failure", ["yaml", "large", "symlink"])
def test_invalid_package_has_no_partial_resources_and_does_not_hide_good_package(
    tmp_path, monkeypatch, failure,
):
    root = tmp_path / "skills"
    create_package(root, "good")
    broken = create_package(root, "broken")
    (broken / "a.py").write_text("raise RuntimeError('never execute when scanning')")
    if failure == "yaml":
        (broken / "SKILL.md").write_text("---\nname: [broken\n---")
    elif failure == "large":
        (broken / "z.bin").write_bytes(b"x" * (1024 * 1024 + 1))
    else:
        (broken / "z-link").symlink_to(tmp_path / "outside")
    monkeypatch.setenv("SKILLS_DIRECTORY", str(root))
    assert [r["resource"] for r in load_skills()] == ["/skills/good/SKILL.md"]
    catalog = {row["name"]: row for row in skill_catalog()}
    assert catalog["good"]["enabled"] is True
    assert catalog["broken"]["enabled"] is False
    assert catalog["broken"]["availability_status"] == "invalid"
    assert catalog["broken"]["unavailable_reason"]


def test_global_policy_failure_is_closed_but_ordinary_agent_still_responds(
    runtime_services, tmp_path, monkeypatch,
):
    root = tmp_path / "skills"
    create_package(root, "good")
    (root / "availability.json").write_text("{invalid")
    monkeypatch.setenv("SKILLS_DIRECTORY", str(root))
    module, context, run = setup_run(runtime_services)

    class Provider:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            yield AIMessageChunk(content="普通任务仍可以处理。")

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    result = graph.invoke(
        {"messages": module.initial_messages(context)},
        {"configurable": {"thread_id": run["checkpoint_id"]}}, context=context,
    )
    assert result["messages"][-1].content == "普通任务仍可以处理。"
    assert context.resources["skills"] == []
    assert context.resources["skill_diagnostics"][0]["availability_status"] == "config_error"
    assert all(not row["enabled"] for row in skill_catalog())
    # 配置修好只影响新快照，旧运行不静默增补技能。
    (root / "availability.json").write_text(json.dumps({"version": 1, "disabled": {}}))
    assert ResourceService(context.jobs.sessions).ensure(context)["skills"] == []
