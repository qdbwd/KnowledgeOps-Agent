"""同步委派保持原工具参数与结果，使用统一持久子运行调度。

同步指主图在原工具位置等待；中断后释放 worker 槽位，由子结果接续。
不再占住主线程等待第二套线程池。
"""

from knowledgeops_agent.harness.subagents.async_tools import start, wait


def research_many(context, tasks: list[str], tool_call_id: str) -> list[dict]:
    if not tool_call_id or not 1 <= len(tasks) <= 2:
        raise ValueError("delegation requires a call ID and one or two research tasks")
    children = [start(context, task, tool_call_id, suffix=str(index)) for index, task in enumerate(tasks)]
    return wait(context, [row["task_id"] for row in children])


def delegate_one(context, definition: dict, task: str, tool_call_id: str) -> dict:
    child = start(context, task, tool_call_id, definition)
    return wait(context, [child["task_id"]])[0]
