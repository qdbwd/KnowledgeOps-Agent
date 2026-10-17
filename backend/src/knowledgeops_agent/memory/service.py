"""可审查、可撤回的通用长期记忆，沿用旧偏好表和提取回执。

本应用不用自由文件编辑或后台扫描：只解释新真人消息，按明确性决定生效/待确认；
调用预算、写入许可和学习进度均不由偏好产生。UI修改不需要模型。
"""

from __future__ import annotations

import builtins
import hashlib
import re
import unicodedata
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, text

from knowledgeops_agent.app.lifecycle.access import require_conversation
from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.persistence.models import (
    ChatMessage,
    MemoryEvidence,
    Preference,
    PreferenceExtraction,
    PreferenceRevision,
    Run,
    utcnow,
)

EXTRACTOR_VERSION = "user-intent-v8-general-memory"
MemoryCategory = Literal["preference", "background", "behavior", "cognitive", "goal", "correction"]
CATEGORIES = {"preference", "background", "behavior", "cognitive", "goal", "correction"}


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).lower().split())


def _identity_lock(session, content: str, category: str, scope: str, topic: str) -> None:
    """同一记忆的并发提取串行去重；不向模型开放SQL或锁标识。"""
    digest = hashlib.sha256(
        "\x00".join([category, scope, topic, _normalized(content)]).encode()
    ).digest()
    session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": int.from_bytes(digest[:8], "big", signed=True)},
    )


def _duplicate(session, content: str, category: str, scope: str, topic: str):
    return next(
        (
            r
            for r in session.scalars(
                select(Preference)
                .where(
                    Preference.category == category,
                    Preference.scope == scope,
                    Preference.topic == topic,
                    Preference.status.in_(["active", "pending"]),
                )
                .order_by(Preference.id)
                .with_for_update()
            )
            if _normalized(r.content) == _normalized(content)
        ),
        None,
    )


def _explicitly_temporary(message: str, quote: str) -> bool:
    """挡住模型把明确的单次范围误标为长期；这不是通用自然语言分类器。

    优先看完整引用，避免同一消息中另一条长期安排改变临时要求的范围。
    模型可能摘掉“这次”等前缀，因此无任何明确持续意图时还检查原消息。
    “这次请记住，以后……”有持续范围，仍交原有语义与权限校验处理。
    """
    temporary = r"(?:这|本|此)(?:一)?(?:次|轮|回)|暂时|\b(?:this time|for this (?:turn|request)|just for now)\b"
    durable = r"以后|今后|往后|从今|每次|始终|一直|永久|默认|长期(?:使用|采用|遵循|保持|适用)|\b(?:from now on|always|permanent)\b"
    if re.search(temporary, quote, re.IGNORECASE):
        return not bool(re.search(durable, quote, re.IGNORECASE))
    return bool(re.search(temporary, message, re.IGNORECASE)) and not bool(
        re.search(durable, message, re.IGNORECASE)
    )


