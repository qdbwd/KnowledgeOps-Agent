"""Celery 消费入口与一次 Agent 运行的生命周期。

领取、心跳、检查点和业务结果各有明确职责；Celery 的任务成功只表示消费
函数返回，最终业务状态必须查看 PostgreSQL 的 Run，不依赖 broker 回执。
"""

from __future__ import annotations

import logging
import os
import signal
from contextlib import contextmanager, nullcontext
from threading import Event, Thread
from uuid import uuid4

from langchain_core.messages import AIMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg import Connection
from psycopg.rows import dict_row

from knowledgeops_agent.app.runs.children import ChildrenWaiting, ChildRuns
from knowledgeops_agent.app.runs.questions import InputWaiting, RunQuestions
from knowledgeops_agent.app.runs.queue import create_queue, worker_lease
from knowledgeops_agent.app.runs.service import RunPaused, RunStopped
from knowledgeops_agent.app.services import Services, create_services
from knowledgeops_agent.core.config import get_settings
from knowledgeops_agent.harness.context.capacity import ContextCapacityExceeded
from knowledgeops_agent.harness.context.continuation import from_state
from knowledgeops_agent.harness.mcp.calls import MCPApprovalRequired, MCPCalls
from knowledgeops_agent.harness.models.gateway import (
    ModelBudgetExceeded,
    ModelCallFailed,
    ModelCallsDisabled,
)
from knowledgeops_agent.harness.models.validation import (
    InvalidModelOutput,
    model_output_error,
)
from knowledgeops_agent.harness.permissions.approvals import ApprovalWaiting, ExecutionApprovals
from knowledgeops_agent.harness.permissions.receipts import ExecutionUnknown
from knowledgeops_agent.harness.runtime.agent import RunContext, build_agent, initial_messages
from knowledgeops_agent.harness.runtime.resources import ResourceUnavailable, prepare_resources
from knowledgeops_agent.knowledge.intent import ensure_publication_intent
from knowledgeops_agent.persistence.checkpoints import CachedPostgresSaver
from knowledgeops_agent.sources.pdf import parse_pdf
from knowledgeops_agent.sources.web import WebResearchService

logger = logging.getLogger(__name__)
_services: Services | None = None
_worker_id = "worker-" + uuid4().hex
_lease_connection = None


@contextmanager
def open_checkpointer(services: Services):
    """独立 psycopg 连接保存 LangGraph 状态；连接关闭后检查点仍在 PostgreSQL。

    与业务事务分开，因此恢复时可能重放工具。业务写入的幂等回执仍然必需。
    schema 升级只做幂等 setup，不清除已有 thread 或状态。
    """
    args, kwargs = services.engine.dialect.create_connect_args(services.engine.url)
    # 测试/独立部署可通过engine连接参数选schema，不能另开连接时偷偷回到public。
    from psycopg import sql

    with services.engine.connect() as source:
        schemas: list[str] = source.exec_driver_sql("SELECT current_schemas(false)").scalar_one()
    with Connection.connect(
        *args, **kwargs, autocommit=True, prepare_threshold=0, row_factory=dict_row
    ) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(
            sql.SQL(", ").join(sql.Identifier(name) for name in schemas),
        ))
        saver = (
            CachedPostgresSaver(connection, cache=services.checkpoint_cache)
            if services.checkpoint_cache is not None
            else PostgresSaver(connection)
        )
        saver.setup()
        yield saver


def _outcome(context: RunContext, state: dict, *, stopped: bool = False) -> dict:
    """结合真实草稿/发布事实解释运行结果，不能只靠模型声称“已保存”。"""
    if context.run_id != context.root_id:
        last = state.get("messages", [])[-1:]
        return {"kind": "research", "answer": last[0].text if last and isinstance(last[0], AIMessage)
                and not last[0].tool_calls and not model_output_error(last[0]) else ""}
    drafts = context.knowledge.list_drafts(root_id=context.root_id)
    pending = [row["id"] for row in drafts if row["status"] in {"draft", "approved"}]
    published = [row["id"] for row in drafts if row["status"] == "published"]
    choice = context.jobs.publication_intent(context.root_id) or {}
    published = list(dict.fromkeys([*published, *choice.get("published_ids", [])]))
    decision = choice.get("decision", {})
    if decision.get("action") == "approve_draft" and decision.get("changeset_id"):
        # 跨轮事务提交后、结果事件落盘前崩溃，也按业务事实展示已保存内容。
        # 该ID来自先前已校验并持久化的真人决定，不是当前模型自由声明。
        try:
            target = context.knowledge.get_draft(decision["changeset_id"])
            if target["status"] == "published" and target["id"] not in published:
                published.append(target["id"])
        except (ValueError, KeyError):
            pass
    last = state.get("messages", [])[-1:]
    answer = (
        last[0].text if last and isinstance(last[0], AIMessage) and not last[0].tool_calls else ""
    )
    output_failed = bool(last and model_output_error(last[0]))
    if state.get("final_response_feedback") or output_failed:
        # 未通过的最终文字保留在模型轨迹中，不当作可交付答案；已提交草稿仍可审阅。
        answer = ""
    kind = (
        "partial"
        if stopped or output_failed
        else "draft_ready"
        if pending
        else "published"
        if published
        else "answered"
    )
    return {"kind": kind, "answer": answer, "draft_ids": pending, "published_ids": published}


