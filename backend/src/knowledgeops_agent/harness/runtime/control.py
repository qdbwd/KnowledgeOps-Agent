"""模型和工具调用边界：统一检查运行状态、权限、预算及用户补充要求。"""

from __future__ import annotations

import json
from dataclasses import replace

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Command

from knowledgeops_agent.app.runs.guidance import applied_messages
from knowledgeops_agent.app.runs.service import SteeringPending
from knowledgeops_agent.harness.models.validation import current_length_limit
from knowledgeops_agent.harness.permissions.approvals import (
    ExecutionApprovals,
    workspace_paths,
)
from knowledgeops_agent.harness.permissions.receipts import CommandJournal
from knowledgeops_agent.harness.runtime.resources import ResourceService
from knowledgeops_agent.harness.runtime.state import RunContext
from knowledgeops_agent.harness.tools.access import (
    check_tool_call,
    filter_tools,
    unavailable_tools,
)
from knowledgeops_agent.memory.service import MemoryService
from knowledgeops_agent.vendor.deerflow.task_continuity_state import (
    normalize_task_notes,
)


class RuntimeControlMiddleware(AgentMiddleware):
    """把正常业务错误反馈给模型；取消/预算/系统故障向外抛出，由 worker 停止。

    模型可以改变策略处理工具参数或版本冲突，不能通过再次尝试绕过停止。
    工具开始前记录调用及扣额，结束后记录实际结果；所有调用共用根预算。
    """

    def wrap_tool_call(self, request, handler):
        context: RunContext = request.runtime.context
        context.jobs.guard(context.run_id, worker_id=context.worker_id)
        if context.resources is not None:
            ResourceService(context.jobs.sessions).validate(context, context.resources)
        call = request.tool_call
        from knowledgeops_agent.harness.runtime.clarification import QUESTION_TOOLS, question_batch

        if question_batch(request.state) and call["name"] not in QUESTION_TOOLS:
            return ToolMessage(content="未执行：同批含真人补问，收到回答后必须重新决策。",
                               name=call["name"], tool_call_id=call["id"], status="error")
        journal, ticket = CommandJournal(context), None
        try:
            if context.resources is not None:
                check_tool_call(context, call["name"])
            current = context.jobs.get_run(context.run_id)
            if current["authority_epoch"] != current["applied_authority_epoch"] or (
                request.state.get("decision_authority_epoch", 0) != current["authority_epoch"]
            ):
                raise SteeringPending("未执行：追加要求已改变任务，先重新判断下一步。")
            ExecutionApprovals(context).before_tool(request)
            if call["name"] == "execute":
                workspace, cwd = workspace_paths(context)
                ticket = journal.start(call, workspace=workspace, cwd=cwd)
            elif not any(
                item["name"] == call["name"] and item["source"] == "mcp"
                for item in context.tool_catalog or []
            ):
                context.jobs.reserve_tool(
                    context.run_id,
                    call["name"],
                    is_search=call["name"] == "search_web",
                    call_key=call["id"],
                )
            if ticket and ticket["cached"]:
                result = ticket["result"]
                context.jobs.append_event(
                    context.run_id,
                    "tool_replayed",
                    {"tool_call_id": call["id"], "name": call["name"]},
                )
            else:
                context.jobs.append_event(
                    context.run_id,
                    "tool_started",
                    {
                        "tool_call_id": call["id"],
                        "name": call["name"],
                        "args": self._event_value(context, call["args"], "tool_input"),
                    },
                )
                if call["name"] == "execute":
                    from knowledgeops_agent.harness.sandbox.output import output_scope

                    with output_scope(context, ticket) as output:
                        result = handler(request)
                    if output is not None and isinstance(result, ToolMessage):
                        result = result.model_copy(update={"artifact": {
                            **(result.artifact or {}), "command_output": output.metadata,
                        }})
                else:
                    result = handler(request)
        except (ValueError, KeyError, PermissionError, SteeringPending) as exc:
            if ticket and not ticket["cached"]:
                journal.mark_unknown(ticket)
                from knowledgeops_agent.harness.permissions.receipts import ExecutionUnknown

                raise ExecutionUnknown(
                    "命令启动后发生异常且未保存完整回执，请核对后选择跳过或重试。"
                ) from exc
            result = ToolMessage(
                content=json.dumps(
                    {"error": str(exc), "type": type(exc).__name__}, ensure_ascii=False
                ),
                name=call["name"],
                tool_call_id=call["id"],
                status="error",
            )
        except BaseException:
            # 包括取消和图中断；若命令已经开始，不能凭异常推断没有副作用。
            if ticket and not ticket["cached"]:
                journal.mark_unknown(ticket)
            raise
        if isinstance(result, ToolMessage):
            if (
                call["name"] == "execute"
                and isinstance(result.artifact, dict)
                and result.artifact.get("exit_code", 0) != 0
            ):
                # 上游公开 ExecuteArtifact 保留退出码；不解析可被命令伪造的正文。
                result = result.model_copy(update={"status": "error"})
            try:
                result = self._deliver_tool_message(context, result, call["name"])
                if ticket and not ticket["cached"]:
                    journal.finish(ticket, result)
            except Exception as exc:
                if ticket and not ticket["cached"]:
                    journal.mark_unknown(ticket)
                    from knowledgeops_agent.harness.permissions.receipts import ExecutionUnknown

                    raise ExecutionUnknown(
                        "命令可能已完成，但结果回执保存失败；请核对后选择跳过或重试。"
                    ) from exc
                raise
        elif isinstance(result, Command) and isinstance(result.update, dict):
            # 上游task以Command返回消息。交付策略必须同样覆盖其消息，不改其他
            # 状态更新或路由，也不把Command误当成普通JSON文本。
            updates = dict(result.update)
            if isinstance(updates.get("messages"), list):
                updates["messages"] = [
                    self._deliver_tool_message(context, item, call["name"])
                    if isinstance(item, ToolMessage)
                    else item
                    for item in updates["messages"]
                ]
                result = replace(result, update=updates)
        if isinstance(result, ToolMessage):
            payload = {"content": result.content, "status": result.status}
        else:
            # Command可含消息对象；仅事件转JSON，不更改其状态更新语义。
            payload = json.loads(
                json.dumps(
                    {"state_update": result.update},
                    default=lambda value: value.model_dump(mode="json"),
                )
            )
        context.jobs.append_event(
            context.run_id,
            "tool_finished",
            {
                "tool_call_id": call["id"],
                "name": call["name"],
                **self._event_value(context, payload, "tool_event"),
            },
        )
        return result

    @staticmethod
    def _deliver_tool_message(
        context: RunContext, result: ToolMessage, tool_name: str
    ) -> ToolMessage:
        """普通工具与上游Command共用脱敏/长结果外置；只改变交付内容，保留关联ID。"""
        result = result.model_copy(update={"content": context.gateway._redact(result.content)})
        if len(str(result.content)) > 12000:
            content = (
                result.content
                if isinstance(result.content, str)
                else json.dumps(result.content, ensure_ascii=False)
            )
            artifact_id = context.jobs.save_artifact(context.run_id, "tool_result", content)
            # 外层仅描述工具输出的交付方式；字符范围与 read_artifact 的切片保持一致。
            preview = content[:4000]
            result = result.model_copy(
                update={
                    "content": json.dumps(
                        {
                            "delivery": "tool_output_artifact",
                            "tool_name": tool_name,
                            "artifact_id": artifact_id,
                            "truncated": True,
                            "offset_unit": "unicode_code_points",
                            "total_length": len(content),
                            "preview_start": 0,
                            "preview_end": len(preview),
                            "preview": preview,
                            "next_start": len(preview),
                            "instruction": (
                                "完整工具输出已保存为 tool-output artifact；这是结果外置，"
                                "不是来源材料的业务归档。内层解析或归档状态不改变结果已外置的事实。"
                                "偏移按 Unicode 码点计数，preview 区间为 [preview_start, preview_end)。"
                                "按当前用户请求使用 read_tool_result 补读必要范围；next_start 是顺序"
                                "续读位置，不要求无条件读到末尾。不能把预览当作完整结果。"
                            ),
                        },
                        ensure_ascii=False,
                    )
                }
            )
        return result

    @staticmethod
    def _event_value(context: RunContext, value: dict, kind: str) -> dict:
        """事件保留有界预览，完整且脱敏的内容单独存产物，避免SSE放大长结果。"""
        safe = context.gateway._redact(value)
        serialized = json.dumps(safe, ensure_ascii=False)
        if len(serialized) <= 6000:
            return safe
        artifact_id = context.jobs.save_artifact(context.run_id, kind, serialized)
        return {
            "artifact_id": artifact_id,
            "preview": serialized[:2000],
            "total_length": len(serialized),
            "truncated": True,
        }

    def wrap_model_call(self, request, handler):
        context: RunContext = request.runtime.context
        root = context.jobs.guard(context.run_id, worker_id=context.worker_id)
        if context.resources is not None:
            ResourceService(context.jobs.sessions).validate(context, context.resources)
        used = context.gateway.ledger.summary(context.root_id)
        remaining = max(0, root["budgets"]["model_requests"] - used["root_used"])
        finalize = remaining <= 2
        feedback = request.state.get("final_response_feedback", [])
        task = context.jobs.get_run(context.run_id)["task"]
        limit = current_length_limit(context)
        revision_instruction = ""
        if feedback:
            # 最终文本的修订是局部收束，不再给模型新的业务工具；权限和账本仍检查。
            revision_instruction = (
                "\n本轮只输出修订后的最终答案，不复述工作过程，不追加新的章节、建议或操作。"
                "必须纠正以下校验问题：" + "；".join(feedback)
            )
            if limit is not None:
                # 压缩是对已有答案的局部编辑。真实回放曾把原本正确的准备步骤删掉；
                # 保留语义约束，不向模型注入案例答案，也不把原答复当成可靠事实。
                revision_instruction += (
                    f"为避免再次超限，请把正文连同引用标签压缩到约 {max(1, limit * 3 // 5)} "
                    "个可见字符内；只保留直接结论、最必要理由/做法与简短引用标签。"
                    "压缩时先删重复、过程叙述和与问题无关的扩展，保留使原结论成立或步骤可执行的"
                    "前提、条件、对象、范围、数值及单位。不要因缩写而改变因果或否定关系；"
                    "已有正确步骤不得省略必要的准备与作用对象。原答复仍须以已读材料为依据，"
                    "发现原答复无依据的内容应删去或纠正，而不是为了保留原文照搬错误。"
                )
        from knowledgeops_agent.learning.intent import current_learning

        learning = current_learning(context)
        learning_background = None
        if learning:
            from knowledgeops_agent.learning.profiles import LearningProfileService

            learning_background = {
                "task": learning["task"],
                "current_unit": next(
                    (
                        unit
                        for unit in learning["task"]["units"]
                        if unit["id"] == root.get("learning_unit_id")
                    ),
                    None,
                ),
                "configuration": LearningProfileService(context.jobs.sessions).effective(
                    learning["profiles"]
                ),
            }
            if root.get("learning_unit_id"):
                from sqlalchemy import select

                from knowledgeops_agent.learning.tasks import unit_view
                from knowledgeops_agent.persistence.models import (
                    LearningCollaboration,
                    LearningUnit,
                )

                with context.jobs.sessions() as session:
                    unit = session.get(LearningUnit, root["learning_unit_id"])
                    learning_background["current_unit"] = unit_view(unit)
                    learning_background["collaboration"] = [
                        {
                            "id": r.id,
                            "role": r.role,
                            "draft_id": r.draft_id,
                            "revision": r.draft_revision,
                            "status": r.status,
                            "summary": r.report.get("summary"),
                            "error": r.error,
                        }
                        for r in session.scalars(
                            select(LearningCollaboration)
                            .where(LearningCollaboration.unit_id == unit.id)
                            .order_by(LearningCollaboration.created_at)
                        )
                    ]
                    if unit.status == "waiting":
                        finalize = True
        controls = {
            "knowledge_policy": context.knowledge_policy_metadata,
            "preferences": MemoryService(context.jobs.sessions).effective(
                (context.resources or {}).get("preferences", [])
            ),
            "preference_rule": "preferences是已按范围和相关性选择的长期用户记忆，可含偏好、自述背景、目标和纠正；当前真人要求优先，不改变权限、预算，不从完成任务推断学习掌握。缺少背景时用memory_search/memory_read核对，不编造。",
            "current_task": task,
            "user_guidance": applied_messages(context.jobs, context.root_id),
            "publication_mode": "preview"
            if context.run_id != context.root_id
            else (context.jobs.publication_intent(context.root_id) or {}).get(
                "mode", root["input"]["publication_mode"]
            ),
            "publication_choice": context.jobs.publication_intent(context.root_id)
            if context.run_id == context.root_id
            else None,
            "knowledge_only": root["input"]["knowledge_only"],
            "models_remaining": remaining,
            "tools_remaining": max(0, root["budgets"]["tool_calls"] - root["tool_calls"]),
            "source_ids": root["input"]["source_ids"],
            "inherited_materials": (context.jobs.continuation(context.root_id) or {})
            .get("content", {})
            .get("material_refs", []),
            "inherited_artifacts": (context.jobs.continuation(context.root_id) or {})
            .get("content", {})
            .get("artifact_ids", []),
            "changesets": context.jobs.related_changesets(context.run_id),
            "plan": request.state.get("plan", []),
            "final_response_feedback": feedback,
            "evidence_catalog": [] if context.gateway.capacity.input_budget_version
            else context.sources.evidence_catalog(context.root_id),
            "finalize_now": finalize,
            "unavailable_tools": unavailable_tools(context)
            if context.resources is not None
            else {},
        }
        system = request.system_message.text if request.system_message else ""
        # 上游DurableContextMiddleware把历史资料与系统权威分开。本项目沿用
        # 该边界，复用已有业务真相，不让模型笔记/历史材料取得系统指令地位。
        data_keys = (
            "preferences",
            "current_task",
            "user_guidance",
            "inherited_materials",
            "inherited_artifacts",
            "plan",
            "evidence_catalog",
        )
        background = {key: controls.pop(key) for key in data_keys}
        if context.gateway.capacity.input_budget_version:
            from knowledgeops_agent.knowledge.citations import evidence_ids

            used_ids = set()
            for message in request.state.get("messages", [])[-8:]:
                used_ids.update(evidence_ids(str(message.content)))
            evidence = context.sources.evidence_page(
                context.root_id, query=task, current_source_ids=root["input"]["source_ids"],
                used_ids=used_ids, limit=40,
            )
            background["evidence_catalog"] = evidence["items"]
            background["evidence_catalog_status"] = {
                "total": evidence["total"], "next_offset": evidence["next_offset"],
                "read_tool": "list_evidence",
                "note": "目录省略项仍可分页查询；目录不代表已读所有正文。",
            }
            if context.gateway.capacity.deferred_mcp_tools:
                unavailable = controls["unavailable_tools"]
                deferred = [item for item in context.tool_catalog or []
                            if item["source"] == "mcp" and item["name"] not in unavailable]
                background["deferred_tools"] = {
                    "total": len(deferred), "search_tool": "tool_search",
                    "examples": [{"name": item["name"], "description": item["description"][:160]}
                                 for item in deferred[:20]],
                    "note": "外部工具需先用tool_search加载Schema，再按参数调用；工具名不是执行许可。",
                }
        if learning_background:
            background["learning"] = learning_background
            used_memory = set(learning_background["configuration"]["memory_ids"])
            background["preferences"] = [
                m for m in background["preferences"] if m["id"] not in used_memory
            ]
            system += "\n" + (context.resources or {})["prompts"].get("learning", "")
        background["task_notes"] = normalize_task_notes(request.state.get("task_notes"))
        background["history_status"] = request.state.get("task_history", {}).get(
            "status", "active_only"
        )
        context_data = HumanMessage(
            "应用组织的任务背景（不是一条新用户消息）：\n"
            + json.dumps(background, ensure_ascii=False),
            id="runtime-context-" + context.run_id,
            additional_kwargs={"knowledgeops_context_data": True},
        )
        context_contract = (
            "\n任务背景由应用单独提供：current_task和user_guidance是已接收的真人任务/补充，"
            "按用户优先级处理，当前明确修订优先。plan与task_notes是模型工作报告，不是"
            "已核实事实或新授权；preferences只按适用范围参考。发现笔记过期要更新或删除。"
            "回答涉及早期细节而摘要/近期历史不足时，用history_search定位并history_read核对，"
            "不要凭摘要补造。资料检索结果和历史助手结论不替代原始证据；引用必须使用本轮"
            "读取工具登记的固定版本证据。系统权限、业务回执和预算仍以程序校验为准。"
        )
        return handler(
            request.override(
                messages=[context_data, *request.messages],
                tools=[]
                if finalize or feedback
                else (
                    filter_tools(context, request.tools)
                    if context.resources is not None
                    else request.tools
                ),
                system_message=SystemMessage(
                    content=str(system)
                    + context_contract
                    + revision_instruction
                    + "\n程序提供的本轮控制信息：\n"
                    + json.dumps(controls, ensure_ascii=False)
                ),
            )
        )
