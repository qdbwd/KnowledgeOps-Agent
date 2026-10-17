"""现代 MCP 补问的持久协议桥；不透明状态不进入模型消息、事件或公开接口。

复用公开 InputRequiredResult/ElicitResult 和 LangGraph interrupt。SDK 默认图恢复
会重发首轮，本模块把每轮状态落 PG；重入只重放中断位置，不重做已有远端工作。
"""

import json
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from langgraph.types import interrupt
from mcp.types import ElicitRequest, ElicitResult, InputRequiredResult
from sqlalchemy import select

from knowledgeops_agent.app.runs.questions import question_view
from knowledgeops_agent.persistence.models import (
    MCPCall,
    MCPContinuation,
    Run,
    RunControl,
    RunQuestion,
)


class MCPInputUnsupported(ValueError):
    """服务要求超出客户端支持范围；不能伪装为真人拒绝或重新发首轮。"""


def public_input(result: InputRequiredResult, service_name: str) -> dict:
    """只展示普通表单/URL；抽样调用、roots 与无问题后台续接不在本轮范围。"""
    if not result.input_requests or len(result.input_requests) > 8:
        raise MCPInputUnsupported("MCP当前仅支持每轮1至8项真人表单或URL补问，不支持无问题后台续接")
    requests = []
    for key, request in result.input_requests.items():
        if not isinstance(request, ElicitRequest):
            raise MCPInputUnsupported("MCP仅支持真人补问；不能借服务输入请求调用模型或开放文件根目录")
        data = request.params.model_dump(mode="json", exclude_none=True)
        if data.get("mode") == "url":
            parsed = urlsplit(data["url"])
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise MCPInputUnsupported("MCP补问URL必须为不含内嵌凭据的HTTP或HTTPS地址")
        else:
            schema = data["requested_schema"]
            if schema.get("type") != "object" or len(schema.get("properties", {})) > 24:
                raise MCPInputUnsupported("MCP表单须为最多24个普通字段的对象")
            if '"$ref"' in json.dumps(schema):
                raise MCPInputUnsupported("MCP表单不支持远端或引用式Schema")
            for field in schema.get("properties", {}).values():
                if field.get("type") not in {"string", "integer", "number", "boolean", "array"}:
                    raise MCPInputUnsupported("MCP表单只支持文本、数字、布尔及字符串数组字段")
                if field.get("type") == "array" and field.get("items", {}).get("type") != "string":
                    raise MCPInputUnsupported("MCP表单数组只支持字符串值")
            Draft202012Validator.check_schema(schema)
        requests.append({"id": key, **data})
    body = {"question": f"{service_name} 需要补充信息", "context": "这是外部服务的请求，回答不增加工具权限。",
            "requests": requests}
    if len(json.dumps(body, ensure_ascii=False)) > 32000:
        raise MCPInputUnsupported("MCP补问内容超过可展示上限，已停止续接")
    return body


def validate_answer(request: dict, kind: str, value) -> dict:
    """验证每一个服务请求的精确回答；拒绝将普通聊天或授权动作当作协议答案。"""
    entries = {item["id"]: item for item in request["requests"]}
    if kind == "decline":
        return {key: {"action": "decline"} for key in entries}
    if kind != "mcp" or not isinstance(value, dict) or set(value) != set(entries):
        raise ValueError("请对本轮每个MCP问题提交对应答案")
    if len(json.dumps(value, ensure_ascii=False)) > 16000:
        raise ValueError("MCP回答过长")
    result = {}
    for key, entry in entries.items():
        body = value[key]
        if not isinstance(body, dict) or set(body) - {"action", "content"}:
            raise ValueError("MCP回答包含无效字段")
        try:
            answer = ElicitResult.model_validate(body)
        except ValueError as error:
            raise ValueError("MCP回答必须选择提交、拒绝或取消") from error
        if answer.action == "accept":
            if entry["mode"] == "url":
                if answer.content is not None:
                    raise ValueError("URL补问不能在聊天中收集凭据或表单内容")
            else:
                schema = entry["requested_schema"]
                try:
                    Draft202012Validator(schema, format_checker=FormatChecker()).validate(answer.content)
                except ValidationError as error:
                    raise ValueError("MCP回答的字段类型、选项或取值范围不符合原表单") from error
                if set(answer.content or {}) - set(schema.get("properties", {})):
                    raise ValueError("MCP回答包含原表单以外的字段")
        elif answer.content is not None:
            raise ValueError("拒绝或取消时不得附带表单内容")
        result[key] = answer.model_dump(mode="json", exclude_none=True)
    return result


