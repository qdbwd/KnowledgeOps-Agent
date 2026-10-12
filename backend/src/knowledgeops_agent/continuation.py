"""会话进展的不可变延续，不能变成恢复旧工具动作或继承发布许可的通道。"""

import json

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_to_dict,
)
from sqlalchemy import select

from knowledgeops_agent.context_management import working_messages
from knowledgeops_agent.db import (
    ChangeSet,
    ContinuationSnapshot,
    Conversation,
    Evidence,
    MessageDelivery,
    Run,
    RunArtifact,
)
from knowledgeops_agent.response_validation import OUTPUT_LIMIT_FAILURE, output_limit_reached
from knowledgeops_agent.run_resources import content_hash
from knowledgeops_agent.task_history import capture_history
from knowledgeops_agent.vendor.deerflow.task_continuity_state import normalize_task_notes


def complete_groups(messages: list) -> list:
    """保留完整请求/观察组；尚未完成的工具提议及孤立结果不进入新run。

    这些历史仅作为材料传给模型。新run总从新消息开始，不用旧checkpoint的
    next/interrupt位置。system控制信息重建，避免复制旧权限或运行预算。
    """
    result, index = [], 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, AIMessage) and message.tool_calls:
            expected = {item["id"] for item in message.tool_calls}
            group: list = [message]
            index += 1
            while index < len(messages) and isinstance(messages[index], ToolMessage):
                group.append(messages[index])
                index += 1
            if {
                item.tool_call_id for item in group[1:] if isinstance(item, ToolMessage)
            } == expected:
                result.extend(group)
            continue
        if not isinstance(message, (SystemMessage, ToolMessage)):
            result.append(message)
        index += 1
    return result


def from_state(context, state: dict) -> dict:
    """只提取工作摘要和完整历史；不复制State其他字段或服务依赖。"""
    messages = working_messages(state.get("messages", []), state.get("context_compaction"))
    capped = bool(messages and output_limit_reached(messages[-1]))
    if capped:
        last = messages[-1]
        if (
            last.tool_calls
            or last.invalid_tool_calls
            or last.additional_kwargs.get("tool_calls")
            or last.additional_kwargs.get("function_call")
        ):
            # 无效调用没有ToolMessage配对，也不得通过原供应商字段进入新请求。
            # 只筛选跨轮工作历史；原State、请求账本和已完成业务记录都保持原样。
            messages = messages[:-1]
    history = complete_groups(messages)
    if capped:
        # 纯文本片段保留为背景，同时明确失败。应用回执不是伪造的AI响应，
        # 也不是新增用户指令；下一任务不能把残缺片段当成完整答案继续推导。
        history.append(
            HumanMessage(
                "应用执行记录（不是新用户指令或模型响应，不授予操作权限）：\n"
                + OUTPUT_LIMIT_FAILURE,
                id="output-limit-receipt-" + context.run_id,
            )
        )
    return context.gateway._redact(
        {
            "messages": [message_to_dict(message) for message in history],
            "summary": state.get("working_summary", ""),
            "guidance_seen_sequence": state.get("guidance_seen_sequence", 0),
            "task_notes": normalize_task_notes(state.get("task_notes")),
            "task_history": capture_history(context, state, state.get("messages", []))
            if state.get("messages")
            else state.get("task_history", {}),
        }
    )


def snapshot_view(snapshot: ContinuationSnapshot) -> dict:
    if snapshot.format_version != 2 or content_hash(snapshot.content) != snapshot.content_hash:
        raise ValueError(
            "continuation snapshot format or hash is invalid; start a new conversation"
        )
    return {
        "id": snapshot.id,
        "run_id": snapshot.run_id,
        "generation": snapshot.generation,
        "parent_snapshot_id": snapshot.parent_snapshot_id,
        "covered_sequence": snapshot.covered_sequence,
        "content_hash": snapshot.content_hash,
        "content": snapshot.content,
    }


