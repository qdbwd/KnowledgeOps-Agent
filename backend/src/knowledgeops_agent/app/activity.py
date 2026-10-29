"""全局任务观察与已读回执；不拥有执行、审批、恢复或内容保留权。"""

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased, load_only

from knowledgeops_agent.app.lifecycle.access import visible_runs
from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.core.pagination import page_rows
from knowledgeops_agent.persistence.models import (
    ApplicationActivityState,
    CleanupJob,
    ExtensionOperation,
    Run,
    RunReadReceipt,
    utcnow,
)


def _tick(session):
    statement = insert(ApplicationActivityState).values(id=1, revision=1)
    session.execute(statement.on_conflict_do_update(index_elements=[ApplicationActivityState.id],
        set_={"revision": ApplicationActivityState.revision + 1}))


def record_result(session, run: Run, kind: str, now) -> None:
    """终态/中断产生结果版本；子任务只在根的完成通知处计入，避免重复计数。

    与原状态事件同事务提交。活动查询和已读确认均不生成新版本，流式字块
    也不反复制造“新结果”。旧诊断事件清理不影响这两个最小展示字段。
    """
    _tick(session)
    if run.parent_id is None and kind in {"finished", "interrupted", "child_finished", "execution_reconciliation_required"}:
        run.result_version = (run.result_version or 0) + 1
        run.result_at = now


