"""用户聊天的目录和窗口读取；模型完整历史仍由RunService读取。

展示修订与消息变更在同一事务提交。分页读取使用一致快照，旧游标遇到
新增消息或执行顺序变化必须重新定位，不能把不同时间的窗口直接拼接。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime

from sqlalchemy import func, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased, load_only

from knowledgeops_agent.app.lifecycle.access import require_conversation
from knowledgeops_agent.persistence.models import (
    ChatMessage,
    Conversation,
    ConversationIndexState,
    MessageDelivery,
    Run,
)


class CursorStale(ValueError):
    """展示已变化，客户端应以当前阅读锚点重新定位。"""


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def touch_directory(session) -> None:
    """使用PG原子递增；回滚不留下虚假的展示版本。"""
    session.execute(insert(ConversationIndexState).values(id=1, revision=1).on_conflict_do_update(
        index_elements=[ConversationIndexState.id], set_={"revision": ConversationIndexState.revision + 1},
    ))


def touch_messages(session, conversation_id: str, *, activity_at: datetime | None = None) -> None:
    values: dict = {"messages_revision": Conversation.messages_revision + 1}
    if activity_at is not None:
        values["activity_at"] = func.greatest(Conversation.activity_at, activity_at)
    session.execute(update(Conversation).where(Conversation.id == conversation_id).values(**values))
    if activity_at is not None:
        touch_directory(session)


def message_view(row) -> dict:
    return {"id": row.id, "run_id": row.run_id, "role": row.role, "content": row.content,
            "user_sequence": row.user_sequence, "created_at": row.created_at.isoformat()}


def ordered_messages(conversation_id: str):
    """排序合同唯一入口：自动轮次、排队真人消息及明确引导均保持原语义。"""
    target = aliased(Run)
    order = (func.coalesce(Run.execution_sequence, target.execution_sequence, 2147483647),
             ChatMessage.user_sequence, ChatMessage.role.desc(), ChatMessage.created_at, ChatMessage.id)
    query = (select(ChatMessage).join(Run, Run.id == ChatMessage.run_id)
             .outerjoin(MessageDelivery, MessageDelivery.message_id == ChatMessage.id)
             .outerjoin(target, target.id == MessageDelivery.target_root_id)
             .where(ChatMessage.conversation_id == conversation_id))
    return query, order


def _encode(scope: str, revision: int, start: int, size: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([1, scope, revision, start, size]).encode()).decode()


def _decode(cursor: str, scope: str, revision: int, limit: int) -> tuple[int, int]:
    try:
        if len(cursor) > 1024:
            raise ValueError()
        version, saved_scope, saved_revision, start, size = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (version != 1 or saved_scope != scope or any(type(n) is not int for n in (saved_revision, start, size))
                or not 0 <= start <= 2147483647 or not 1 <= size <= limit):
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise ValueError("分页参数无效，请重新打开列表。") from None
    if saved_revision != revision:
        raise CursorStale("内容或顺序已更新，正在重新定位当前阅读位置。")
    return start, size


def _literal_pattern(query: str) -> str:
    return "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class ConversationBrowser:
    def __init__(self, sessions):
        self.sessions = sessions

    def page(self, *, query: str = "", cursor: str | None = None, limit: int = 20) -> dict:
        query = query.strip()
        if not 1 <= limit <= 100 or len(query) > 200:
            raise ValueError("invalid conversation page parameters")
        scope = fingerprint(["directory-v1", query, limit])
        with self.sessions() as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            revision = session.scalar(select(ConversationIndexState.revision).where(ConversationIndexState.id == 1)) or 0
            start, size = _decode(cursor, scope, revision, limit) if cursor else (0, limit)
            criteria = [Conversation.deletion_state == "active", select(ChatMessage.id).where(
                ChatMessage.conversation_id == Conversation.id, ChatMessage.role == "user",
            ).exists()]
            pattern = _literal_pattern(query)
            if query:
                criteria.append(Conversation.title.ilike(pattern, escape="\\") | select(ChatMessage.id).where(
                    ChatMessage.conversation_id == Conversation.id, ChatMessage.content.ilike(pattern, escape="\\"),
                ).exists())
            total = session.scalar(select(func.count()).select_from(Conversation).where(*criteria)) or 0
            # 只返回当前页的短正文，不加载供应商协议；命中位置可直接定位到原消息。
            excerpt_start = func.greatest(1, func.strpos(func.lower(ChatMessage.content), query.lower()) - 60) if query else 1
            hit = select(ChatMessage.id.label("message_id"), func.substr(ChatMessage.content, excerpt_start, 240).label("excerpt")).where(
                ChatMessage.conversation_id == Conversation.id,
            )
            if query:
                hit = hit.where(ChatMessage.content.ilike(pattern, escape="\\"))
            match = hit.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(1).lateral()
            rows = session.execute(select(Conversation, match.c.message_id, match.c.excerpt).outerjoin(match, true()).where(*criteria)
                .order_by(Conversation.activity_at.desc().nullslast(), Conversation.created_at.desc(), Conversation.id.desc())
                .offset(start).limit(size)).all()
            return {"items": [{"id": row.id, "title": row.title, "selected_learning_task_id": row.selected_learning_task_id,
                              "activity_at": row.activity_at.isoformat() if row.activity_at else None,
                              "match_message_id": message_id if query else None, "excerpt": excerpt or ""}
                             for row, message_id, excerpt in rows],
                    "total": total, "revision": revision,
                    "next_cursor": _encode(scope, revision, start + size, limit) if start + size < total else None}

    def messages(self, conversation_id: str, *, cursor: str | None = None, anchor: str | None = None, limit: int = 40) -> dict:
        if not 1 <= limit <= 100 or (anchor and cursor):
            raise ValueError("invalid message page parameters")
        scope = fingerprint(["messages-v1", conversation_id, limit])
        with self.sessions() as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            conversation = require_conversation(session, conversation_id)
            revision = conversation.messages_revision
            query, order = ordered_messages(conversation_id)
            ranked = query.with_only_columns(ChatMessage.id, func.row_number().over(order_by=order).label("position")).subquery()
            total = session.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.conversation_id == conversation_id)) or 0
            start, size = _decode(cursor, scope, revision, limit) if cursor else (max(0, total - limit), limit)
            if anchor:
                position = session.scalar(select(ranked.c.position).where(ranked.c.id == anchor))
                if position is None:
                    raise KeyError("消息已不存在或不属于当前会话")
                start = max(0, min(position - 1 - limit // 2, total - limit))
            stop = min(total, start + size)
            rows = session.scalars(select(ChatMessage).options(load_only(ChatMessage.id, ChatMessage.run_id,
                ChatMessage.role, ChatMessage.content, ChatMessage.user_sequence, ChatMessage.created_at))
                .join(ranked, ranked.c.id == ChatMessage.id)
                .where(ranked.c.position > start, ranked.c.position <= stop).order_by(ranked.c.position)).all()
            return {"items": [message_view(row) for row in rows], "revision": revision, "total": total,
                    "anchor": anchor, "start": start,
                    "older_cursor": _encode(scope, revision, max(0, start - limit), min(limit, start)) if start else None,
                    "newer_cursor": _encode(scope, revision, stop, limit) if stop < total else None}
