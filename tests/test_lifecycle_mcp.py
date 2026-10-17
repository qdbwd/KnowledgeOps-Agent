"""聊天删除会使别的worker池失效；不需要再有一次用户工具调用才关闭连接。"""

import pytest
from test_mcp_session_business import content, register
from test_mcp_session_protocol import assert_process_exited
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.harness.mcp.calls import MCPCalls
from knowledgeops_agent.persistence.models import Conversation

runtime_services = _runtime_services


def test_deleting_chat_closes_idle_connection_and_forbids_reborrow(runtime_services):
    _, context, root = setup_run(runtime_services)
    binding = register(context)
    first = MCPCalls(context).invoke(binding, "counter", {}, call_id="lifecycle-connection")
    pid = content(first)["pid"]
    context.jobs.finish(root["id"], context.worker_id)
    with context.jobs.sessions.begin() as session:
        session.get(Conversation, root["conversation_id"]).deletion_state = "deleting"
    assert_process_exited(pid)

    async def reborrow():
        async with context.jobs.mcp_sessions.lease(f"conversation:{root['conversation_id']}:main", binding):
            raise AssertionError("deleted scope must not reconnect")

    with pytest.raises(PermissionError):
        context.jobs.mcp_sessions.run(reborrow)