def execute_material(services: Services, run: dict, owner: str) -> dict:
    """确定性材料任务，与 Agent 共用队列/事件/停止，不用 LLM 做下载或解析。"""
    services.jobs.guard(run["id"], worker_id=owner)
    cached = False
    if run["input"]["type"] == "pdf":
        source = parse_pdf(
            services.sources,
            run["input"]["version_id"],
            check=lambda: services.jobs.guard(run["id"], worker_id=owner),
            progress=lambda data: services.jobs.append_event(run["id"], "page_parsed", data),
        )
    elif run["input"]["type"] == "web":
        source, cached = WebResearchService(services.sources).import_url(
            run["input"]["url"], refresh=run["input"].get("refresh", False),
            conversation_id=run["input"].get("conversation_id"),
        )
    else:
        raise ValueError("unsupported material parser")
    services.jobs.append_event(
        run["id"],
        "material_ready",
        {"source_id": source["id"], "version_id": source["version_id"], "cached": cached},
    )
    return {
        "kind": "material_ready" if source["status"] == "ready" else "partial",
        "source_id": source["id"],
        "source_version_id": source["version_id"],
        "answer": "材料已保存，可在对话中选择并引用。"
        if source["status"] == "ready"
        else "原件及已完成页面已保存，部分页面提取失败。请查看来源逐页状态。",
    }