def _preference_evidence(message: str, quote: str, action: str) -> tuple[str, str]:
    """对模型分类再加原话证据门：active/pending/ignore不是另一份语义分类结果。

    仅识别有直接证据的中英文个人表达意图；无法识别时不自动授予持久性。
    原话附近同句的临时限定不能被截掉，也不能借另一句“以后”长期化。
    此保守门允许漏提取，由用户在偏好页明确新增；不声称能理解任意语言。
    """
    start = message.find(quote)
    delimiters = r"[。！？!?；;，,\n]"
    left = list(re.finditer(delimiters, message[:start]))
    right = re.search(delimiters, message[start + len(quote) :])
    local = message[
        left[-1].end() if left else 0 : start + len(quote) + right.start()
        if right
        else len(message)
    ]
    if _explicitly_temporary(local, quote):
        return "ignore", "当前引用所在语句仅约束本次任务"
    if action == "forget":
        if re.search(r"不要忘|别忘|不用忘|do not forget|don't forget", local, re.I):
            return "ignore", "否定遗忘不构成撤回"
        if re.search(
            r"忘掉|忘记|删除|撤回|取消|停用|不再|别再|\b(?:forget|remove|revoke|stop)\b",
            quote,
            re.I,
        ):
            return "active", "当前原话明确表达撤回，仍需有效目标与既有语义校验"
        return "ignore", "原话没有明确撤回意图"
    # “记住某个业务规则”不是个人表达偏好，不能仅凭记住/以后就纳入记忆。
    style = r"回答|解释|讲解|表达|语气|详略|简短|详细|简洁|例子|举例|排版|段落|标题|称呼|中文|英文|英语|偏好|\b(?:answer|explain|tone|concise|detail|example|format|language|prefer)\b"
    stable = r"我(?:更)?(?:喜欢|偏好|习惯)|\bI (?:prefer|like|usually)\b"
    persistent = r"以后|今后|往后|从今|每次|始终|一直|长期|默认|请记住|记住这个|\b(?:from now on|always|remember|permanent|default)\b"
    if not re.search(style, quote, re.I):
        return "ignore", "没有个人表达或组织偏好的原话证据"
    if re.search(stable, quote, re.I) or re.search(persistent, local, re.I):
        return "active", "原话包含个人表达及持续性证据，仍需既有语义校验"
    if re.search(r"我|偏好|喜欢|习惯|希望|\b(?:I|prefer|like)\b", quote, re.I):
        return "pending", "可能是个人偏好，但持续范围需要确认"
    return "ignore", "普通任务要求不自动成为长期偏好"


