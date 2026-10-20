"""真实 Agent 图运行文件闭环；仅模型选工具的响应是可重复的离线输入。"""

import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fixtures.execution_provider import ExecutionProvider
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_api_worker import services as _services
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.app.runs.service import RunStopped
from knowledgeops_agent.harness.permissions.settings import ExecutionSettings
from knowledgeops_agent.harness.tools.catalog import tool_options
from knowledgeops_agent.harness.tools.policy import ToolPolicyService

runtime_services = _runtime_services
services = _services


def enable(sessions):
    service = ToolPolicyService(sessions)
    return service.update(
        expected_revision=service.get()["revision"],
        enabled={row["id"]: True for row in tool_options() if row["condition"] == "workspace"},
    )


def test_real_graph_writes_edits_executes_presents_and_imports(runtime_services, tmp_path):
    module, context, run = setup_run(runtime_services)
    ExecutionSettings(context.jobs.sessions).update(
        run["conversation_id"], expected_revision=0, permission_mode="auto_review"
    )
    context.workspace_root = tmp_path / "workspace"
    enable(context.jobs.sessions)

    class Provider:
        invoke = ExecutionProvider.invoke

        def bind_tools(self, tools):
            assert {
                "read_file",
                "write_file",
                "edit_file",
                "execute",
                "present_files",
                "import_file",
            } <= {tool.name for tool in tools}
            return self

        def stream(self, messages, **kwargs):
            observations = [m for m in messages if isinstance(m, ToolMessage)]
            steps = [
                ("write_file", {"file_path": "/outputs/note.md", "content": "# 初稿\n本地材料"}),
                ("read_file", {"file_path": "/outputs/note.md"}),
                (
                    "edit_file",
                    {"file_path": "/outputs/note.md", "old_string": "初稿", "new_string": "定稿"},
                ),
                ("execute", {"command": "printf '脚本产物' > outputs/result.txt", "timeout": 5}),
                ("present_files", {"file_paths": ["/outputs/note.md", "/outputs/result.txt"]}),
                ("import_file", {"file_path": "/outputs/note.md"}),
            ]
            if observations:
                assert observations[-1].status != "error", observations[-1].content
            if len(observations) == len(steps):
                source = json.loads(observations[-1].content)
                name, args = "read_source", {"segment_id": source["segments"][0]["id"]}
            elif len(observations) > len(steps):
                evidence = json.loads(observations[-1].content)
                delivery = json.loads(observations[4].content)[0]
                yield AIMessageChunk(
                    f"已生成[笔记]({delivery['url']})。"
                    + evidence["citation"]
                )
                return
            else:
                name, args = steps[len(observations)]
            yield AIMessageChunk(
                "",
                tool_call_chunks=[
                    {
                        "name": name,
                        "args": json.dumps(args),
                        "id": f"file-{len(observations)}",
                        "index": 0,
                    }
                ],
            )

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    result = graph.invoke(
        {"messages": module.initial_messages(context)},
        {"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    assert (context.workspace_root / "outputs/note.md").read_text() == "# 定稿\n本地材料"
    assert (context.workspace_root / "outputs/result.txt").read_text() == "脚本产物"
    assert "/api/deliveries/" in result["messages"][-1].text
    assert context.jobs.get_run(run["id"])["tool_calls"] == 7
    assert context.sources.evidence_catalog(context.root_id)


def test_old_snapshot_does_not_gain_workspace_and_live_disable_hides_tools(
    runtime_services, tmp_path
):
    from knowledgeops_agent.harness.tools.access import unavailable_tools

    module, context, _ = setup_run(runtime_services)
    module.build_agent(context, checkpointer=InMemorySaver())
    context.workspace_root = tmp_path
    enable(context.jobs.sessions)
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    assert "execute" not in graph.nodes["tools"].bound.tools_by_name
    assert "workspace" not in context.resources
    assert "write_file" in unavailable_tools(context)


def test_graph_executes_frozen_skill_script_and_records_resource_use(
    runtime_services, tmp_path, monkeypatch
):
    """从部署包到冻结清单、真实 SDK 工具、子进程和回执，只有模型响应是替身。"""
    module, context, run = setup_run(runtime_services)
    ExecutionSettings(context.jobs.sessions).update(
        run["conversation_id"], expected_revision=0, permission_mode="auto_review"
    )
    context.workspace_root = tmp_path / "workspace"
    directory = tmp_path / "skills" / "script-example"
    (directory / "scripts").mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: script-example\ndescription: execute a local script\n---\nUse scripts/run.py"
    )
    script = directory / "scripts/run.py"
    script.write_text("print('frozen-script-output')")
    monkeypatch.setenv("SKILLS_DIRECTORY", str(directory.parent))
    enable(context.jobs.sessions)

    class Provider:
        invoke = ExecutionProvider.invoke

        def bind_tools(self, tools):
            assert "execute" in {tool.name for tool in tools}
            return self

        def stream(self, messages, **kwargs):
            observations = [m for m in messages if isinstance(m, ToolMessage)]
            if observations and observations[-1].name == "execute":
                assert "frozen-script-output" in str(observations[-1].content)
                yield AIMessageChunk("脚本运行完成。")
                return
            if not observations:
                yield AIMessageChunk("", tool_call_chunks=[{
                    "name": "read_file", "id": "read-skill", "index": 0,
                    "args": json.dumps({"file_path": "/skills/script-example/SKILL.md"}),
                }])
                return
            yield AIMessageChunk(
                "",
                tool_call_chunks=[
                    {
                        "name": "execute",
                        "id": "skill-script",
                        "index": 0,
                        "args": json.dumps(
                            {
                                "command": 'python "$SKILLS_DIR/script-example/scripts/run.py"',
                                "timeout": 5,
                            }
                        ),
                    }
                ],
            )

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    script.write_text("raise RuntimeError('must not use new deployment')")
    result = graph.invoke(
        {"messages": module.initial_messages(context)},
        {"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    assert "脚本运行完成" in result["messages"][-1].text
    from sqlalchemy import select

    from knowledgeops_agent.persistence.models import RunEvent

    with context.jobs.sessions() as session:
        events = list(
            session.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == context.run_id, RunEvent.kind == "skill_materialized"
                )
            )
        )
    assert {event.data["path"] for event in events} == {
        "/skills/script-example/SKILL.md",
        "/skills/script-example/scripts/run.py",
    }


def test_real_graph_cancellation_reaches_running_shell(runtime_services, tmp_path):
    """模拟 Web 的取消写入 PG，验证真实图/SDK 调用中的命令会及时退出。"""
    module, context, run = setup_run(runtime_services)
    ExecutionSettings(context.jobs.sessions).update(
        run["conversation_id"], expected_revision=0, permission_mode="auto_review"
    )
    context.workspace_root = tmp_path / "workspace"
    context.workspace_execute_timeout = 15
    enable(context.jobs.sessions)

    class Provider:
        invoke = ExecutionProvider.invoke

        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            yield AIMessageChunk(
                "",
                tool_call_chunks=[
                    {
                        "name": "execute",
                        "args": json.dumps(
                            {"command": "touch work/started; sleep 30; touch outputs/too-late"}
                        ),
                        "id": "cancel-shell",
                        "index": 0,
                    }
                ],
            )

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            graph.invoke,
            {"messages": module.initial_messages(context)},
            {"configurable": {"thread_id": run["checkpoint_id"]}},
            context=context,
        )
        deadline = time.monotonic() + 15
        while not (context.workspace_root / "work/started").exists():
            if future.done():
                future.result()  # 先暴露图内错误，避免被笼统的等待超时掩盖。
                pytest.fail("graph ended before executing the command")
            assert time.monotonic() < deadline, "shell did not start"
            time.sleep(0.05)
        context.jobs.cancel(run["id"])
        with pytest.raises(RunStopped, match="cancelled"):
            future.result(timeout=5)
    assert context.jobs.get_run(run["id"])["cancel_requested"]
    assert not (context.workspace_root / "outputs/too-late").exists()


def test_workspace_http_download_and_tool_preview(services, tmp_path):
    from fastapi.testclient import TestClient

    from knowledgeops_agent.app.application import create_app

    services.settings.workspace_root = tmp_path
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs/result.md").write_text("已完成")
    enable(services.sessions)
    with TestClient(create_app(services=services, publish_jobs=False)) as client:
        response = client.get("/api/workspace/download", params={"path": "outputs/result.md"})
        assert response.status_code == 200 and response.text == "已完成"
        assert response.headers["content-disposition"].startswith("attachment")
        assert (
            client.get("/api/workspace/download", params={"path": "outputs/../../.env"}).status_code
            == 409
        )
        assert (
            client.get(
                "/api/workspace/download", params={"path": "knowledge/versions/a.md"}
            ).status_code
            == 403
        )
        current = {row["name"]: row for row in client.get("/api/tools/effective").json()["tools"]}
        assert current["execute"]["available"]
        service = ToolPolicyService(services.sessions)
        service.update(
            expected_revision=service.get()["revision"], enabled={"middleware:execute": False}
        )
        current = {row["name"]: row for row in client.get("/api/tools/effective").json()["tools"]}
        assert not current["execute"]["available"]
