"""PG记忆检索与有界选择；用冻结修订读取，当前撤回优先于旧快照。"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import delete, or_, select, tuple_

from knowledgeops_agent.db import MemorySearchChunk, Preference, PreferenceRevision, utcnow
from knowledgeops_agent.retrieval import LexicalQuery, search_expressions, split_passages, tokenize
from knowledgeops_agent.token_counting import count_text_tokens
from knowledgeops_agent.vendor.deerflow.memory_relevance import build_idf, rank_facts
from knowledgeops_agent.vendor.deerflow.memory_relevance import tokenize as relevance_tokens


def index_revision(session, value: dict) -> int:
    """派生索引跟随不可变修订入同一事务；重建只替换该修订的片段。"""
    session.execute(
        delete(MemorySearchChunk).where(
            MemorySearchChunk.preference_id == value["id"],
            MemorySearchChunk.revision == value["revision"],
        )
    )
    passages = split_passages(value["content"])
    for i, passage in enumerate(passages):
        session.add(
            MemorySearchChunk(
                preference_id=value["id"],
                revision=value["revision"],
                chunk_index=i,
                body_tokens=tokenize(passage.text),
            )
        )
    return len(passages)


def catalog(session, *, topic: str = "") -> list[dict]:
    """只给运行保存读取身份，不把整个记忆库正文塞给模型。"""
    rows = session.scalars(
        select(Preference)
        .where(
            Preference.status == "active",
            or_(
                Preference.scope == "global",
                (Preference.scope == "topic") & (Preference.topic == topic),
            ),
        )
        .order_by(Preference.id)
    )
    return [
        {"id": row.id, "revision": row.revision, "revocation_epoch": row.revocation_epoch}
        for row in rows
    ]


def available_refs(session, refs: list[dict]) -> list[dict]:
    if not refs:
        return []
    current = {
        r.id: r
        for r in session.scalars(
            select(Preference).where(
                Preference.id.in_([ref["id"] for ref in refs]),
                Preference.status == "active",
            )
        )
    }
    return [
        ref
        for ref in refs
        if ref["id"] in current
        and ref.get("revocation_epoch", 0) == current[ref["id"]].revocation_epoch
    ]


def read_frozen(session, refs: list[dict]) -> list[dict]:
    """普通修改不偷偷改变旧运行内容；停用再启用也不复活旧运行的读取资格。"""
    refs = available_refs(session, refs)
    if not refs:
        return []
    versions = session.scalars(
        select(PreferenceRevision).where(
            tuple_(PreferenceRevision.preference_id, PreferenceRevision.revision).in_(
                [(r["id"], r["revision"]) for r in refs]
            )
        )
    )
    by_key = {(r.content["id"], r.revision): r.content for r in versions}
    result = []
    for ref in refs:
        value = by_key.get((ref["id"], ref["revision"]))
        if value is not None:
            review_after = value.get("review_after")
            result.append(
                {
                    "category": "preference",
                    "confidence": 1.0,
                    "revocation_epoch": 0,
                    **value,
                    "needs_review": bool(
                        review_after and datetime.fromisoformat(review_after) <= utcnow()
                    ),
                }
            )
    return result


def search(
    session, query: str, *, topic: str = "", refs: list[dict] | None = None, limit: int = 8
) -> list[dict]:
    """先以合法修订限制PG召回，再按原记忆去重；不能先全库取top-k后再过滤。"""
    if not 1 <= limit <= 50:
        raise ValueError("记忆检索数量须在1至50之间")
    parsed = LexicalQuery.parse(query)
    allowed = available_refs(session, catalog(session, topic=topic) if refs is None else refs)
    if not parsed.terms or not allowed:
        return []
    match, score = search_expressions(MemorySearchChunk, parsed)
    # 片段先聚合为记忆，防止一条长记忆占满候选。查询范围包含冻结修订。
    from sqlalchemy import func

    best = func.max(score).label("bm25_score")
    hits = session.execute(
        select(
            MemorySearchChunk.preference_id,
            MemorySearchChunk.revision,
            best,
        )
        .where(
            match,
            tuple_(MemorySearchChunk.preference_id, MemorySearchChunk.revision).in_(
                [(r["id"], r["revision"]) for r in allowed]
            ),
        )
        .group_by(MemorySearchChunk.preference_id, MemorySearchChunk.revision)
        .order_by(best.desc(), MemorySearchChunk.preference_id)
        .limit(limit * 2)
    ).all()
    values = read_frozen(
        session,
        [
            {**next(r for r in allowed if r["id"] == hit.preference_id), "revision": hit.revision}
            for hit in hits
        ],
    )
    scores = {hit.preference_id: float(hit.bm25_score) for hit in hits}
    idf = build_idf([relevance_tokens(r["content"]) for r in values])
    ranked = rank_facts(values, parsed.text, idf=idf)
    return [{**value, "bm25_score": scores[value["id"]]} for value in ranked[:limit]]


def context_entry(value: dict) -> dict:
    """背景只带当前记录和原话定位；完整证据/修订按需读，不复制整个审计链。"""
    return {
        **{key: value[key] for key in ("id", "revision", "content", "scope", "topic")},
        "category": value.get("category", "preference"),
        "revocation_epoch": value.get("revocation_epoch", 0),
        "source_message_id": value.get("source_message_id"),
        "source_kind": value.get("source", {}).get("kind", "legacy"),
        "review_after": value.get("review_after"),
        "needs_review": value.get("needs_review", False),
    }


def freeze(
    session, query: str, *, topic: str = "", token_budget: int = 2000, correction_budget: int = 500
) -> dict:
    """新真人消息/单元选一次。纠正预留包含在总预算内；整条取舍，不静默截断。"""
    if token_budget < 32 or not 0 <= correction_budget <= token_budget:
        raise ValueError("无效记忆注入预算")
    refs = catalog(session, topic=topic)
    ranked = search(session, query, topic=topic, refs=refs, limit=50)
    # 通用表达/互动约定如“用中文”不必逐字命中当前问题，但仍受有界选择控制。
    baseline_refs = session.execute(
        select(Preference.id, Preference.revision)
        .where(
            Preference.status == "active",
            Preference.scope == "global",
            Preference.category.in_(["preference", "behavior", "cognitive"]),
        )
        .order_by(Preference.confirmed_at.desc().nulls_last(), Preference.id)
        .limit(32)
    ).all()
    baseline_keys = {(r.id, r.revision) for r in baseline_refs}
    baseline = read_frozen(session, [r for r in refs if (r["id"], r["revision"]) in baseline_keys])
    candidates = {r["id"]: r for r in [*ranked, *baseline]}
    ordered = rank_facts(list(candidates.values()), query)
    selected: list[dict] = []

    def size(items):
        return count_text_tokens(json.dumps(items, ensure_ascii=False))

    def append_if_fits(value, maximum):
        entry = context_entry(value)
        if any(r["id"] == entry["id"] for r in selected):
            return
        if size([*selected, entry]) <= maximum:
            selected.append(entry)

    for value in ordered:
        if value.get("category") == "correction":
            append_if_fits(value, correction_budget)
    for value in ordered:
        append_if_fits(value, token_budget)
    return {
        "contract": "pg-memory-v1",
        "query": query[:500],
        "topic": topic,
        "catalog": refs,
        "selected": selected,
        "token_count": size(selected),
        "token_budget": token_budget,
    }
