"""生命周期读写边界。删除意图提交后即拒绝旧入口，物理清理可以随后重试。"""

from sqlalchemy import select

from knowledgeops_agent.persistence.models import Conversation, Run


class DetailsExpired(ValueError):
    """运行仍有简短身份，但完整轨迹/恢复位置或指定内部材料已经不可用。"""


def require_conversation(session, identity: str) -> Conversation:
    row = session.get(Conversation, identity)
    if row is None or row.deletion_state != "active":
        raise KeyError("conversation not found or deleted")
    return row


def require_readable_run(session, run: Run | None) -> Run:
    if run is None:
        raise KeyError("run not found")
    root = run if run.root_id == run.id else session.get(Run, run.root_id)
    if root is None or root.lifecycle_state == "deleted":
        raise KeyError("run not found or deleted")
    if root.conversation_id:
        require_conversation(session, root.conversation_id)
    return run


def require_writable_run(session, run: Run | None) -> Run:
    run = require_readable_run(session, run)
    root = run if run.root_id == run.id else session.get(Run, run.root_id)
    if run.lifecycle_state != "active" or root.lifecycle_state != "active":
        raise DetailsExpired("运行详情已过期或正在清理，不能继续写入或原地恢复；请创建新任务。")
    return run


def visible_runs(query):
    """子Run可能没有conversation_id，必须连同所属根一起从列表隐藏。"""
    hidden = select(Run.root_id).join(Conversation, Run.conversation_id == Conversation.id).where(
        Conversation.deletion_state != "active",
    ).correlate(None)
    return query.where(Run.lifecycle_state != "deleted", Run.root_id.not_in(hidden))


def mcp_scope_active(session, scope: str) -> bool:
    """跨Run主连接随聊天存活，子连接随真实子Run；池维护线程也复查这个事实。"""
    parts = scope.split(":")
    if len(parts) < 3 or parts[0] != "conversation":
        return False
    owner = session.get(Conversation, parts[1])
    fallback = None if owner is not None else session.get(Run, parts[1])
    if owner is not None:
        if owner.deletion_state != "active":
            return False
    elif fallback is None or fallback.lifecycle_state != "active":
        return False
    if parts[2:] == ["main"]:
        return True
    if len(parts) != 4 or parts[2] != "child":
        return False
    child = session.get(Run, parts[3])
    if child is None or child.parent_id is None or child.lifecycle_state != "active":
        return False
    root = session.get(Run, child.root_id)
    return bool(root and root.lifecycle_state == "active" and (root.conversation_id or root.id) == parts[1])
