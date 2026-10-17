"""项目模型调用的唯一入口：先持久化占额，再调用，最后记录可核验结果。

调用回执使用独立 PostgreSQL 数据库，供任务恢复和协议回放，不再累计项目用量。
一个失败、超时或中途终止的请求也消耗根任务预算；reserved 代表结果未知。
模型内部隐藏重试必须关闭，否则一次预占可能对应多次实际请求。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, message_chunk_to_message
from langchain_core.utils.function_calling import convert_to_openai_tool
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    func,
    inspect,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from knowledgeops_agent.harness.context.capacity import ModelCapacity

accounting_metadata = MetaData()
ACCOUNTING_SCOPE = "knowledgeops-v1-2026-10-08"
quotas = Table(
    "model_quotas",
    accounting_metadata,
    Column("scope", String(120), primary_key=True),
    # 保留旧表形状以兼容历史迁移与外键；当前应用不读写累计统计。
    Column("total_limit", Integer, nullable=True),
    Column("used", Integer, nullable=False, default=0),
    Column("canary_used", Integer, nullable=False, default=0),
    CheckConstraint(
        "total_limit IS NULL OR total_limit > 0",
        name="model_quotas_total_limit_check",
    ),
    CheckConstraint("used >= 0 AND used <= total_limit"),
    CheckConstraint("canary_used >= 0 AND canary_used <= 10"),
)
roots = Table(
    "model_root_budgets",
    accounting_metadata,
    Column("scope", ForeignKey("model_quotas.scope"), primary_key=True),
    Column("root_id", String(120), primary_key=True),
    Column("request_limit", Integer, nullable=False),
    Column("used", Integer, nullable=False, default=0),
    Column("closed_at", DateTime(timezone=True)),
    CheckConstraint("request_limit > 0 AND used >= 0 AND used <= request_limit"),
)
requests = Table(
    "model_requests",
    accounting_metadata,
    Column("id", String(36), primary_key=True),
    Column("scope", ForeignKey("model_quotas.scope"), nullable=False, index=True),
    Column("root_id", String(120), nullable=False, index=True),
    Column("run_id", String(120), nullable=False, index=True),
    Column("purpose", String(40), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("status", String(20), nullable=False),
    Column("request", JSON, nullable=False),
    Column("response", JSON),
    Column("usage", JSON),
    Column("error", Text),
    Column("duration_ms", Float),
    Column("first_token_ms", Float),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
)
accounting_migrations = Table(
    "accounting_migrations",
    accounting_metadata,
    Column("version", String(80), primary_key=True),
    Column("scope", String(120), primary_key=True),
    Column("details", JSON, nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False),
)
call_links = Table(
    "model_call_links",
    accounting_metadata,
    Column("scope", String(120), primary_key=True),
    Column("run_id", String(120), primary_key=True),
    Column("call_id", String(200), primary_key=True),
    Column("input_hash", String(64), nullable=False),
    Column("reservation_id", ForeignKey("model_requests.id"), nullable=False, unique=True),
)


class ModelBudgetExceeded(RuntimeError):
    """请求尚未发出，根任务预算已经不足。"""


class ModelCallsDisabled(RuntimeError):
    """配置未开启真实调用，不能因客户端存在就产生费用。"""


class ModelCallFailed(RuntimeError):
    """供应商调用或协议失败；对外错误已经移除已知密钥。"""


class ModelReplayConflict(ModelCallFailed):
    """同一调用ID不能绑定不同输入；恢复时拒绝悄悄替换原请求。"""


class ModelCallUncertain(ModelCallFailed):
    """请求已预占但结果未知；不能自动重发或退款。"""


def prepare_accounting(engine: Engine) -> None:
    """幂等建立独立账本，数据库锁避免多服务同时首次启动的 DDL 竞争。

    该函数只创建缺失表，既不删除记录也不重置额度。业务 Alembic 迁移和
    测试数据清理不能访问此独立数据库；以后改表结构需要明确迁移。
    """
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(748616001)"))
        accounting_metadata.create_all(connection)


def upgrade_accounting_lifecycle(engine: Engine, *, scope: str = ACCOUNTING_SCOPE) -> dict:
    """显式增加根任务封闭字段；不封闭旧根、不删除请求，也不改变已有预算。"""
    if engine.url.database not in {"knowledgeops_accounting", "knowledgeops_test"}:
        raise ValueError("accounting migration requires the isolated accounting database")
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(748616001)"))
        accounting_metadata.create_all(connection)
        if "closed_at" not in {column["name"] for column in inspect(connection).get_columns("model_root_budgets")}:
            connection.execute(text("ALTER TABLE model_root_budgets ADD COLUMN closed_at TIMESTAMP WITH TIME ZONE"))
        result = {"version": "0003_lifecycle", "scope": scope, "closed_old_roots": 0}
        connection.execute(insert(accounting_migrations).values(
            version="0003_lifecycle", scope=scope, details=result, applied_at=datetime.now(UTC),
        ).on_conflict_do_nothing())
        return result


def upgrade_accounting_budget(
    engine: Engine, *, scope: str = ACCOUNTING_SCOPE, target_limit: int = 1200
) -> dict:
    """显式执行已批准的1200、1600或2000累计额度迁移；普通启动不扩额。

    输入是独立账本连接及原scope，输出迁移前后计数。约束、额度和迁移
    回执同事务提交，失败整体回滚；再次执行只复核，不重置消费或canary。
    旧表结构必须可识别，未知scope不能被当成绕过历史账本的新额度。
    target_limit保留1200默认值兼容历史迁移；更高额度必须显式传入。
    正式执行前应停止业务写入并备份；隔离测试库也允许验证同一迁移。
    """
    versions = {
        1200: "0001_budget_1200",
        1600: "0003_budget_1600",
        2000: "0004_budget_2000",
    }
    if target_limit not in versions:
        raise ValueError("target limit has no approved migration")
    if engine.url.database not in {"knowledgeops_accounting", "knowledgeops_test"}:
        raise ValueError("accounting migration requires the isolated accounting database")
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(748616001)"))
        accounting_metadata.create_all(connection)
        quota = (
            connection.execute(select(quotas).where(quotas.c.scope == scope).with_for_update())
            .mappings()
            .one_or_none()
        )
        if quota is None:
            empty = connection.scalar(select(quotas.c.scope).limit(1)) is None
            if not empty or scope != ACCOUNTING_SCOPE:
                raise ValueError("unknown accounting scope; refusing to create fresh allowance")
        elif quota["total_limit"] not in {600, *versions}:
            raise ValueError("existing scope has an unexpected approved limit")
        if quota and quota["total_limit"] > target_limit:
            raise ValueError("accounting limit downgrade refused")
        constraints = inspect(connection).get_check_constraints("model_quotas")
        bounds = [
            c
            for c in constraints
            if re.search(r"total_limit\s*<=\s*(600|1200|1600|2000)\b", c["sqltext"])
        ]
        optional_bound = any(
            c["name"] == "model_quotas_total_limit_check"
            and re.search(r"total_limit\s+IS\s+NULL", c["sqltext"], re.IGNORECASE)
            for c in constraints
        )
        if len(bounds) > 1 or (not bounds and not optional_bound):
            raise ValueError("unrecognized accounting limit constraint; migration refused")
        bound = bounds[0] if bounds else None
        match = (
            re.search(r"total_limit\s*<=\s*(600|1200|1600|2000)\b", bound["sqltext"])
            if bound
            else None
        )
        if bound and match and int(match.group(1)) < target_limit:
            constraint_name = bound["name"]
            if not constraint_name:
                raise ValueError("accounting limit constraint has no stable name")
            name = connection.dialect.identifier_preparer.quote(constraint_name)
            connection.execute(text(f"ALTER TABLE model_quotas DROP CONSTRAINT {name}"))
            connection.execute(
                text(
                    "ALTER TABLE model_quotas ADD CONSTRAINT model_quotas_total_limit_check "
                    f"CHECK(total_limit > 0 AND total_limit <= {target_limit})"
                )
            )
        previous_limit = quota["total_limit"] if quota else 0
        if quota:
            connection.execute(
                update(quotas).where(quotas.c.scope == scope).values(total_limit=target_limit)
            )
        else:
            connection.execute(
                quotas.insert().values(scope=scope, total_limit=target_limit, used=0, canary_used=0)
            )
        result = {
            "scope": scope,
            "previous_limit": previous_limit,
            "total_limit": target_limit,
            "used": quota["used"] if quota else 0,
            "canary_used": quota["canary_used"] if quota else 0,
        }
        connection.execute(
            insert(accounting_migrations)
            .values(
                version=versions[target_limit],
                scope=scope,
                details=result,
                applied_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing()
        )
        return result


def remove_accounting_budget_limit(engine: Engine, *, scope: str = ACCOUNTING_SCOPE) -> dict:
    """显式取消既有scope的累计上限，保留请求、计数、根预算和旧回执。

    只用于本次用户授权的运维操作；普通启动不修改既有预算。先备份并
    停止账本写入，再在一个事务中放宽旧表约束并将total_limit置空。
    不创建替代scope，不重置历史，也不取消单任务或首次接通保护。
    """
    if engine.url.database not in {"knowledgeops_accounting", "knowledgeops_test"}:
        raise ValueError("accounting migration requires the isolated accounting database")
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(748616001)"))
        quota = (
            connection.execute(select(quotas).where(quotas.c.scope == scope).with_for_update())
            .mappings()
            .one_or_none()
        )
        if quota is None:
            raise ValueError("unknown accounting scope; refusing to create fresh allowance")
        constraints = inspect(connection).get_check_constraints("model_quotas")
        bound = next(
            (c for c in constraints if c["name"] == "model_quotas_total_limit_check"), None
        )
        if bound is None or not re.search(r"total_limit\s*>\s*0", bound["sqltext"]):
            raise ValueError("unrecognized accounting limit constraint; migration refused")
        connection.execute(text("ALTER TABLE model_quotas ALTER COLUMN total_limit DROP NOT NULL"))
        connection.execute(
            text("ALTER TABLE model_quotas DROP CONSTRAINT model_quotas_total_limit_check")
        )
        connection.execute(
            text(
                "ALTER TABLE model_quotas ADD CONSTRAINT model_quotas_total_limit_check "
                "CHECK(total_limit IS NULL OR total_limit > 0)"
            )
        )
        connection.execute(update(quotas).where(quotas.c.scope == scope).values(total_limit=None))
        result = {
            "scope": scope,
            "previous_limit": quota["total_limit"],
            "total_limit": None,
            "used": quota["used"],
            "canary_used": quota["canary_used"],
        }
        connection.execute(
            insert(accounting_migrations)
            .values(
                version="0005_remove_total_limit",
                scope=scope,
                details=result,
                applied_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing()
        )
        return result


def _effective_total_limit(*limits: int | None) -> int | None:
    """None不参与取最小值；只要仍有有限配置，就继续遵守该有限预算。"""
    return min((limit for limit in limits if limit is not None), default=None)


def retire_project_usage(engine: Engine, *, scope: str = ACCOUNTING_SCOPE) -> dict:
    """清除已废弃的项目累计值；不删除任务预算、模型回执或重放关联。

    仅在用户授权的迁移中执行。要求已完成取消总上限的旧迁移，事务内
    锁定原scope并清零聚合字段；重复执行安全，不另建scope或重置任务。
    新回执只记录退役动作，不再保存一份被清除的累计用量。
    """
    if engine.url.database not in {"knowledgeops_accounting", "knowledgeops_test"}:
        raise ValueError("accounting migration requires the isolated accounting database")
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(748616001)"))
        quota = (
            connection.execute(select(quotas).where(quotas.c.scope == scope).with_for_update())
            .mappings()
            .one_or_none()
        )
        if quota is None or quota["total_limit"] is not None:
            raise ValueError("remove the existing total limit before retiring project usage")
        connection.execute(
            update(quotas).where(quotas.c.scope == scope).values(used=0, canary_used=0)
        )
        result = {"scope": scope, "project_usage_retired": True}
        connection.execute(
            insert(accounting_migrations)
            .values(
                version="0006_retire_project_usage",
                scope=scope,
                details=result,
                applied_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing()
        )
    return result


def upgrade_accounting_call_links(engine: Engine) -> dict:
    """新增调用关联表并记录显式迁移，不为历史请求补造调用ID或覆盖费用。"""
    if engine.url.database not in {"knowledgeops_accounting", "knowledgeops_test"}:
        raise ValueError("accounting migration requires the isolated accounting database")
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(748616001)"))
        accounting_metadata.create_all(connection)
        quota = (
            connection.execute(select(quotas).where(quotas.c.scope == ACCOUNTING_SCOPE))
            .mappings()
            .one()
        )
        result = {
            "scope": ACCOUNTING_SCOPE,
            "used": quota["used"],
            "total_limit": quota["total_limit"],
            "version": "0002_call_links",
        }
        connection.execute(
            insert(accounting_migrations)
            .values(
                version="0002_call_links",
                scope=ACCOUNTING_SCOPE,
                details=result,
                applied_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing()
        )
        return result


class ModelLedger:
    """跨进程共享根任务预算与调用回执，不持有预算的内存副本。

    legacy_project_accounting 仅用于旧迁移的兼容性验证；正常应用使用默认
    关闭值，不再维护或返回项目累计用量，即使旧库中还有累计字段。
    """

    def __init__(
        self,
        engine: Engine,
        *,
        scope: str = ACCOUNTING_SCOPE,
        total_limit: int | None = None,
        legacy_project_accounting: bool = False,
    ) -> None:
        if total_limit is not None and total_limit <= 0:
            raise ValueError("total_limit must be positive or None")
        if legacy_project_accounting and engine.url.database != "knowledgeops_test":
            raise ValueError("legacy project accounting is only for isolated migration tests")
        if total_limit is not None and not legacy_project_accounting:
            raise ValueError("project-wide limits are retired; configure the root task budget")
        self.engine = engine
        self.scope = scope
        self.total_limit = total_limit
        self.legacy_project_accounting = legacy_project_accounting

    def pending_root_calls(self, root_id: str) -> list[str]:
        """清理资格只读接口；未确认结局的预占不能随终态任务一起遗忘。"""
        with self.engine.connect() as connection:
            return list(connection.scalars(select(requests.c.id).where(
                requests.c.scope == self.scope,
                requests.c.root_id == root_id,
                or_(requests.c.status.not_in(["succeeded", "failed"]), requests.c.finished_at.is_(None)),
            ).order_by(requests.c.id)))

    def settled_root_at(self, root_id: str) -> datetime | None:
        """保留期也要覆盖晚结算的请求，不以Run更早的结束时刻替代。"""
        with self.engine.connect() as connection:
            return connection.scalar(select(func.max(requests.c.finished_at)).where(
                requests.c.scope == self.scope, requests.c.root_id == root_id,
            ))

    def seal_root(self, root_id: str) -> None:
        """与预占使用同一scope锁；未决请求阻止清理，封闭后任何旧调用都不能重做。

        即使没有调用过模型也创建最小封闭身份，避免删除请求后重新获得一份额度。
        业务库应先封闭运行再调用这里；跨库失败时由清理作业重试，不重新开放运行。
        """
        if not root_id:
            raise ValueError("root identity is required")
        with self.engine.begin() as connection:
            quota_query = select(quotas).where(quotas.c.scope == self.scope).with_for_update()
            if connection.execute(quota_query).first() is None:
                connection.execute(insert(quotas).values(scope=self.scope, total_limit=None, used=0, canary_used=0).on_conflict_do_nothing())
                connection.execute(quota_query).one()
            pending = connection.scalar(select(requests.c.id).where(
                requests.c.scope == self.scope, requests.c.root_id == root_id,
                or_(requests.c.status.not_in(["succeeded", "failed"]), requests.c.finished_at.is_(None)),
            ).limit(1))
            if pending:
                raise ValueError("unsettled model request prevents cleanup")
            connection.execute(insert(roots).values(
                scope=self.scope, root_id=root_id, request_limit=1, used=0,
            ).on_conflict_do_nothing())
            connection.execute(update(roots).where(
                roots.c.scope == self.scope, roots.c.root_id == root_id, roots.c.closed_at.is_(None),
            ).values(closed_at=datetime.now(UTC)))

    def purge_root_details(self, root_id: str, *, limit: int = 100) -> dict:
        """已封闭根的请求/响应按批删除；先删关联再删正文，保留防重做和预算事实。"""
        if not 1 <= limit <= 1000:
            raise ValueError("batch limit must be between 1 and 1000")
        with self.engine.begin() as connection:
            root = connection.execute(select(roots).where(
                roots.c.scope == self.scope, roots.c.root_id == root_id,
            ).with_for_update()).mappings().one_or_none()
            if root is None or root["closed_at"] is None:
                raise ValueError("root must be sealed before removing model details")
            ids: list[str] = list(connection.scalars(select(requests.c.id).where(
                requests.c.scope == self.scope, requests.c.root_id == root_id,
            ).order_by(requests.c.id).limit(limit + 1)))
            batch = ids[:limit]
            if batch:
                connection.execute(delete(call_links).where(call_links.c.scope == self.scope, call_links.c.reservation_id.in_(batch)))
                connection.execute(delete(requests).where(requests.c.scope == self.scope, requests.c.id.in_(batch)))
            return {"deleted": len(batch), "remaining": len(ids) > limit}

    def reserve(
        self,
        root_id: str,
        run_id: str,
        purpose: str,
        request: list | dict,
        *,
        root_limit: int = 40,
    ) -> str:
        """兼容普通请求入口，每次调用代表一个新的实际尝试。"""
        return self.reserve_call(root_id, run_id, purpose, request, root_limit=root_limit)[0]

    def reserve_call(
        self,
        root_id: str,
        run_id: str,
        purpose: str,
        request: list[dict[str, Any]] | dict[str, Any],
        *,
        root_limit: int = 40,
        root_reserve: int = 0,
        call_id: str | None = None,
        input_hash: str | None = None,
    ) -> tuple[str, bool]:
        """原子预占根任务预算，提交事务后才能接触供应商。

        锁顺序固定为scope→根任务，子 Agent 并发不会超卖或产生反向死锁。
        scope行保留为旧外键的身份锚点；正常调用不会更新其中的累计字段。
        返回预占ID及是否新尝试。调用关联在同一额度锁下查询/插入，即使
        恢复与原进程并发也只能有一次供应商尝试；未知结果不自动再发。
        """
        if root_limit <= 0 or root_reserve < 0 or not root_id or not run_id:
            raise ValueError("positive root limit and nonempty run IDs are required")
        if call_id is not None and (not call_id or len(call_id) > 200 or not input_hash):
            raise ValueError("call identity requires a bounded nonempty ID and input hash")
        reservation = str(uuid4())
        with self.engine.begin() as connection:
            quota_query = select(quotas).where(quotas.c.scope == self.scope).with_for_update()
            quota = connection.execute(quota_query).mappings().one_or_none()
            if quota is None:
                # PostgreSQL先检查CHECK再处理ON CONFLICT。已存在旧额度时不能先
                # 尝试插入更高的新默认值，否则尚未显式迁移就会触发旧约束失败。
                # 新scope首次初始化仍用唯一键处理并发，随后重新锁定实际额度。
                connection.execute(
                    insert(quotas)
                    .values(
                        scope=self.scope,
                        total_limit=self.total_limit if self.legacy_project_accounting else None,
                        used=0,
                        canary_used=0,
                    )
                    .on_conflict_do_nothing()
                )
                quota = connection.execute(quota_query).mappings().one()
            # 封闭身份先于旧call_id重放检查。完整回执已清理时也不能重新预占。
            closed_at = connection.scalar(select(roots.c.closed_at).where(
                roots.c.scope == self.scope, roots.c.root_id == root_id,
            ))
            if closed_at is not None:
                raise ModelCallFailed("root execution details expired; start a new task")
            if call_id is not None:
                prior = (
                    connection.execute(
                        select(call_links).where(
                            call_links.c.scope == self.scope,
                            call_links.c.run_id == run_id,
                            call_links.c.call_id == call_id,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if prior:
                    if prior["input_hash"] != input_hash:
                        raise ModelReplayConflict("same model call ID has different input")
                    return prior["reservation_id"], False
            if self.legacy_project_accounting:
                total_limit = _effective_total_limit(quota["total_limit"], self.total_limit)
                if total_limit is not None and quota["used"] >= total_limit:
                    raise ModelBudgetExceeded("project total model request budget exhausted")
                if purpose == "canary" and quota["canary_used"] >= 10:
                    raise ModelBudgetExceeded("initial canary request budget exhausted")
            connection.execute(
                insert(roots)
                .values(scope=self.scope, root_id=root_id, request_limit=root_limit, used=0)
                .on_conflict_do_nothing()
            )
            root = (
                connection.execute(
                    select(roots)
                    .where(roots.c.scope == self.scope, roots.c.root_id == root_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if root["used"] >= max(0, min(root["request_limit"], root_limit) - root_reserve):
                raise ModelBudgetExceeded("root model request budget exhausted")
            if self.legacy_project_accounting:
                connection.execute(
                    update(quotas)
                    .where(quotas.c.scope == self.scope)
                    .values(
                        used=quota["used"] + 1,
                        canary_used=quota["canary_used"] + int(purpose == "canary"),
                    )
                )
                ordinal = quota["used"] + 1
            else:
                # 只保持同根任务内的调用顺序；兼容曾使用全局序号的旧任务。
                # 不能直接用root.used+1，否则恢复后新请求可能排在旧请求之前。
                ordinal = 1 + (
                    connection.scalar(
                        select(func.max(requests.c.ordinal)).where(
                            requests.c.scope == self.scope, requests.c.root_id == root_id
                        )
                    )
                    or 0
                )
            connection.execute(
                update(roots)
                .where(roots.c.scope == self.scope, roots.c.root_id == root_id)
                .values(used=root["used"] + 1)
            )
            connection.execute(
                requests.insert().values(
                    id=reservation,
                    scope=self.scope,
                    root_id=root_id,
                    run_id=run_id,
                    purpose=purpose,
                    ordinal=ordinal,
                    status="reserved",
                    request=request,
                    created_at=datetime.now(UTC),
                )
            )
            if call_id is not None:
                connection.execute(
                    call_links.insert().values(
                        scope=self.scope,
                        run_id=run_id,
                        call_id=call_id,
                        input_hash=input_hash,
                        reservation_id=reservation,
                    )
                )
        return reservation, True

    def record(self, reservation: str) -> dict:
        """恢复只读该scope对应的真实预占，不接受外部响应作为成功回执。"""
        with self.engine.connect() as connection:
            return dict(
                connection.execute(
                    select(requests).where(
                        requests.c.id == reservation,
                        requests.c.scope == self.scope,
                    )
                )
                .mappings()
                .one()
            )

    def linked_record(self, run_id: str, call_id: str) -> dict | None:
        """只读稳定调用身份对应的尝试；不预占额度，也不把未知请求标为失败。"""
        with self.engine.connect() as connection:
            row = connection.execute(select(requests).join(
                call_links, call_links.c.reservation_id == requests.c.id,
            ).where(
                call_links.c.scope == self.scope,
                call_links.c.run_id == run_id,
                call_links.c.call_id == call_id,
            )).mappings().one_or_none()
            return dict(row) if row else None

    def finish(
        self,
        reservation: str,
        *,
        response: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
        duration_ms: float | None = None,
        first_token_ms: float | None = None,
    ) -> None:
        """只终结一次预占，不覆盖已确认结果；没有 usage 就保留未知。"""
        with self.engine.begin() as connection:
            connection.execute(
                update(requests)
                .where(
                    requests.c.id == reservation,
                    requests.c.scope == self.scope,
                    requests.c.status == "reserved",
                )
                .values(
                    status="failed" if error is not None else "succeeded",
                    response=response,
                    usage=usage,
                    error=error,
                    duration_ms=duration_ms,
                    first_token_ms=first_token_ms,
                    finished_at=datetime.now(UTC),
                )
            )

    def latest_agent_request(self, run_id: str) -> dict | None:
        """读取本run最后一次成功的主/子模型请求，供轮末容量重算；不扫描整份账本。

        完整请求已持久化，轮末钩子在进程恢复后也能重建计数所需的系统/工具
        部分，不依赖中间件实例里易丢失或串到别的运行的临时变量。
        """
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(requests.c.id, requests.c.request)
                    .where(
                        requests.c.scope == self.scope,
                        requests.c.run_id == run_id,
                        requests.c.purpose.in_(["agent", "research"]),
                        requests.c.status == "succeeded",
                    )
                    .order_by(requests.c.ordinal.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def records(self, *, root_id: str | None = None) -> list[dict[str, Any]]:
        """供运维/运行详情读取证据，不返回配置或数据库凭据。"""
        query = select(requests).where(requests.c.scope == self.scope)
        if root_id is not None:
            query = query.where(requests.c.root_id == root_id)
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    query.order_by(requests.c.created_at, requests.c.ordinal, requests.c.id)
                ).mappings()
            ]

    def set_root_limit(self, root_id: str, request_limit: int) -> None:
        """仅供可信用户恢复入口更新根上限；不重置已消费值或项目总额。"""
        if not 1 <= request_limit <= 600:
            raise ValueError("root request limit must be between 1 and 600")
        with self.engine.begin() as connection:
            root = (
                connection.execute(
                    select(roots)
                    .where(roots.c.scope == self.scope, roots.c.root_id == root_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if root is not None:
                if root["closed_at"] is not None:
                    raise ValueError("root execution details expired; budget cannot be changed")
                if request_limit < root["used"]:
                    raise ValueError("limit cannot be below requests already spent")
                connection.execute(
                    update(roots)
                    .where(roots.c.scope == self.scope, roots.c.root_id == root_id)
                    .values(request_limit=request_limit)
                )

    def summary(self, root_id: str | None = None) -> dict:
        """正常应用只返回指定任务预算，不构造项目累计或历史用量统计。"""
        with self.engine.connect() as connection:
            quota = (
                (
                    connection.execute(select(quotas).where(quotas.c.scope == self.scope))
                    .mappings()
                    .one_or_none()
                )
                if self.legacy_project_accounting
                else None
            )
            root = (
                connection.execute(
                    select(roots).where(roots.c.scope == self.scope, roots.c.root_id == root_id)
                )
                .mappings()
                .one_or_none()
                if root_id
                else None
            )
        summary = {
            "root_used": root["used"] if root else 0,
            "root_limit": root["request_limit"] if root else None,
        }
        if not self.legacy_project_accounting:
            return summary
        return {
            "project_limit": _effective_total_limit(quota["total_limit"], self.total_limit)
            if quota
            else self.total_limit,
            "project_used": quota["used"] if quota else 0,
            "canary_used": quota["canary_used"] if quota else 0,
            **summary,
        }


class ModelGateway:
    """包围真实客户端的同步入口；客户端替身只用于离线测试。

    Agent、摘要和裁判都必须经此入口。工具绑定只是客户端配置，不产生
    网络请求；实际 invoke 发生前必须成功完成数据库预占。
    """

    def __init__(
        self,
        ledger: ModelLedger,
        *,
        enabled: bool,
        client: Any,
        sensitive_values: Sequence[str] = (),
        capacity: ModelCapacity | None = None,
    ) -> None:
        self.ledger = ledger
        self.enabled = enabled
        self.client = client
        self.sensitive_values = [value for value in sensitive_values if value]
        self.capacity = capacity or ModelCapacity()
        self.attempt_guard: Callable[[str, str, str], None] | None = None

    def _redact(self, value: Any) -> Any:
        """递归移除已知凭据，既覆盖异常文本，也覆盖可能被回显的输入输出。"""
        if isinstance(value, str):
            for secret in self.sensitive_values:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def invoke(
        self,
        messages: Sequence[BaseMessage],
        *,
        root_id: str,
        run_id: str,
        purpose: str = "agent",
        root_limit: int = 40,
        root_reserve: int = 0,
        tools: Sequence[Any] = (),
        stream: bool = False,
        on_chunk: Callable[[str], None] | None = None,
        call_id: str | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        """执行一次尝试，不隐式重试；失败也在持久账本占一次额度。"""
        if not self.enabled:
            raise ModelCallsDisabled("model calls are disabled")
        kwargs = self.request_parameters(kwargs)
        packet = self.request_packet(messages, tools=tools, **kwargs)
        payload = self.provider_payload(messages, tools=tools, **kwargs)
        capacity = self.capacity.validate_request(payload, kwargs["max_tokens"])
        if self.capacity.input_budget_version:
            capacity["components"] = self.measure_components(messages, tools=tools, **kwargs)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"root_id": root_id, "purpose": purpose, **packet},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        reservation, is_new = self.ledger.reserve_call(
            root_id,
            run_id,
            purpose,
            self._redact({**packet, "stream": stream, "capacity": capacity}),
            root_limit=root_limit,
            root_reserve=root_reserve,
            call_id=call_id,
            input_hash=fingerprint,
        )
        if not is_new:
            existing = self.ledger.record(reservation)
            if existing["status"] == "succeeded":
                return AIMessage.model_validate(existing["response"])
            if existing["status"] == "reserved":
                raise ModelCallUncertain(
                    "previous model attempt has unknown outcome; automatic retry refused"
                )
            raise ModelCallFailed(existing["error"] or "previous model attempt failed")
        started = monotonic()
        first_token_ms = None
        combined: AIMessageChunk | None = None
        # 独立账本已分配不可重放的请求身份；跨Run业务预算也必须在供应商访问前预占。
        # 守卫异常保留原控制类型，worker才会把预算/暂停解释为停止而非供应商故障。
        if self.attempt_guard:
            try:
                self.attempt_guard(reservation, root_id, run_id)
            except Exception as exc:
                self.ledger.finish(
                    reservation,
                    error=self._redact(f"{type(exc).__name__}: {exc}")[:2000],
                    duration_ms=0,
                )
                raise
        try:
            client = self.client.bind_tools(list(tools)) if tools else self.client
            if stream:
                # 首个有文本或工具参数的块才算首响应，不把空 metadata 块当 token。
                for chunk in client.stream(list(messages), **kwargs):
                    if first_token_ms is None and (chunk.content or chunk.tool_call_chunks):
                        first_token_ms = (monotonic() - started) * 1000
                    combined = chunk if combined is None else combined + chunk
                    if on_chunk is not None and isinstance(chunk.content, str) and chunk.content:
                        on_chunk(self._redact(chunk.content))
                if combined is None:
                    raise TypeError("provider returned an empty stream")
                response = message_chunk_to_message(combined)
            else:
                response = client.invoke(list(messages), **kwargs)
            if not isinstance(response, AIMessage):
                raise TypeError("provider did not return an AIMessage")
        except Exception as exc:
            safe_error = self._redact(f"{type(exc).__name__}: {exc}")[:2000]
            self.ledger.finish(
                reservation,
                error=safe_error,
                response=self._redact(combined.model_dump(mode="json")) if combined else None,
                duration_ms=(monotonic() - started) * 1000,
                first_token_ms=first_token_ms,
            )
            raise ModelCallFailed(safe_error) from None
        self.ledger.finish(
            reservation,
            response=self._redact(response.model_dump(mode="json")),
            usage=dict(response.usage_metadata) if response.usage_metadata is not None else None,
            duration_ms=(monotonic() - started) * 1000,
            first_token_ms=first_token_ms,
        )
        return response

    def request_parameters(self, parameters: dict) -> dict:
        """明确输出预留，防止供应商默认值与本地容量估计不一致。"""
        result = dict(parameters)
        result.setdefault("max_tokens", self.capacity.output_reserve)
        return result

    def request_packet(self, messages: Sequence[BaseMessage], *, tools=(), **kwargs) -> dict:
        """账本保留框架消息全字段，包含不可冒充业务事实的协议附属数据。"""
        return {
            "messages": [message.model_dump(mode="json") for message in messages],
            "tools": [convert_to_openai_tool(tool) for tool in tools],
            "parameters": kwargs,
        }

    def provider_payload(self, messages: Sequence[BaseMessage], *, tools=(), **kwargs) -> dict:
        """使用真实SDK最终序列化路径；离线替身按同一完整字段保守估计。"""
        schemas = [convert_to_openai_tool(tool) for tool in tools]
        serializer = getattr(self.client, "_get_request_payload", None)
        if serializer is not None:
            return serializer(list(messages), **({"tools": schemas} if schemas else {}), **kwargs)
        return self.request_packet(messages, tools=tools, **kwargs)

    def measure(self, messages: Sequence[BaseMessage], *, tools=(), **kwargs) -> dict:
        """上下文整理使用与最终发送相同的估计器，不产生费用或网络请求。"""
        parameters = self.request_parameters(kwargs)
        return self.capacity.measure(
            self.provider_payload(messages, tools=tools, **parameters), parameters["max_tokens"]
        )

    def measure_components(self, messages: Sequence[BaseMessage], *, tools=(), **kwargs) -> dict:
        """只在最终请求/交付统计时分项计数，压缩二分搜索仍只算总量。"""
        from knowledgeops_agent.harness.context.budget import measure_components

        result = measure_components(
            messages, tools, lambda view, selected: self.measure(view, tools=selected, **kwargs)
        )
        result["output_reserve"] = kwargs.get("max_tokens", self.capacity.output_reserve)
        result["framing_reserve"] = self.capacity.framing_reserve
        return result
