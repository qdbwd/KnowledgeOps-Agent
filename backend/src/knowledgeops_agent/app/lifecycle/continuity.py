"""把继续聊天所需的少量事实与完整执行日志分开保存。"""

import hashlib

from langchain_core.messages import AIMessage, message_to_dict, messages_from_dict
from sqlalchemy import select

from knowledgeops_agent.knowledge.citations import (
    citation_placeholders,
    evidence_ids,
    render_answer_citations,
    render_citation_markers,
)
from knowledgeops_agent.persistence.models import ChatMessage, ChildNotification, Evidence, Run


def matches_visible(session, run_id: str, original: str, answer: str) -> bool:
    """只允许相同正文或已验证引用的确定性渲染，不能按相似度拼接模型协议。"""
    if original == answer:
        return True
    if session is None:
        return False
    marker_ids = {identity for marker in citation_placeholders(original) for identity in evidence_ids(marker)}
    verified = set()
    for identity in evidence_ids(original):
        row = session.get(Evidence, identity)
        if row is None or (identity in marker_ids and row.root_id != run_id):
            return False
        verified.add(identity)
    try:
        return answer in (render_citation_markers(original, verified), render_answer_citations(original, verified))
    except ValueError:
        return False


def message_payload(answer: str, continuation: dict | None, *, session=None, run_id: str = "") -> dict | None:
    """仅绑定确实生成了当前可见答复的原消息，不为程序回执编造模型协议。"""
    raw = (continuation or {}).get("final_assistant_message")
    if not isinstance(raw, dict):
        return None
    message = messages_from_dict([raw])[0]
    if not isinstance(message, AIMessage) or message.tool_calls or not matches_visible(session, run_id, message.text, answer):
        return None
    # 保留原始附属协议，只将可见正文换成已经证明的应用渲染结果；不保存完整请求。
    visible = message.model_copy(update={"content": answer})
    return {"version": 1, "visible_sha256": hashlib.sha256(answer.encode()).hexdigest(),
            "original_sha256": hashlib.sha256(message.text.encode()).hexdigest(), "message": message_to_dict(visible)}


def restore_message(content: str, payload: dict) -> AIMessage:
    """正文与协议必须绑定同一条答复；损坏时明确报错，不能猜测隐藏字段。"""
    if payload.get("version") != 1 or payload.get("visible_sha256") != hashlib.sha256(content.encode()).hexdigest():
        raise ValueError("conversation message protocol does not match visible content")
    message = messages_from_dict([payload["message"]])[0]
    if not isinstance(message, AIMessage) or message.tool_calls or message.text != content:
        raise ValueError("conversation protocol is not a final assistant message")
    return message


def plan_protocol_backfill(session, run: Run, records: list[dict]) -> tuple[dict[str, dict], list[str]]:
    """返回可证明的逐消息更新和阻塞原因，不修改旧记录。执行器在封闭清理前提交。

    多次恢复可能留下多条答复，必须逐条匹配该Run的成功响应；没有真实原响应
    时保留完整日志。只有明确标识的应用回执不需要供应商协议，不能猜测来源。
    """
    updates, blockers = {}, []
    for row in session.scalars(select(ChatMessage).where(ChatMessage.run_id == run.id, ChatMessage.role == "assistant")):
        if row.protocol_payload:
            try:
                restore_message(row.content, row.protocol_payload)
            except (ValueError, TypeError, KeyError):
                blockers.append(f"message:{row.id}:protocol_invalid")
            continue
        if run.outcome.get("answer_origin") == "application_receipt" and run.outcome.get("answer") == row.content:
            continue
        candidates = []
        for record in records:
            if record.get("run_id") != run.id or record.get("purpose") != "agent" or record.get("status") != "succeeded":
                continue
            try:
                original = AIMessage.model_validate(record.get("response"))
                payload = message_payload(row.content, {"final_assistant_message": message_to_dict(original)}, session=session, run_id=run.id)
            except (ValueError, TypeError, KeyError):
                continue
            if payload and payload not in candidates:
                candidates.append(payload)
        if len(candidates) == 1:
            updates[row.id] = candidates[0]
        else:
            # 同一可见答案对应不同隐藏字段也不能任意选取；逐消息来源不明先保护。
            blockers.append(f"message:{row.id}:protocol_unproven")
    return updates, blockers


def confirm_child_consumption(session, run: Run, continuation: dict | None, *, now) -> None:
    """与最终状态/延续提交同事务确认；未知ID和其他根的通知不能被顺带确认。"""
    if run.parent_id is not None:
        return
    seen = (continuation or {}).get("child_notifications_seen", [])
    if not seen:
        return
    for row in session.scalars(select(ChildNotification).where(
        ChildNotification.root_id == run.id, ChildNotification.id.in_(seen),
        ChildNotification.consumed_at.is_(None),
    )):
        row.consumed_at = now
