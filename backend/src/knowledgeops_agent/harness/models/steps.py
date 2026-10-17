"""将图中的模型步骤绑定到网关回执，弥合响应与检查点的提交间隙。

使用 LangChain 公开 before_model / wrap_model_call 钩子；步骤身份先进入图状态。
请求快照在最终模型适配层保存，避免恢复时预算等动态背景变化产生另一份请求。
这里不增加模型重试；网关中的未知请求仍保持未知。
"""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import messages_from_dict, messages_to_dict
from langchain_core.utils.function_calling import convert_to_openai_tool
from sqlalchemy import or_, select

from knowledgeops_agent.persistence.models import ModelStep, RunEvent, new_id

_step_scope: ContextVar[tuple[str, str, int] | None] = ContextVar("model_step", default=None)


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ModelStepMiddleware(AgentMiddleware):
    def before_model(self, state, runtime):
        # 节点提交之后才进入实际模型节点；恢复模型节点不重新生成这个身份。
        return {"model_step_key": new_id()}

    def wrap_model_call(self, request, handler):
        key = request.state.get("model_step_key")
        if key is None:
            # 旧检查点可能直接停在 model，不会经过新增的 before_model 节点。
            # 使用旧消息及修订信息确定身份，不能在每次恢复时生成新的随机值。
            key = "legacy-" + _digest({
                "messages": messages_to_dict(request.state.get("messages", [])),
                "feedback": request.state.get("final_response_feedback", []),
                "authority_epoch": request.state.get("decision_authority_epoch", 0),
            })
        token = _step_scope.set((
            request.runtime.context.run_id, key, request.state.get("decision_authority_epoch", 0),
        ))
        try:
            return handler(request)
        finally:
            _step_scope.reset(token)

    def after_model(self, state, runtime):
        # 本钩子是独立图节点，进入这里时模型响应已写入前一节点检查点。
        # 暂停不能在包裹模型的 handler 返回后抛出，否则会丢失该次状态更新。
        context = runtime.context
        context.jobs.guard(context.run_id, worker_id=context.worker_id)
        return None


def bind_step_request(context, messages, tools, parameters):
    """返回稳定调用 ID 和该步骤第一次真正发出的请求，不以文本相同合并不同步骤。"""
    scope = _step_scope.get()
    if scope is None:
        # 适配器也可独立使用；独立业务调用仍使用它们各自现有的网关身份。
        return None, messages, tools, parameters
    run_id, key, authority_epoch = scope
    if run_id != context.run_id:
        raise ValueError("模型步骤属于另一运行，不能跨主子 Agent 共享")
    with context.jobs.sessions.begin() as session:
        root = context.jobs._locked(session, context.root_id)
        step = session.scalar(select(ModelStep).where(
            ModelStep.run_id == run_id,
            or_(ModelStep.step_key == key, ModelStep.step_key.startswith(key + ":retry:", autoescape=True)),
        ).order_by(ModelStep.created_at.desc(), ModelStep.id.desc()).limit(1))
        if step is not None:
            previous = context.gateway.ledger.linked_record(run_id, step.call_id)
            resumed = session.scalar(select(RunEvent).where(
                RunEvent.run_id == context.root_id, RunEvent.kind == "resumed",
            ).order_by(RunEvent.sequence.desc()).limit(1))
            if (previous and previous["status"] == "failed" and resumed
                    and resumed.created_at > step.created_at
                    and resumed.data.get("generation") == root.generation):
                # 保留原有真人明确恢复后重试已结束失败的能力。事件自动接续不能
                # 获得此权限；reserved/成功的尝试在任何代次都不重新发送。
                key = key + f":retry:{root.generation}"
                step = None
        if step is None:
            snapshot = context.gateway._redact({
                "messages": messages_to_dict(messages),
                "tools": [convert_to_openai_tool(tool) for tool in tools],
                "parameters": context.gateway.request_parameters(parameters),
            })
            step = ModelStep(
                run_id=run_id, step_key=key, call_id=new_id(), authority_epoch=authority_epoch,
                request_hash=_digest(snapshot), request_snapshot=snapshot,
            )
            session.add(step)
            session.flush()
        if _digest(step.request_snapshot) != step.request_hash:
            raise ValueError("模型步骤请求快照校验失败，不能重新发送")
        call_id, snapshot = step.call_id, step.request_snapshot
    # 正常路径保留 SDK 工具对象；只有重放时当前投影变化，才使用原 Schema。
    # 网关按原调用身份返回已保存结果，执行工具仍经过当前权限检查。
    bound_tools = tools if [convert_to_openai_tool(tool) for tool in tools] == snapshot["tools"] else snapshot["tools"]
    return call_id, messages_from_dict(snapshot["messages"]), bound_tools, snapshot["parameters"]