def save_snapshot(session, run: Run, data: dict | None) -> str | None:
    """与运行终态在同一事务保存并单调推进指针；旧任务恢复只保存分支结果。"""
    if run.parent_id or run.kind != "agent" or not run.conversation_id or run.format_version != 2:
        return None
    conversation = session.scalar(
        select(Conversation).where(Conversation.id == run.conversation_id).with_for_update()
    )
    previous = (
        session.get(ContinuationSnapshot, run.continuation_snapshot_id)
        if run.continuation_snapshot_id
        else None
    )
    prior = snapshot_view(previous)["content"] if previous else {}
    own_artifacts = list(
        session.scalars(
            select(RunArtifact.id)
            .join(Run, Run.id == RunArtifact.run_id)
            .where(Run.root_id == run.id)
        )
    )
    references = list(prior.get("material_refs", []))
    for evidence in session.scalars(select(Evidence).where(Evidence.root_id == run.id)):
        reference = (
            {"kind": "source_segment", "id": evidence.source_segment_id}
            if evidence.source_segment_id
            else {
                "kind": "knowledge_version",
                "id": evidence.knowledge_version_id,
                "start": evidence.start_offset,
                "end": evidence.end_offset,
            }
        )
        if reference not in references:
            references.append(reference)
    draft_refs = list(prior.get("draft_refs", []))
    for draft in session.scalars(
        select(ChangeSet).where(
            (ChangeSet.root_id == run.id) | (ChangeSet.publication_root_id == run.id)
        )
    ):
        draft_refs.append({"kind": "draft_revision", "id": draft.id, "revision": draft.revision})
    # 只白名单继承内容。权限、计数、next、pending tool call不属于快照合同。
    data = data or {}
    messages = data.get("messages", [])
    summary = data.get("summary", "")
    if not messages:
        # 纯审批和图构建前失败没有新的图状态，不能用空列表覆盖前序进展。
        # 本轮真人任务仍进入历史；确定性回执作为明确标识的材料，不伪造
        # AIMessage/reasoning_content，也不能用于重新取得发布许可。
        messages = [
            *prior.get("messages", []),
            message_to_dict(
                HumanMessage(
                    run.task,
                    id=("application-task-" if run.origin == "automatic" else "user-task-")
                    + run.id,
                    additional_kwargs={"knowledgeops_origin": run.origin},
                )
            ),
            message_to_dict(
                HumanMessage(
                    "应用执行记录（不是新用户指令或模型响应，不授予操作权限）：\n"
                    + json.dumps(
                        {
                            "run_id": run.id,
                            "status": run.status,
                            "reason": run.stop_reason,
                            "outcome": run.outcome,
                        },
                        ensure_ascii=False,
                    ),
                    id="application-receipt-" + run.id,
                )
            ),
        ]
        summary = prior.get("summary", "")
    # 只有已进入本次图状态的真人引导才计为已覆盖。数据库已接收但checkpoint
    # 尚未合并的消息不能被后续任务当作已经处理过。
    seen_guidance = max(
        session.scalars(
            select(MessageDelivery.user_sequence).where(
                MessageDelivery.target_root_id == run.id,
                MessageDelivery.status == "applied",
                MessageDelivery.user_sequence <= data.get("guidance_seen_sequence", 0),
            )
        ),
        default=0,
    )
    content = {
        "messages": messages,
        "summary": summary,
        "artifact_ids": sorted(set(prior.get("artifact_ids", []) + own_artifacts)),
        "material_refs": references,
        "draft_refs": draft_refs,
        "outcome": run.outcome,
        "task_notes": normalize_task_notes(data.get("task_notes", prior.get("task_notes")))
        if data.get("messages")
        else normalize_task_notes(prior.get("task_notes")),
        "task_history": data.get("task_history", prior.get("task_history", {}))
        if data.get("messages")
        else prior.get("task_history", {}),
    }
    snapshot = ContinuationSnapshot(
        conversation_id=run.conversation_id,
        run_id=run.id,
        generation=run.generation,
        parent_snapshot_id=run.continuation_snapshot_id,
        covered_sequence=max(
            previous.covered_sequence if previous else 0,
            run.conversation_sequence or 0,
            seen_guidance,
        ),
        content=content,
        content_hash=content_hash(content),
    )
    session.add(snapshot)
    session.flush()
    order = run.execution_sequence or run.conversation_sequence or 0
    if order >= conversation.latest_execution_sequence:
        conversation.latest_snapshot_id = snapshot.id
        conversation.latest_execution_sequence = order
        conversation.latest_root_sequence = max(
            conversation.latest_root_sequence, run.conversation_sequence or 0
        )
    return snapshot.id