class PreferenceProposal(BaseModel):
    """模型只能建议描述性偏好；真人原话、来源、目标版本由业务服务核验。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["remember", "forget"]
    content: str = Field(max_length=2000)
    quote: str = Field(min_length=1, max_length=4000)
    scope: Literal["global", "topic"]
    topic: str = Field(max_length=200)
    durability: Literal["durable", "temporary", "uncertain"]
    authority: Literal["descriptive", "transactional"]
    explicit: bool
    target_id: str | None
    conflicts: list[str] = Field(max_length=20)
    # 缺省值使旧冻结解释器的输出仍可回放，不重写旧资源和历史响应。
    category: MemoryCategory = "preference"


def _memory_evidence(message: str, item: PreferenceProposal) -> tuple[str, str]:
    if item.category == "preference" or item.action == "forget":
        return _preference_evidence(message, item.quote, item.action)
    if _explicitly_temporary(message, item.quote):
        return "ignore", "单次要求不是长期用户信息"
    if re.search(r"授权|无需确认|跳过审批|调用预算|权限|密钥|api[_ -]?key", item.content, re.I):
        return "ignore", "权限与秘密不属于用户背景记忆"
    if not re.search(r"我|本人|\b(?:I|my|I'm)\b", item.quote, re.I):
        return "pending", "背景和目标缺少可核对的用户自述，等待人工确认"
    if item.category in {"behavior", "cognitive"} and not re.search(
        r"习惯|喜欢|通常|一般|一直|偏好|希望|prefer|usually|like", item.quote, re.I
    ):
        return "pending", "不能从一次任务推断稳定协作习惯"
    return "active", "真人原话包含自述；语义分类、范围和明确性仍分别校验"


def preference_view(row: Preference) -> dict:
    return {
        "id": row.id,
        "revision": row.revision,
        "content": row.content,
        "scope": row.scope,
        "topic": row.topic,
        "status": row.status,
        "source_message_id": row.source_message_id,
        "source": row.source,
        "supersedes_id": row.supersedes_id,
        "category": row.category,
        "confidence": row.confidence,
        "revocation_epoch": row.revocation_epoch,
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
        "review_after": row.review_after.isoformat() if row.review_after else None,
        "needs_review": bool(row.review_after and row.review_after <= utcnow()),
    }


def known_preferences(session, query: str | None = None) -> list[dict]:
    """提交时冻结指代目录；有查询时相关检索并限制token，旧调用保留兼容。"""
    rows = [
        preference_view(row)
        for row in session.scalars(
            select(Preference)
            .where(Preference.status.in_(["active", "pending"]))
            .order_by(Preference.created_at, Preference.id)
        )
    ]
    if query is None:
        return rows
    import json

    from knowledgeops_agent.harness.context.tokens import count_text_tokens
    from knowledgeops_agent.vendor.deerflow.memory_relevance import rank_facts

    selected: list[dict] = []
    # 目录含pending，用于明确确认/纠正；不是主模型自动生效的记忆。
    for row in rank_facts(rows, query):
        if len(selected) >= 32:
            break
        value = {
            k: row[k]
            for k in (
                "id",
                "revision",
                "category",
                "content",
                "scope",
                "topic",
                "status",
                "needs_review",
            )
        }
        if count_text_tokens(json.dumps([*selected, value], ensure_ascii=False)) <= 3000:
            selected.append(value)
    return selected


def _validate(content: str, scope: str, topic: str) -> None:
    if not content.strip() or len(content) > 2000:
        raise ValueError("偏好正文需为1至2000字")
    if scope not in {"global", "topic"} or len(topic) > 200:
        raise ValueError("无效偏好范围")
    if (scope == "topic") != bool(topic.strip()):
        raise ValueError("主题偏好必须指定主题；全局偏好无需主题")


class MemoryService:
    def __init__(self, sessions):
        self.sessions = sessions

    @staticmethod
    def _save(session, row):
        session.flush()
        from knowledgeops_agent.memory.retrieval import index_revision

        if row.status == "active" and row.confirmed_at is None:
            row.confirmed_at = utcnow()
        if row.category in {"background", "goal"} and row.review_after is None:
            row.review_after = utcnow() + timedelta(days=90)
        value = preference_view(row)
        session.add(PreferenceRevision(preference_id=row.id, revision=row.revision, content=value))
        session.flush()
        index_revision(session, value)

    @staticmethod
    def _evidence(session, row, *, message=None, kind="manual", quote=""):
        session.flush()
        if message is not None and session.scalar(
            select(MemoryEvidence.id).where(
                MemoryEvidence.preference_id == row.id,
                MemoryEvidence.message_id == message.id,
            )
        ):
            return
        context_key = None
        if message is not None:
            run = session.get(Run, message.run_id)
            learning_id = getattr(run, "learning_task_id", None) or (run.input_data or {}).get(
                "learning_task_id"
            )
            context_key = (
                "learning:" + learning_id
                if learning_id
                else "conversation:" + message.conversation_id
            )
        session.add(
            MemoryEvidence(
                preference_id=row.id,
                message_id=message.id if message else None,
                kind=kind,
                quote=quote,
                context_key=context_key,
            )
        )
        session.flush()

    def list(self, *, include_deleted: bool = False) -> builtins.list[dict]:
        with self.sessions() as session:
            query = select(Preference).order_by(Preference.created_at, Preference.id)
            if not include_deleted:
                query = query.where(Preference.status != "deleted")
            return [preference_view(row) for row in session.scalars(query)]

    def history(self, preference_id: str) -> builtins.list[dict]:
        with self.sessions() as session:
            if session.get(Preference, preference_id) is None:
                raise KeyError("偏好不存在")
            return [
                row.content
                for row in session.scalars(
                    select(PreferenceRevision)
                    .where(PreferenceRevision.preference_id == preference_id)
                    .order_by(PreferenceRevision.revision)
                )
            ]

    def create(self, content: str, *, scope: str, topic: str, category: str = "preference") -> dict:
        _validate(content, scope, topic)
        if category not in CATEGORIES:
            raise ValueError("无效记忆类别")
        with self.sessions.begin() as session:
            lock_control(session)
            _identity_lock(session, content, category, scope, topic)
            existing = _duplicate(session, content, category, scope, topic)
            if existing and existing.status == "active":
                return preference_view(existing)
            if existing:
                existing.status, existing.revision = "active", existing.revision + 1
                existing.source = {**existing.source, "last_editor": "user"}
                self._evidence(session, existing)
                self._save(session, existing)
                return preference_view(existing)
            row = Preference(
                content=content,
                scope=scope,
                topic=topic,
                category=category,
                status="active",
                source={"kind": "manual"},
            )
            session.add(row)
            self._evidence(session, row)
            self._save(session, row)
            return preference_view(row)

    def update(
        self,
        preference_id: str,
        *,
        expected_revision: int,
        content: str | None = None,
        scope: str | None = None,
        topic: str | None = None,
        status: str | None = None,
        supersedes_id: str | None = None,
        category: str | None = None,
    ) -> dict:
        """乐观修订号和行锁避免覆盖旧页面；取代与确认同事务，不先删除旧偏好。"""
        with self.sessions.begin() as session:
            lock_control(session)
            # 多行按ID锁定，两个相反取代请求也不形成锁顺序反转。
            rows = {
                row.id: row
                for row in session.scalars(
                    select(Preference)
                    .where(Preference.id.in_([preference_id, supersedes_id or preference_id]))
                    .order_by(Preference.id)
                    .with_for_update()
                )
            }
            row = rows.get(preference_id)
            if row is None:
                raise KeyError("偏好不存在")
            if row.revision != expected_revision:
                raise ValueError("偏好修订已变化，请刷新后再操作")
            if row.status == "deleted":
                raise ValueError("已删除的偏好保留历史，不自动恢复")
            new_status = status or row.status
            if new_status not in {"active", "pending", "inactive", "deleted"}:
                raise ValueError("无效偏好状态")
            row.content = row.content if content is None else content
            row.scope = row.scope if scope is None else scope
            row.topic = row.topic if topic is None else topic
            row.category = row.category if category is None else category
            if row.category not in CATEGORIES:
                raise ValueError("无效记忆类别")
            _validate(row.content, row.scope, row.topic)
            if supersedes_id:
                old = rows.get(supersedes_id)
                if (
                    old is None
                    or old.id == row.id
                    or old.status == "deleted"
                    or new_status != "active"
                ):
                    raise ValueError("取代对象或确认状态无效")
                old.status, old.revision = "inactive", old.revision + 1
                old.revocation_epoch += 1
                self._save(session, old)
                row.supersedes_id = old.id
            if new_status in {"inactive", "deleted"} and new_status != row.status:
                row.revocation_epoch += 1
            row.status, row.revision = new_status, row.revision + 1
            row.source = {**row.source, "last_editor": "user"}
            row.confidence = 1.0
            row.confirmed_at = utcnow() if new_status == "active" else row.confirmed_at
            row.review_after = (
                utcnow() + timedelta(days=90) if row.category in {"background", "goal"} else None
            )
            self._evidence(session, row)
            self._save(session, row)
            return preference_view(row)

    def extract(
        self, message_id: str, proposals: builtins.list, *, known: builtins.list[dict]
    ) -> dict:
        """对新消息单次记录有效/忽略/错误结果；个别错误不扩大发布许可或阻塞其他项。"""
        with self.sessions.begin() as session:
            lock_control(session)
            message = session.scalar(
                select(ChatMessage)
                .where(ChatMessage.id == message_id, ChatMessage.role == "user")
                .with_for_update()
            )
            if message is None:
                raise KeyError("偏好必须来自真实用户消息")
            require_conversation(session, message.conversation_id)
            source_run = session.get(Run, message.run_id)
            if (
                source_run is None
                or source_run.parent_id
                or source_run.kind != "agent"
                or source_run.conversation_id != message.conversation_id
                or getattr(source_run, "origin", "human") != "human"
            ):
                raise KeyError("自动推进或子运行消息不能作为真人记忆来源")
            prior = session.scalar(
                select(PreferenceExtraction)
                .where(PreferenceExtraction.message_id == message_id)
                .order_by(PreferenceExtraction.created_at)
            )
            if prior:
                return prior.result
            result: dict = {"created": [], "updated": [], "ignored": [], "errors": []}
            expected = {row["id"]: row for row in known}
            for index, data in enumerate(proposals[:8]):
                try:
                    item = PreferenceProposal.model_validate(data)
                    if item.quote not in message.content:
                        raise ValueError("偏好原话不属于当前真人消息")
                    if (
                        item.authority != "descriptive"
                        or item.durability == "temporary"
                        or _explicitly_temporary(message.content, item.quote)
                    ):
                        result["ignored"].append(
                            {"index": index, "reason": "临时要求或操作授权不形成长期偏好"}
                        )
                        continue
                    evidence, evidence_reason = _memory_evidence(message.content, item)
                    if evidence == "ignore":
                        result["ignored"].append({"index": index, "reason": evidence_reason})
                        continue
                    # 主题/项目限定不能因为模型选了global而自动扩到其他场景。
                    if item.scope == "global" and re.search(
                        r"(?:在|针对|关于|对于).{1,50}?(?:项目|主题|领域|时|中)", item.quote
                    ):
                        evidence, evidence_reason = "pending", "原话有限定范围，不能自动提升为全局"
                    target = None
                    if item.target_id:
                        target = session.scalar(
                            select(Preference)
                            .where(Preference.id == item.target_id)
                            .with_for_update()
                        )
                        ref = expected.get(item.target_id)
                        if (
                            not ref
                            or target is None
                            or target.revision != ref["revision"]
                            or target.status == "deleted"
                        ):
                            raise ValueError("目标偏好不是本消息已知的有效修订")
                    if any(identifier not in expected for identifier in item.conflicts):
                        raise ValueError("冲突对象不在已知偏好中")
                    certain = (
                        evidence == "active"
                        and item.explicit
                        and item.durability == "durable"
                        and not item.conflicts
                    )
                    if target and (target.scope, target.topic) != (item.scope, item.topic):
                        certain = False  # 主题例外不能自动撤下不同范围的旧偏好。
                    if item.action == "forget":
                        if not certain or target is None:
                            raise ValueError("撤回偏好需要明确长期选择和确定对象")
                        target.status, target.revision = "inactive", target.revision + 1
                        target.revocation_epoch += 1
                        self._evidence(
                            session, target, message=message, kind="explicit", quote=item.quote
                        )
                        self._save(session, target)
                        result["updated"].append(target.id)
                        continue
                    _validate(item.content, item.scope, item.topic)
                    if target and not certain:
                        # 待确认的新偏好不提前停用旧的；UI确认时再原子取代。
                        item.conflicts = list(dict.fromkeys([*item.conflicts, target.id]))
                    _identity_lock(session, item.content, item.category, item.scope, item.topic)
                    duplicate = _duplicate(
                        session, item.content, item.category, item.scope, item.topic
                    )
                    if duplicate is not None:
                        self._evidence(
                            session,
                            duplicate,
                            message=message,
                            kind="explicit" if item.explicit else "inferred",
                            quote=item.quote,
                        )
                        contexts = set(
                            session.scalars(
                                select(MemoryEvidence.context_key).where(
                                    MemoryEvidence.preference_id == duplicate.id,
                                    MemoryEvidence.context_key.is_not(None),
                                )
                            )
                        )
                        safe_inferred = (
                            len(contexts) >= 2
                            and item.category in {"preference", "behavior", "cognitive"}
                            and not item.conflicts
                            and not duplicate.source.get("conflicts")
                            and not item.target_id
                            and item.durability == "durable"
                        )
                        if duplicate.status == "pending" and (certain or safe_inferred):
                            duplicate.status = "active"
                            duplicate.confidence = 0.9 if certain else 0.65
                        if certain:
                            duplicate.confirmed_at = utcnow()
                            duplicate.review_after = (
                                utcnow() + timedelta(days=90)
                                if duplicate.category in {"background", "goal"}
                                else None
                            )
                        if target and target.id != duplicate.id and certain:
                            target.status, target.revision = "inactive", target.revision + 1
                            target.revocation_epoch += 1
                            self._save(session, target)
                            duplicate.supersedes_id = target.id
                        # 重复不会覆盖人工正文，也不借重复记录处理其他目标的替代。
                        duplicate.revision += 1
                        self._save(session, duplicate)
                        result["updated"].append(duplicate.id)
                        continue
                    row = Preference(
                        content=item.content,
                        scope=item.scope,
                        topic=item.topic,
                        status="active" if certain else "pending",
                        category=item.category,
                        confidence=0.9 if certain else 0.5,
                        source_message_id=message.id,
                        source={
                            "kind": "explicit" if item.explicit else "inferred",
                            "quote": item.quote,
                            "conversation_id": message.conversation_id,
                            "run_id": message.run_id,
                            "extractor_version": EXTRACTOR_VERSION,
                            "conflicts": item.conflicts,
                            "evidence_reason": evidence_reason,
                        },
                        supersedes_id=target.id if target and certain else None,
                    )
                    session.add(row)
                    self._evidence(
                        session,
                        row,
                        message=message,
                        kind="explicit" if item.explicit else "inferred",
                        quote=item.quote,
                    )
                    self._save(session, row)
                    if target and certain:
                        target.status, target.revision = "inactive", target.revision + 1
                        target.revocation_epoch += 1
                        self._save(session, target)
                    result["created"].append(row.id)
                except (ValueError, ValidationError) as error:
                    result["errors"].append({"index": index, "error": str(error)[:350]})
            session.add(
                PreferenceExtraction(
                    message_id=message.id, extractor_version=EXTRACTOR_VERSION, result=result
                )
            )
            return result

    def snapshot(self) -> builtins.list[dict]:
        return [row for row in self.list() if row["status"] == "active"]

    def effective(self, frozen: builtins.list[dict]) -> builtins.list[dict]:
        """普通编辑从下一任务生效；停用/删除在下一调用前去掉旧快照条目。"""
        if not frozen:
            return []
        with self.sessions() as session:
            active = {
                row.id: row.revocation_epoch
                for row in session.scalars(
                    select(Preference).where(
                        Preference.id.in_([row["id"] for row in frozen]),
                        Preference.status == "active",
                    )
                )
            }
            return [
                row
                for row in frozen
                if row["id"] in active and row.get("revocation_epoch", 0) == active[row["id"]]
            ]

    def get(self, memory_id: str) -> dict:
        with self.sessions() as session:
            row = session.get(Preference, memory_id)
            if row is None:
                raise KeyError("记忆不存在")
            evidence = session.scalars(
                select(MemoryEvidence)
                .where(
                    MemoryEvidence.preference_id == row.id,
                )
                .order_by(MemoryEvidence.created_at, MemoryEvidence.id)
            )
            return {
                **preference_view(row),
                "evidence": [
                    {
                        "id": e.id,
                        "message_id": e.message_id,
                        "kind": e.kind,
                        "quote": e.quote,
                        "context_key": e.context_key,
                    }
                    for e in evidence
                ],
            }

    def rollback(self, memory_id: str, *, expected_revision: int, target_revision: int) -> dict:
        """历史保持不可变，恢复另建人工修订；删除墓碑仍不自动复活。"""
        with self.sessions() as session:
            old = session.get(PreferenceRevision, (memory_id, target_revision))
            if old is None:
                raise KeyError("记忆修订不存在")
            value = old.content
        return self.update(
            memory_id,
            expected_revision=expected_revision,
            content=value["content"],
            scope=value["scope"],
            topic=value["topic"],
            category=value.get("category", "preference"),
        )

    def merge(self, *, revisions: dict[str, int], content: str) -> dict:
        """用户审阅后的语义合并，不靠字符串相似度自动丢弃内容。

        同一事务创建新记录、关联全部来源并停用原项；保留原正文/版本，
        undo_merge可以整体撤销。不同类别或范围不能借合并扩大适用性。
        """
        if not 2 <= len(revisions) <= 10:
            raise ValueError("合并需要2至10条明确修订")
        with self.sessions.begin() as session:
            lock_control(session)
            rows = list(
                session.scalars(
                    select(Preference)
                    .where(
                        Preference.id.in_(revisions),
                    )
                    .order_by(Preference.id)
                    .with_for_update()
                )
            )
            if len(rows) != len(revisions) or any(
                row.revision != revisions[row.id] or row.status not in {"active", "pending"}
                for row in rows
            ):
                raise ValueError("合并对象修订或状态已变化，请重新审阅")
            first = rows[0]
            if any(
                (r.category, r.scope, r.topic) != (first.category, first.scope, first.topic)
                for r in rows
            ):
                raise ValueError("不同类别或作用域的记忆不能直接合并")
            _validate(content, first.scope, first.topic)
            merged = Preference(
                content=content,
                category=first.category,
                scope=first.scope,
                topic=first.topic,
                status="active",
                confidence=1.0,
                source={
                    "kind": "manual",
                    "merged_from": [
                        {
                            "id": r.id,
                            "before_revision": r.revision,
                            "withdrawn_revision": r.revision + 1,
                            "before_status": r.status,
                        }
                        for r in rows
                    ],
                },
            )
            session.add(merged)
            self._evidence(session, merged)
            seen = set()
            for evidence in session.scalars(
                select(MemoryEvidence)
                .where(
                    MemoryEvidence.preference_id.in_(revisions),
                    MemoryEvidence.message_id.is_not(None),
                )
                .order_by(MemoryEvidence.created_at, MemoryEvidence.id)
            ):
                if evidence.message_id not in seen:
                    session.add(
                        MemoryEvidence(
                            preference_id=merged.id,
                            message_id=evidence.message_id,
                            kind=evidence.kind,
                            quote=evidence.quote,
                            context_key=evidence.context_key,
                        )
                    )
                    seen.add(evidence.message_id)
            self._save(session, merged)
            for row in rows:
                row.status, row.revision = "inactive", row.revision + 1
                row.revocation_epoch += 1
                self._save(session, row)
            return preference_view(merged)

    def undo_merge(self, memory_id: str, *, expected_revision: int) -> dict:
        """只有整组源记录仍在合并后的修订时才能撤销，避免覆盖后续人工更改。"""
        with self.sessions.begin() as session:
            lock_control(session)
            candidate = session.get(Preference, memory_id)
            sources = candidate.source.get("merged_from", []) if candidate else []
            if not sources:
                raise ValueError("该记录没有可撤销的合并来源")
            rows = {
                r.id: r
                for r in session.scalars(
                    select(Preference)
                    .where(
                        Preference.id.in_([memory_id, *(s["id"] for s in sources)]),
                    )
                    .order_by(Preference.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            }
            merged = rows[memory_id]
            if merged.revision != expected_revision or merged.status != "active":
                raise ValueError("合并记录修订已变化，请刷新")
            if any(
                s["id"] not in rows
                or rows[s["id"]].revision != s["withdrawn_revision"]
                or rows[s["id"]].status != "inactive"
                for s in sources
            ):
                raise ValueError("原记忆已经另有修改，不能整体回退合并")
            for source in sources:
                row = rows[source["id"]]
                row.status, row.revision = source["before_status"], row.revision + 1
                row.source = {**row.source, "last_editor": "user", "undo_merge": memory_id}
                self._save(session, row)
            merged.status, merged.revision = "inactive", merged.revision + 1
            merged.revocation_epoch += 1
            self._save(session, merged)
            return preference_view(merged)

    def search(
        self,
        query: str,
        *,
        topic: str = "",
        catalog: builtins.list[dict] | None = None,
        limit: int = 8,
    ) -> builtins.list[dict]:
        from knowledgeops_agent.memory.retrieval import search

        with self.sessions() as session:
            return search(session, query, topic=topic, refs=catalog, limit=limit)

    def freeze(
        self, query: str, *, topic: str = "", token_budget: int = 2000, correction_budget: int = 500
    ) -> dict:
        from knowledgeops_agent.memory.retrieval import freeze

        with self.sessions() as session:
            return freeze(
                session,
                query,
                topic=topic,
                token_budget=token_budget,
                correction_budget=correction_budget,
            )

    def read(self, memory_id: str, *, catalog: builtins.list[dict]) -> dict:
        from knowledgeops_agent.memory.retrieval import read_frozen

        with self.sessions() as session:
            result = read_frozen(session, [ref for ref in catalog if ref["id"] == memory_id])
            if not result:
                raise KeyError("记忆不可读、已撤回或不在本运行范围")
            return result[0]

    def rebuild_index(self) -> dict:
        from knowledgeops_agent.memory.retrieval import index_revision

        count = chunks = 0
        with self.sessions.begin() as session:
            lock_control(session)
            for row in session.scalars(
                select(PreferenceRevision)
                .order_by(
                    PreferenceRevision.preference_id,
                    PreferenceRevision.revision,
                )
                .with_for_update()
            ):
                chunks += index_revision(session, row.content)
                count += 1
            session.flush()
        return {"indexed_revisions": count, "indexed_chunks": chunks}


class PreferenceService(MemoryService):
    """旧偏好API的兼容视图；更新、来源、回退和撤回均走同一记忆服务。"""

    def list(self, *, include_deleted: bool = False) -> builtins.list[dict]:
        return [
            row
            for row in super().list(include_deleted=include_deleted)
            if row["category"] == "preference"
        ]
