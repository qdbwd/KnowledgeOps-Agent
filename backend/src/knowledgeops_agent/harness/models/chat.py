"""LangChain 模型适配层：工具绑定与请求统一经过模型网关。"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from knowledgeops_agent.harness.runtime.resources import ResourceService
from knowledgeops_agent.harness.tools.access import (
    filter_tools,
)


class LedgerChatModel(BaseChatModel):
    """将 LangChain 的标准模型接口接到统一账本，禁止旁路模型请求。

    bind_tools 只记录本轮工具说明。真正的 _generate 才检查取消、预占额度、
    请求供应商，并把流式片段作为可恢复事件写入数据库。
    """

    context: Any = Field(exclude=True)

    @property
    def _llm_type(self) -> str:
        return "knowledgeops-ledger-model"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        return self.bind(tools=tools, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        context = self.context
        root = context.jobs.guard(context.run_id, worker_id=context.worker_id)
        # 摘要中间件可能已执行模型请求；每次真正发出研究/主请求前重新核对撤回。
        if context.resources is not None:
            ResourceService(context.jobs.sessions).validate(context, context.resources)
        tools = kwargs.pop("tools", ())
        if context.resources is not None:
            # 上下文压缩本身可能耗时；真正发送前再次检查期间发生的工具撤回。
            tools = filter_tools(context, tools)
        # 保存的是用户可见输出片段，不假装能读取供应商隐藏的推理。
        pieces: list[str] = []

        def on_chunk(content: str) -> None:
            pieces.append(content)
            if sum(map(len, pieces)) >= 120:
                context.jobs.append_event(context.run_id, "model_delta", {"text": "".join(pieces)})
                pieces.clear()

        context.jobs.append_event(context.run_id, "model_started", {})
        from knowledgeops_agent.harness.models.reasoning import run_reasoning_parameters

        # 每个根运行固定请求参数，不能由模型或恢复时的新配置覆盖。
        kwargs.update(run_reasoning_parameters(context))
        from knowledgeops_agent.harness.models.steps import bind_step_request

        call_id, messages, tools, kwargs = bind_step_request(
            context, messages, tools, {**({"stop": stop} if stop else {}), **kwargs},
        )
        response = context.gateway.invoke(
            messages,
            root_id=context.root_id,
            run_id=context.run_id,
            purpose="research" if context.run_id != context.root_id else "agent",
            root_limit=root["budgets"]["model_requests"],
            root_reserve=1 if context.run_id != context.root_id else 0,
            tools=tools,
            stream=True,
            on_chunk=on_chunk,
            call_id=call_id,
            **kwargs,
        )
        if pieces:
            context.jobs.append_event(context.run_id, "model_delta", {"text": "".join(pieces)})
        context.jobs.append_event(
            context.run_id,
            "model_finished",
            {
                "tool_calls": len(response.tool_calls),
                "usage": response.usage_metadata,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=response)])
