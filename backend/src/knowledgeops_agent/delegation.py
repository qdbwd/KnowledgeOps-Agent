"""受根任务控制的同步研究委派：并行执行，子图独立检查点，不另起任务队列。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from langchain_core.messages import AIMessage

from knowledgeops_agent.jobs import RunStopped
from knowledgeops_agent.model_gateway import ModelBudgetExceeded
from knowledgeops_agent.response_validation import InvalidModelOutput, model_output_error
from knowledgeops_agent.run_resources import prepare_resources


def research_many(context, tasks: list[str], tool_call_id: str) -> list[dict]:
    """主工具等待研究者返回；线程不继承主图的隐式 Runnable 配置/检查点命名空间。

    子运行的稳定键来自真实工具调用 ID，重复恢复不会创建另一批研究者。
    限制在数据库里再次执行，两个同时到达的委派工具也无法突破并发上限。
    """
    if context.run_id != context.root_id:
        raise PermissionError("recursive delegation is disabled")
    if not tool_call_id or not 1 <= len(tasks) <= 2:
        raise ValueError("delegation requires a call ID and one or two research tasks")
    if context.checkpointer is None:
        raise RuntimeError("research requires a configured checkpointer")
    # 服务入口可独立调用；先由主运行冻结，子研究只能继承，不抢先生成清单。
    prepare_resources(context)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="research") as pool:
        futures = [
            pool.submit(_research_one, context, task, f"{context.run_id}:{tool_call_id}:{index}")
            for index, task in enumerate(tasks)
        ]
        return [future.result() for future in futures]


def delegate_one(context, definition: dict, task: str, tool_call_id: str) -> dict:
    """为上游task复用原持久子运行服务；参数必须来自根任务冻结角色定义。"""
    from knowledgeops_agent.subagent_registry import SubagentRegistry

    if context.run_id != context.root_id:
        raise PermissionError("recursive delegation is disabled")
    if not tool_call_id or context.checkpointer is None:
        raise ValueError("delegation requires a call ID and configured checkpointer")
    manifest = prepare_resources(context)
    if definition not in manifest.get("subagents", []):
        raise PermissionError("角色不在本任务冻结目录中；请以新任务使用新增配置")
    SubagentRegistry(context.jobs.sessions).validate_binding(definition)
    # 与原研究委派相同：隔离父图隐式配置，子图使用自己的checkpoint_id。
    # 并发上限由start_child数据库锁控制，不能只依赖各线程池的容量。
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="subagent") as pool:
        return pool.submit(
            _research_one,
            context,
            task,
            f"{context.run_id}:{tool_call_id}:0",
            definition,
        ).result()


def _research_one(parent, task: str, operation_key: str, definition: dict | None = None) -> dict:
    from knowledgeops_agent.runtime import build_agent, initial_messages

    parent.jobs.guard(parent.run_id, worker_id=parent.worker_id)
    child = parent.jobs.start_child(
        parent.run_id, task, operation_key=operation_key, subagent=definition
    )
    if child["status"] != "running":
        return _result(child)
    from knowledgeops_agent.learning_collaboration import LearningCollaborationService

    LearningCollaborationService(parent.jobs.sessions).bind_child(parent, child, operation_key)
    context = replace(parent, run_id=child["id"])
    config = {"configurable": {"thread_id": child["checkpoint_id"]}, "recursion_limit": 400}
    status, reason, outcome = "completed", None, {}
    stop = None
    try:
        graph = build_agent(context, checkpointer=parent.checkpointer)
        snapshot = graph.get_state(config)
        if snapshot.values.get("messages") and not snapshot.next:
            state = snapshot.values
        else:
            state = graph.invoke(
                None
                if snapshot.values.get("messages")
                else {"messages": initial_messages(context)},
                config,
                context=context,
            )
        context.jobs.guard(context.run_id, worker_id=context.worker_id)
        last = state.get("messages", [])[-1:]
        # 完成后的旧检查点不会再次经过模型中间件，交付给主Agent前仍要检查。
        if last and (error := model_output_error(last[0])):
            raise InvalidModelOutput(error)
        outcome = {
            "kind": "research",
            "answer": last[0].text if last and isinstance(last[0], AIMessage) else "",
        }
    except InvalidModelOutput as exc:
        # 应用自己定义的说明可直接反馈；供应商异常仍由下方通用路径隐藏细节。
        status, reason = "failed", str(exc)
    except (RunStopped, ModelBudgetExceeded) as exc:
        stop = exc
        status = (
            "budget_exhausted"
            if isinstance(exc, ModelBudgetExceeded) or "budget" in str(exc)
            else "interrupted"
        )
        reason = str(exc)
    except Exception as exc:
        status, reason = (
            "failed",
            f"{type(exc).__name__}: research failed; saved checkpoint is available",
        )
    finished = context.jobs.finish(
        context.run_id, context.worker_id, status=status, outcome=outcome, reason=reason
    )
    if stop is not None:
        raise stop
    return _result(finished)


def _result(run: dict) -> dict:
    """只返回最终报告，原始搜索、读取、提示词和计费留在子运行记录。"""
    return {
        "run_id": run["id"],
        "agent_name": run["input"].get("subagent", {}).get("name", "researcher"),
        "status": run["status"],
        "answer": run["outcome"].get("answer", ""),
        "error": run["stop_reason"],
    }