def execute_run(
    run_id: str,
    generation: int,
    *,
    services: Services | None = None,
    checkpointer=None,
    worker_id: str | None = None,
):
    """校验代次后执行一次；失败保留状态，重复通知返回 None 且不调用模型。

    tests 可注入相同真实服务和隔离检查点。生产依赖由 main 初始化，绝不在
    任意外部调用时静默开启一套绕过 worker 独占租约的消费者。
    """
    services = services or _services
    if services is None:
        raise RuntimeError("start the worker entry point before consuming tasks")
    owner = worker_id or f"{_worker_id}:{run_id}:{generation}"
    run = services.jobs.claim(owner, run_id=run_id, generation=generation)
    if run is None:
        return None
    context = RunContext(
        run_id=run_id,
        root_id=run["root_id"],
        worker_id=owner,
        jobs=services.jobs,
        knowledge=services.knowledge,
        sources=services.sources,
        gateway=services.gateway,
        workspace_root=services.settings.workspace_root,
        workspace_execute_timeout=services.settings.workspace_execute_timeout,
        workspace_output_limit_bytes=services.settings.workspace_output_limit_bytes,
    )
    stop_heartbeat = Event()

    def heartbeat() -> None:
        while not stop_heartbeat.wait(2):
            try:
                if _lease_connection is not None:
                    _lease_connection.check()
                services.jobs.heartbeat(run_id, owner)
            except Exception:
                # 租约失效不能继续领取/执行新动作；具体根因在服务日志中只记类型。
                try:
                    services.jobs.finish(
                        run_id, owner, status="interrupted", reason="worker lease or heartbeat lost"
                    )
                except Exception as exc:
                    logger.error(
                        "heartbeat could not save interrupted state (%s)", type(exc).__name__
                    )
                return

    heart = Thread(target=heartbeat, name=f"heartbeat-{run_id}", daemon=True)
    heart.start()
    graph = None
    state: dict = {}
    config = {"configurable": {"thread_id": run["checkpoint_id"]}, "recursion_limit": 400}
    status, reason = "completed", None
    material_outcome = None
    children_wait = None
    compacted_state = None
    try:
        if run["kind"] == "material":
            material_outcome = execute_material(services, run, owner)
            services.jobs.guard(run_id, worker_id=owner)
            if material_outcome["kind"] == "partial":
                status, reason = "failed", "部分页面提取失败，可核对原件后选择重试。"
        elif run["kind"] == "context_compaction":
            from knowledgeops_agent.harness.context.manual import execute as compact_context

            prepare_resources(context)
            material_outcome, compacted_state = compact_context(context)
        elif run["kind"] == "memory_classification":
            from knowledgeops_agent.memory.classification import execute as classify_memory

            material_outcome = classify_memory(context)
            if material_outcome["kind"] == "memory_classification_failed":
                status, reason = "failed", material_outcome["message"]
        else:
            # 在第一条收费解释请求之前验证/冻结核心规则，避免缺失配置仍消费额度。
            prepare_resources(context)
            choice = ensure_publication_intent(context) if run_id == context.root_id else {}
            if choice.get("published_ids") and not choice.get("continue_task"):
                # 单纯确认既有草稿已由可信服务完成，无需再耗一个模型请求解释回执。
                material_outcome = {
                    "kind": "published",
                    "answer": "已保存确认的草稿，可在知识库中查看。",
                    "answer_origin": "application_receipt",
                    "draft_ids": [],
                    "published_ids": choice["published_ids"],
                }
                return services.jobs.finish(
                    run_id, owner, status="completed", outcome=material_outcome
                )
            with (
                nullcontext(checkpointer)
                if checkpointer is not None
                else open_checkpointer(services) as saver
            ):
                graph = build_agent(context, checkpointer=saver)
                snapshot = graph.get_state(config)
                if (
                    snapshot.values.get("messages")
                    and not snapshot.next
                    and not snapshot.interrupts
                ):
                    # 图完成后、应用终态保存前崩溃：读取已有输出，不再发模型请求。
                    state = snapshot.values
                else:
                    approvals = ExecutionApprovals(context)
                    inputs: Command | dict | None
                    if snapshot.interrupts:
                        approval_input = approvals.resume_input(snapshot)
                        answers = RunQuestions(services.jobs).resume_answers(context, snapshot)
                        child_answers = ChildRuns(services.jobs).resume_answers(context.root_id, snapshot)
                        inputs = Command(resume={**(approval_input.resume or {}), **answers, **child_answers})
                    else:
                        inputs = None if snapshot.values.get("messages") else {"messages": initial_messages(context)}
                    try:
                        state = graph.invoke(inputs, config=config, context=context)
                    finally:
                        # 即使中途抛出异常，已提交的节点状态仍可展示并用于手动恢复。
                        state = graph.get_state(config).values
                    stopped_snapshot = graph.get_state(config)
                    if stopped_snapshot.interrupts:
                        for item in stopped_snapshot.interrupts:
                            if isinstance(item.value, dict) and "wait_child_ids" in item.value:
                                raise ChildrenWaiting(item.value["wait_child_ids"], item.value.get("mode", "all"))
                        if RunQuestions(services.jobs).record_interrupts(context, stopped_snapshot):
                            raise InputWaiting("问题等待你的回答，执行资源已释放。")
                        approvals.record_interrupts(stopped_snapshot)
                        raise ApprovalWaiting("操作等待你的批准，执行资源已释放。")
                # 兼容旧检查点：旧图已结束仍要检查截断/泄漏，不能恢复成成功交付。
                last = state.get("messages", [])[-1:]
                if last and (error := model_output_error(last[0])):
                    raise InvalidModelOutput(error)
    except ChildrenWaiting as exc:
        status, reason, children_wait = "waiting_children", str(exc), exc
    except InputWaiting as exc:
        status, reason = "waiting_input", str(exc)
    except (MCPApprovalRequired, ApprovalWaiting) as exc:
        status, reason = "interrupted", str(exc)
        material_outcome = {
            **_outcome(context, state, stopped=True),
            "kind": "waiting_approval",
            "mcp_pending_ids": [
                item["id"] for item in MCPCalls(context).list() if item["status"] == "pending"
            ],
        }
    except ExecutionUnknown as exc:
        status, reason = "interrupted", str(exc)
        material_outcome = {**_outcome(context, state, stopped=True), "kind": "execution_unknown"}
    except ModelBudgetExceeded as exc:
        status, reason = "budget_exhausted", str(exc)
    except RunPaused as exc:
        status, reason = "paused", str(exc)
    except RunStopped as exc:
        reason = str(exc)
        status = (
            "cancelled"
            if "cancel" in reason
            else "budget_exhausted"
            if "budget" in reason
            else "interrupted"
        )
    except (
        ModelCallsDisabled,
        ModelCallFailed,
        ContextCapacityExceeded,
        ResourceUnavailable,
        InvalidModelOutput,
    ) as exc:
        status, reason = "failed", str(exc)
    except ValueError as exc:
        # 材料适配器的业务错误已清除底层 HTTP/解析配置，可明确展示失败原因。
        status, reason = (
            "failed",
            str(exc)
            if run["kind"] in {"material", "context_compaction"}
            else "invalid operation; saved progress is available",
        )
    except Exception as exc:
        # 底层异常正文可能携带连接参数；对外保留类型、运行 ID 和已保存进度。
        status, reason = (
            "failed",
            f"{type(exc).__name__}: operation failed; saved progress is available",
        )
        logger.error("run %s failed (%s)", run_id, type(exc).__name__)
    finally:
        stop_heartbeat.set()
        heart.join(timeout=5)
    outcome = material_outcome or _outcome(context, state, stopped=status != "completed")
    if status == "paused":
        return services.jobs.settle_pause(run_id, owner, outcome=outcome)
    if status == "waiting_input":
        return RunQuestions(services.jobs).settle_wait(context, outcome)
    if children_wait is not None:
        return ChildRuns(services.jobs).wait(run_id, owner, children_wait.ids,
                                            mode=children_wait.mode, outcome=outcome)
    if run["kind"] == "material" and not material_outcome and run["input"].get("version_id"):
        source = services.sources.get_version(run["input"]["version_id"])
        outcome.update(source_id=source["id"], source_version_id=source["version_id"])
    finished = services.jobs.finish(
        run_id,
        owner,
        status=status,
        outcome=outcome,
        reason=reason,
        continuation=from_state(context, compacted_state) if compacted_state and status == "completed"
        else from_state(context, state) if run["kind"] == "agent" else None,
    )
    if (
        finished["status"] == "interrupted"
        and finished["outcome"].get("kind") == "waiting_approval"
    ):
        from knowledgeops_agent.harness.permissions.approvals import queue_after_decisions

        # MCP 的兼容 API 允许用户在 worker 写终态前决定；同一事务锁守卫
        # 保证这里与用户请求竞争时最多只新增一个继续代次。
        return queue_after_decisions(services.jobs, run_id) or finished
    return finished


