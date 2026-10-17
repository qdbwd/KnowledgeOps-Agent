"""离线兼容用例的调度驱动：真实图中断、持久 dispatch 与真实子 worker。

旧用例直接调用同步函数；现在同步的含义是主图等待，不是 Python 栈阻塞。
这里显式领取已经持久化的子任务，复用生产 worker，不恢复第二套执行实现。
真正三槽并发和进程恢复由 test_async_worker_persistence 单独验证。
"""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from knowledgeops_agent.app.runs.children import ChildRuns
from knowledgeops_agent.app.runs.worker import execute_run
from knowledgeops_agent.app.services import Services
from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.harness.runtime.state import RunContext


def execute_family(run_id, generation, *, services, checkpointer, worker_id="offline-family"):
    """在没有 Celery 的测试进程投递原根任务及其已入队子任务，不跨到下一学习单元。"""
    result = execute_run(run_id, generation, services=services, checkpointer=checkpointer,
                         worker_id=worker_id)
    if result is None:
        return None
    children = ChildRuns(services.jobs)
    for _ in range(16):
        if result["status"] != "waiting_children":
            return result
        dispatched = False
        for child in children.list(run_id):
            row = services.jobs.get_run(child["task_id"])
            if row["status"] == "queued":
                execute_run(row["id"], row["generation"], services=services,
                            checkpointer=checkpointer, worker_id=f"{worker_id}:child")
                dispatched = True
        result = services.jobs.get_run(run_id)
        if result["status"] == "queued":
            result = execute_run(run_id, result["generation"], services=services,
                                 checkpointer=checkpointer, worker_id=worker_id)
        elif not dispatched:
            return result  # 真人待办不会因测试调度器而自动批准或恢复。
    raise AssertionError("根任务组未在限定投递次数内收束")


def drive_graph(context, graph, inputs, *, thread_id=None):
    config = {"configurable": {"thread_id": thread_id or context.run_id}}
    service = ChildRuns(context.jobs)
    services = Services(
        settings=Settings(_env_file=None, model_calls_enabled=False, workspace_root=context.workspace_root),
        engine=context.jobs.sessions.kw["bind"], sessions=context.jobs.sessions,
        jobs=context.jobs, knowledge=context.knowledge, sources=context.sources, gateway=context.gateway,
    )
    for _ in range(16):
        state = graph.invoke(inputs, config, context=context)
        snapshot = graph.get_state(config)
        if not snapshot.interrupts:
            return state
        ids = []
        for item in snapshot.interrupts:
            assert isinstance(item.value, dict) and "wait_child_ids" in item.value, item.value
            ids.extend(item.value["wait_child_ids"])
        service.wait(context.root_id, context.worker_id, list(dict.fromkeys(ids)))
        for child in service.list(context.root_id):
            row = context.jobs.get_run(child["task_id"])
            if row["status"] == "queued":
                execute_run(row["id"], row["generation"], services=services, checkpointer=context.checkpointer)
        root = context.jobs.get_run(context.root_id)
        assert root["status"] == "queued", root
        assert context.jobs.claim(context.worker_id, run_id=root["id"], generation=root["generation"])
        inputs = Command(resume=service.resume_answers(context.root_id, snapshot))
    raise AssertionError("离线兼容场景未在限定轮次内完成，不能靠无限轮询掩盖问题")


def drive_delegation(context, action, *, identity):
    """把服务调用放入最小真实图，保留原用例对身份/权限/账本的断言。"""
    if context.checkpointer is None:
        context.checkpointer = InMemorySaver()
    builder = StateGraph(dict, context_schema=RunContext)
    builder.add_node("delegate", lambda state: {"result": action()})
    builder.add_edge(START, "delegate")
    builder.add_edge("delegate", END)
    graph = builder.compile(checkpointer=context.checkpointer)
    return drive_graph(context, graph, {}, thread_id=f"{context.run_id}:compat:{identity}")["result"]
