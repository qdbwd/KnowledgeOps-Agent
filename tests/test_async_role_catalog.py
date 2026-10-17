"""仅启用异步委派时也必须向主模型说明可选角色，不要求先调用错误工具试探。"""

from langgraph.checkpoint.memory import InMemorySaver
from test_api_worker import services as _services
from test_async_run_graph import configure

from knowledgeops_agent.harness.runtime.agent import build_agent
from knowledgeops_agent.harness.runtime.state import RunContext
from knowledgeops_agent.harness.tools.policy import ToolPolicyService

services = _services


def test_async_start_description_includes_configured_roles_when_sync_task_is_disabled(services):
    root = configure(services, role_tools=["read_source"])
    policy = ToolPolicyService(services.sessions)
    policy.update(expected_revision=policy.get()["revision"], enabled={"middleware:task": False})
    services.jobs.claim("catalog", run_id=root["id"], generation=1)
    context = RunContext(root["id"], root["id"], "catalog", services.jobs, services.knowledge,
                         services.sources, services.gateway)
    graph = build_agent(context, checkpointer=InMemorySaver())
    tools = graph.nodes["tools"].bound.tools_by_name
    assert "task" not in tools
    description = tools["start_async_task"].description
    assert "offline-reader" in description and "独立核对一个问题" in description
    assert "read_source" in description