class MCPInputs:
    def __init__(self, context):
        self.context = context

    def save(self, receipt_id: str, binding: dict, result: InputRequiredResult) -> dict:
        context = self.context
        body = public_input(result, binding["name"])
        from knowledgeops_agent.harness.mcp.registry import redact

        body = context.gateway._redact(redact(body, binding["config"]))
        with context.jobs.sessions.begin() as session:
            root = context.jobs._locked(session, context.root_id)
            executor = session.get(Run, context.run_id)
            call = session.get(MCPCall, receipt_id)
            previous = list(session.scalars(select(MCPContinuation).where(
                MCPContinuation.mcp_call_id == receipt_id,
            ).order_by(MCPContinuation.round)))
            if call.status != "running":
                raise ValueError("MCP调用已不处于接收状态，不能新增补问")
            for old in previous:
                if old.status == "sent":
                    old.status = "completed"
            cancelled = root.cancel_requested or executor.cancel_requested
            stale = (executor.authority_epoch != call.authority_epoch
                     or root.authority_epoch != root.applied_authority_epoch
                     or root.authority_epoch != call.request.get("root_authority_epoch", root.authority_epoch))
            row = RunQuestion(root_id=root.id, run_id=context.run_id, tool_call_id=call.tool_call_id,
                              round=len(previous) + 1, kind="mcp", authority_epoch=executor.authority_epoch,
                              request=body, status="cancelled" if cancelled else "stale" if stale else "pending")
            session.add(row)
            session.flush()
            session.add(MCPContinuation(mcp_call_id=receipt_id, question_id=row.id, round=row.round,
                                        request_state={"state": result.request_state, "root_epoch": root.authority_epoch},
                                        status="cancelled" if cancelled else "invalidated" if stale else "waiting"))
            control = session.get(RunControl, root.id)
            if control is None:
                control = RunControl(run_id=root.id, wait_spec={})
                session.add(control)
            control.wait_spec = {**control.wait_spec, "question_ids": [
                *control.wait_spec.get("question_ids", []), row.id,
            ]}
            call.status, call.updated_at = "denied" if cancelled or stale else "waiting_input", context.jobs.now()
            context.jobs._event(session, root, "question_pending", {"question_id": row.id, "run_id": context.run_id})
            return question_view(row)

    def await_ready(self, receipt_id: str, consumed: int) -> tuple[dict, int]:
        """重入按原顺序重放中断位置；Graph 的旧答案不能替代数据库中的当前状态。"""
        context = self.context
        with context.jobs.sessions() as session:
            rounds = [(row.id, row.question_id) for row in session.scalars(select(MCPContinuation).where(
                MCPContinuation.mcp_call_id == receipt_id,
            ).order_by(MCPContinuation.round))]
        for _, question_id in rounds[consumed:]:
            interrupt({"run_question_id": question_id})
            consumed += 1
        with context.jobs.sessions.begin() as session:
            root, executor = context.jobs.execution_guard(session, context.run_id, context.worker_id)
            continuation = session.get(MCPContinuation, rounds[-1][0])
            question = session.get(RunQuestion, continuation.question_id)
            if (question.status not in {"answered", "declined"}
                    or question.authority_epoch != executor.authority_epoch
                    or continuation.request_state["root_epoch"] != root.authority_epoch):
                raise PermissionError("原MCP补问已取消或因要求变化失效，未继续外部调用")
            responses = validate_answer(question.request, question.answer["kind"], question.answer["value"])
            continuation.input_responses, continuation.status = responses, "ready"
            continuation.updated_at = context.jobs.now()
            return {"id": continuation.id, "request_state": continuation.request_state["state"],
                    "input_responses": responses}, consumed

    def complete(self, session, receipt_id: str) -> None:
        for row in session.scalars(select(MCPContinuation).where(
            MCPContinuation.mcp_call_id == receipt_id, MCPContinuation.status == "sent",
        )):
            row.status, row.updated_at = "completed", self.context.jobs.now()
