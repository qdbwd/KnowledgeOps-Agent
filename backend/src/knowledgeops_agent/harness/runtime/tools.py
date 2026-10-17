"""通用运行工具：读取长结果、更新计划及显式启用的辅助调用和委派。"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from knowledgeops_agent.harness.models.auxiliary import MaterialReference, call_auxiliary
from knowledgeops_agent.harness.runtime.state import RunContext


@tool
def read_tool_result(artifact_id: str, runtime: ToolRuntime[RunContext], start: int = 0) -> dict:
    """补读长工具结果或压缩前历史，每次最多 6000 字符。它不是新的来源证据。"""
    return runtime.context.jobs.read_artifact(
        artifact_id, root_id=runtime.context.root_id, start=start
    )


@tool
def call_model(
    prompt: str,
    runtime: ToolRuntime[RunContext],
    material_refs: list[MaterialReference] | None = None,
) -> dict:
    """按指定prompt做一次独立无工具模型调用，不自动继承主历史；需要集中处理/核对材料时使用。

    material_refs以kind/id引用固定source_version、source_segment、knowledge_version、draft_revision或artifact；
    草稿必须有revision，可用start/end选择字符范围。只读原文，不接受文件路径或URL。
    返回状态、完整产物引用及预览，消耗当前根任务模型预算；短答可以由主Agent直接完成。
    """
    return call_auxiliary(runtime.context, prompt, material_refs or [], runtime.tool_call_id or "")


@tool
def delegate_research(tasks: list[str], runtime: ToolRuntime[RunContext]) -> list[dict]:
    """并行委派一到两个相互独立的研究任务。每项应包含问题、材料 ID、范围和所需证据。

    研究者只有读取能力、不继承主聊天、不可递归，所有调用共享本任务预算。
    简单任务无需委派；同一工具调用恢复时复用已有子任务结果和检查点。
    """
    from knowledgeops_agent.harness.subagents.delegation import research_many

    return research_many(runtime.context, tasks, runtime.tool_call_id or "")


@tool
def update_plan(steps: list[dict[str, str]], runtime: ToolRuntime[RunContext]) -> Command:
    """为复杂任务维护少量计划步骤，每项含 step 和 status；简单问答不强制计划。"""
    if not 1 <= len(steps) <= 15 or any(
        not item.get("step") or item.get("status") not in {"pending", "active", "done"}
        for item in steps
    ):
        raise ValueError("plan needs 1..15 steps with step and pending/active/done status")
    return Command(
        update={
            "plan": steps,
            "messages": [
                ToolMessage(
                    content="计划已保存。",
                    name="update_plan",
                    tool_call_id=runtime.tool_call_id or "",
                )
            ],
        }
    )