class ApplicationActivity:
    def __init__(self, sessions):
        self.sessions = sessions

    @staticmethod
    def _query():
        member = aliased(Run)
        flags = select(
            member.root_id.label("root_id"),
            func.bool_or(member.status.in_(["paused", "waiting_input", "failed", "budget_exhausted", "interrupted"])).label("attention"),
            func.bool_or(member.status.in_(["queued", "running", "waiting_children"])).label("active"),
        ).group_by(member.root_id).subquery()
        attention = and_(Run.lifecycle_state == "active", Run.status.not_in(["completed", "cancelled"]), flags.c.attention.is_(True))
        active = and_(Run.lifecycle_state == "active", Run.status.not_in(["completed", "cancelled"]), flags.c.active.is_(True))
        unread = Run.result_version > func.coalesce(RunReadReceipt.version, 0)
        query = visible_runs(select(Run).join(flags, flags.c.root_id == Run.id).outerjoin(RunReadReceipt, RunReadReceipt.run_id == Run.id)).where(
            Run.parent_id.is_(None), Run.user_deleted_at.is_(None))
        return query, {"attention": attention, "active": active, "unread": unread}

    @staticmethod
    def _bounded(session):
        # 多条摘要/计数查询观察同一快照，不把重连或中途状态变化拼成伪事实。
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        session.execute(text("SET LOCAL statement_timeout = '2000ms'"))

    @staticmethod
    def _counts(session, query, flags):
        return dict(session.execute(query.with_only_columns(
            func.count(Run.id).label("all"),
            *(func.count(Run.id).filter(condition).label(name) for name, condition in flags.items()),
        )).mappings().one())

    def summary(self, conversation_ids=()):
        ids = list(dict.fromkeys(conversation_ids))
        if len(ids) > 40 or any(len(value) > 36 for value in ids):
            raise ValueError("活动查询最多包含40个会话身份")
        with self.sessions() as session:
            self._bounded(session)
            query, flags = self._query()
            counts = self._counts(session, query, flags)
            conversations = {}
            if ids:
                rows = session.execute(query.where(Run.conversation_id.in_(ids)).with_only_columns(
                    Run.conversation_id,
                    *(func.bool_or(condition).label(name) for name, condition in flags.items()),
                ).group_by(Run.conversation_id)).mappings()
                conversations = {row["conversation_id"]: {name: row[name] for name in flags} for row in rows}
            clock = session.get(ApplicationActivityState, 1)
            return {"revision": clock.revision if clock else 0, "counts": counts,
                    "conversations": conversations, "management": self._management(session), "checked_at": utcnow()}

    def page(self, *, view="attention", cursor=None, limit=20):
        with self.sessions() as session:
            self._bounded(session)
            query, flags = self._query()
            if view not in {"all", *flags}:
                raise ValueError("未知活动视图")
            if view != "all":
                query = query.where(flags[view])
            page_query = query.options(load_only(Run.id, Run.root_id, Run.parent_id, Run.conversation_id, Run.kind,
                Run.task, Run.status, Run.outcome, Run.created_at, Run.result_at, Run.result_version, Run.lifecycle_state))
            rows, next_cursor = page_rows(session, page_query, Run, cursor=cursor, limit=limit)
            ids = [row.id for row in rows]
            receipts = dict(session.execute(select(RunReadReceipt.run_id, RunReadReceipt.version).where(RunReadReceipt.run_id.in_(ids))).all()) if ids else {}
            children = []
            reasons = {}
            if ids:
                # 关注原因覆盖整组，不能因为只展示最近三个子摘要而丢失更早的未决操作。
                reasons = {item["root_id"]: [name for name in self._reason_conditions() if item[name]]
                    for item in session.execute(select(Run.root_id,
                        *(func.bool_or(condition).label(name) for name, condition in self._reason_conditions().items()),
                    ).where(Run.root_id.in_(ids)).group_by(Run.root_id)).mappings()}
                # 每个根只传最近三个子摘要和总数；完整任务活动仍从原详情页读取。
                ranked = select(Run.id, Run.root_id, Run.status,
                    func.substr(Run.task, 1, 160).label("task"), func.substr(Run.outcome["answer"].astext, 1, 500).label("answer"),
                    func.row_number().over(partition_by=Run.root_id, order_by=(Run.created_at.desc(), Run.id.desc())).label("position"),
                    func.count().over(partition_by=Run.root_id).label("total"),
                ).where(Run.root_id.in_(ids), Run.parent_id.is_not(None)).subquery()
                children = list(session.execute(select(ranked).where(ranked.c.position <= 3).order_by(ranked.c.root_id, ranked.c.position)).mappings())
            facts = dict((row["id"], row) for row in session.execute(query.where(Run.id.in_(ids)).with_only_columns(
                Run.id, *(value.label(name) for name, value in flags.items()),
            )).mappings()) if ids else {}
            items = []
            for row in rows:
                members = [dict(child) for child in children if child["root_id"] == row.id]
                items.append({"id": row.id, "parent_id": None, "conversation_id": row.conversation_id, "kind": row.kind,
                    "task": row.task[:160], "status": row.status, "created_at": row.created_at,
                    "answer": str(row.outcome.get("answer") or "")[:600], "result_version": row.result_version,
                    "result_at": row.result_at, "read_version": receipts.get(row.id, 0),
                    **{name: facts[row.id][name] for name in flags},
                    "attention_reasons": sorted(reasons.get(row.id, [])) if facts[row.id]["attention"] else [],
                    "children": members, "child_count": members[0]["total"] if members else 0,
                    "href": f"/runs/{row.id}"})
            return {"items": items, "next_cursor": next_cursor}

    @staticmethod
    def _reason_conditions():
        kind = func.coalesce(Run.outcome["kind"].astext, "")
        return {
            "approval": and_(Run.status == "interrupted", kind == "waiting_approval"),
            "unknown": and_(Run.status == "interrupted", kind == "execution_unknown"),
            "input": or_(Run.status == "waiting_input", and_(Run.status == "interrupted", kind == "waiting_input")),
            "paused": Run.status == "paused",
            "recovery": or_(Run.status.in_(["failed", "budget_exhausted"]),
                and_(Run.status == "interrupted", kind.not_in(["waiting_approval", "execution_unknown", "waiting_input"]))),
        }

    def mark_read(self, root_id, version):
        if not isinstance(version, int) or not 0 <= version <= 2**63 - 1:
            raise ValueError("无效结果版本")
        with self.sessions.begin() as session:
            # 与删除/执行写入沿用相同锁顺序；只推进回执，不改Run状态/权限/预算。
            lock_control(session)
            query, _ = self._query()
            root = session.scalar(query.where(Run.id == root_id))
            if root is None:
                raise KeyError("run not found or removed")
            if version > root.result_version:
                raise ValueError("结果版本不存在，请刷新后核对")
            statement = insert(RunReadReceipt).values(run_id=root_id, version=version, updated_at=utcnow())
            statement = statement.on_conflict_do_update(index_elements=[RunReadReceipt.run_id],
                set_={"version": statement.excluded.version, "updated_at": statement.excluded.updated_at},
                where=RunReadReceipt.version < statement.excluded.version).returning(RunReadReceipt.version)
            if session.execute(statement).scalar_one_or_none() is not None:
                _tick(session)
            receipt = session.get(RunReadReceipt, root_id)
            return {"id": root_id, "read_version": receipt.version, "result_version": root.result_version,
                    "unread": receipt.version < root.result_version}

    @staticmethod
    def _management_query(kind, target_kind=None):
        if kind == "mcp":
            if target_kind is not None:
                raise ValueError("MCP操作不接受资产筛选")
            return select(ExtensionOperation).where(or_(
                and_(ExtensionOperation.kind == "mcp_candidate", ExtensionOperation.status.in_([
                    "failed", "interrupted", "queued", "preparing", "testing"])),
                and_(ExtensionOperation.kind == "mcp_cleanup", ExtensionOperation.status.in_([
                    "failed", "interrupted", "queued", "running", "waiting"])))), ExtensionOperation
        if kind != "deletion":
            raise ValueError("未知后台操作类型")
        # 自动到期清理不冒充用户删除；会话删除失败仍由原删除接口接续。
        query = select(CleanupJob).where(CleanupJob.status == "failed", or_(
            CleanupJob.target_kind.in_(["conversation", "source", "delivery", "draft", "knowledge"]),
            and_(CleanupJob.target_kind == "run", CleanupJob.summary["user_requested"].as_boolean().is_(True))))
        if target_kind is not None:
            if target_kind not in {"conversation", "source", "delivery", "draft", "knowledge", "run"}:
                raise ValueError("未知删除对象")
            query = query.where(CleanupJob.target_kind == target_kind)
        return query, CleanupJob

    @staticmethod
    def _management_item(row):
        if isinstance(row, ExtensionOperation):
            cleanup = row.kind == "mcp_cleanup"
            return {"id": row.id, "kind": "mcp", "status": row.status,
                "label": "工具服务清理" if cleanup else "工具服务准备",
                "href": f"/mcp?{'cleanup' if cleanup else 'operation'}={row.id}",
                "attention": row.status in {"failed", "interrupted"}}
        paths = {"conversation": "/conversations", "knowledge": "/knowledge", "run": "/runs"}
        identity = row.target_id if row.target_kind == "conversation" else row.id
        return {"id": row.id, "target_id": row.target_id if row.target_kind == "conversation" else None,
            "kind": "deletion", "status": row.status, "attention": True,
            "label": f"后台删除未完成：{str(row.summary.get('title') or '已移除的内容')[:160]}",
            "href": paths.get(row.target_kind, "/sources") + f"?deletion={identity}"}

    def _management(self, session):
        items, attention, total = [], 0, 0
        for kind in ("mcp", "deletion"):
            query, model = self._management_query(kind)
            total += int(session.scalar(query.with_only_columns(func.count(model.id))) or 0)
            failures = query.where(model.status.in_(["failed", "interrupted"]))
            attention += int(session.scalar(failures.with_only_columns(func.count(model.id))) or 0)
            items.extend(self._management_item(row) for row in session.scalars(query.order_by(model.created_at.desc(), model.id.desc()).limit(10)))
        return {"attention": attention, "items": items, "truncated": total > len(items)}

    def management_page(self, *, kind, target_kind=None, cursor=None, limit=20):
        """摘要之外的旧失败必须可达；只投影原作业，不创建新的管理状态。"""
        with self.sessions() as session:
            self._bounded(session)
            query, model = self._management_query(kind, target_kind)
            rows, next_cursor = page_rows(session, query, model, cursor=cursor, limit=limit)
            return {"items": [self._management_item(row) for row in rows], "next_cursor": next_cursor}