def prepare_mcp(operation_id: str, generation: int, *, services: Services | None = None):
    """复用本worker依赖及租约；安装拥有独立持久身份，不走模型或对话预算。"""
    from knowledgeops_agent.harness.mcp.management import MCPManagement

    services = services or _services
    if services is None:
        raise RuntimeError("start the worker entry point before preparing MCP dependencies")
    def check_live():
        services.check_maintenance()
        if _lease_connection is not None:
            _lease_connection.check()

    check_live()
    return MCPManagement(services.sessions, live_check=check_live).execute(operation_id, generation)


def main() -> None:
    """先取得独占租约并中断遗留运行，再启动一个根任务消费者。"""
    global _services, _lease_connection
    _services = create_services(get_settings())
    try:
        with worker_lease(_services.engine) as lease:
            _lease_connection = lease
            def check_live():
                _services.check_maintenance()
                lease.check()

            _services.jobs.live_check = check_live
            count = _services.jobs.recover_on_startup()
            logger.warning("worker startup: %s unfinished runs await explicit resume", count)
            # 顶层 -m 薄入口调用本模块 main；显式注册这里已初始化依赖的函数，
            # 避免入口包装或另一种导入路径意外取得独立的 worker 实例。
            app = create_queue(_services.settings.celery_broker_url, handler=execute_run, mcp_handler=prepare_mcp)
            stop_watchdog = Event()

            def watch_lease():
                while not stop_watchdog.wait(2):
                    try:
                        check_live()
                    except RuntimeError:
                        # 只终止本 worker，Celery 正常处理 SIGTERM。进程重启后
                        # 遗留任务等待用户恢复，绝不靠重连静默继续收费操作。
                        logger.error("worker lease lost; stopping this consumer")
                        os.kill(os.getpid(), signal.SIGTERM)
                        return

            watcher = Thread(target=watch_lease, name="worker-lease", daemon=True)
            watcher.start()
            try:
                app.worker_main(
                    [
                        "worker",
                        "--pool=threads",
                        "--concurrency=3",
                        f"--loglevel={_services.settings.app_log_level.upper()}",
                        "--without-gossip",
                        "--without-mingle",
                    ]
                )
            finally:
                stop_watchdog.set()
                watcher.join(timeout=5)
    finally:
        _lease_connection = None
        _services.close()


if __name__ == "__main__":
    main()
