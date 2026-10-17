"""补问校验和撤回的确定性边界；不依赖外部服务或收费模型。"""

from types import SimpleNamespace

import pytest
from mcp.types import (
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitRequestURLParams,
    InputRequiredResult,
)
from sqlalchemy import select
from test_async_run_scheduling import family
from test_jobs import jobs as _jobs

from knowledgeops_agent.app.runs.children import ChildRuns
from knowledgeops_agent.app.runs.questions import QuestionAnswer, RunQuestions
from knowledgeops_agent.harness.mcp.input import MCPInputUnsupported, public_input, validate_answer
from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.persistence.models import MCPCall, MCPContinuation

jobs = _jobs


def form():
    return public_input(InputRequiredResult(input_requests={"q": ElicitRequest(params=ElicitRequestFormParams(
        message="选择条数", requested_schema={"type": "object", "properties": {
            "count": {"type": "integer", "minimum": 1, "maximum": 5},
        }, "required": ["count"]},
    ))}), "服务")


@pytest.mark.parametrize("content", [{"count": "2"}, {"count": True}, {"count": 0}, {"count": 6}, {"count": 2, "extra": 1}, None])
def test_form_rejects_values_outside_saved_schema(content):
    with pytest.raises(ValueError):
        validate_answer(form(), "mcp", {"q": {"action": "accept", "content": content}})


def test_form_accepts_typed_values_and_url_never_accepts_credentials():
    assert validate_answer(form(), "mcp", {"q": {"action": "accept", "content": {"count": 2}}})["q"]["content"] == {"count": 2}
    assert validate_answer(form(), "decline", "") == {"q": {"action": "decline"}}
    request = public_input(InputRequiredResult(input_requests={"url": ElicitRequest(params=ElicitRequestURLParams(
        message="请到服务页面确认", url="https://example.com/verify",
    ))}), "服务")
    assert validate_answer(request, "mcp", {"url": {"action": "accept"}}) == {"url": {"action": "accept"}}
    with pytest.raises(ValueError, match="凭据"):
        validate_answer(request, "mcp", {"url": {"action": "accept", "content": {"password": "not-a-secret"}}})
    with pytest.raises(MCPInputUnsupported):
        public_input(InputRequiredResult(input_requests={}, request_state="background-only"), "服务")


def test_child_instruction_invalidates_only_its_own_question_and_queues_actual_run(jobs):
    _, _, service, sessions, _ = jobs
    root, first, second = family(service)
    questions = RunQuestions(service)
    saved = []
    for child, worker in [(first, "first"), (second, "second")]:
        service.claim(worker, run_id=child["id"], generation=1)
        context = SimpleNamespace(jobs=service, root_id=root["id"], run_id=child["id"], worker_id=worker)
        saved.append(questions.ensure(context, "ask", {"question": "还需说明吗？"}))
        questions.settle_wait(context, {})
    ChildRuns(service).wait(root["id"], "main", [first["id"], second["id"]])
    binding = MCPRegistry(sessions).create("输入状态测试", {"transport": "stdio", "command": "python", "args": []})
    # 使用真实持久表检查旧 MCP 续接和问题必须一起撤回。
    with sessions.begin() as session:
        call = MCPCall(run_id=first["id"], connection_id=binding["id"], connection_revision=1,
                       tool_call_id="ask", tool_name="probe", input_hash="test-hash", request={},
                       authority_epoch=0, status="waiting_input")
        session.add(call)
        session.flush()
        session.add(MCPContinuation(mcp_call_id=call.id, question_id=saved[0]["id"], round=1,
                                    request_state={"state": "test-state"}))
    updated = ChildRuns(service).update(root["id"], first["id"], "取消旧问题，继续核对已有资料", request_key="new", origin="human")
    assert updated["status"] == "queued"
    assert service.get_run(second["id"])["status"] == "waiting_input"
    assert service.get_run(root["id"])["status"] == "waiting_children"
    rows = {row["id"]: row for row in questions.list(root["id"])}
    assert rows[saved[0]["id"]]["status"] == "stale" and rows[saved[1]["id"]]["status"] == "pending"
    with pytest.raises(ValueError):
        questions.answer(root["id"], saved[0]["id"], QuestionAnswer(expected_revision=1, kind="text", value="旧答案"))
    with sessions() as session:
        assert session.scalar(select(MCPCall)).status == "denied"
        assert session.scalar(select(MCPContinuation)).status == "invalidated"
