"""借鉴DeerFlow的检查点可达历史，适配本项目PG、会话快照和原文产物。

不查整库聊天表或“当前最新”会话指针。只查当前State，以及该State声明的
不可变历史批次，所以旧检查点和子Agent不能通过检索越界看到未来/父任务。
"""

import hashlib
import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_from_dict
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from knowledgeops_agent.app.lifecycle.access import require_writable_run
from knowledgeops_agent.app.lifecycle.references import replace_references
from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.persistence.models import (
    Run,
    RunArtifact,
    TaskHistoryBatch,
    TaskHistoryChunk,
    TaskHistoryRecord,
)
from knowledgeops_agent.retrieval.index import add_history_chunks
from knowledgeops_agent.retrieval.search import (
    LexicalQuery,
    excerpt,
    search_expressions,
)
from knowledgeops_agent.vendor.deerflow.history_terms import index_text
from knowledgeops_agent.vendor.deerflow.task_continuity_state import normalize_task_history


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def history_scope(context) -> str:
    """主Agent按会话隔离；子Agent仅有自己的run，不继承父历史读权限。"""
    run = context.jobs.get_run(context.run_id)
    owner = (
        ["conversation", run["conversation_id"]]
        if context.run_id == context.root_id
        else ["child", context.run_id]
    )
    return _digest(owner)


def visible_records(messages: list) -> list[dict]:
    """沿用上游可见文本白名单和内容寻址；本项目保存全文供分页，不截为16K。

    排除摘要/运行时注入，避免把模型摘要再登记成真人原话。历史工具调用仅
    表示模型曾请求动作，不证明已执行；执行结果的角色仍为tool。
    """
    result = []
    for message in messages:
        if not isinstance(message, (HumanMessage, AIMessage, ToolMessage)):
            continue
        if message.additional_kwargs.get("knowledgeops_context_data") or (
            message.id or ""
        ).startswith("summary-"):
            continue
        content = message.content
        if isinstance(content, list):
            content = "\n".join(
                block if isinstance(block, str) else str(block.get("text", ""))
                for block in content
                if isinstance(block, str)
                or (isinstance(block, dict) and block.get("type") == "text")
            )
        calls = getattr(message, "tool_calls", [])
        if calls:
            content += "\nTool calls: " + json.dumps(
                [{key: call.get(key) for key in ("name", "args", "id")} for call in calls],
                ensure_ascii=False,
            )
        if not content:
            continue
        role: str = message.type
        if (message.id or "").startswith(("application-receipt-", "output-limit-receipt-")):
            role = "application"
        source = {"role": role, "message_id": message.id, "name": message.name, "text": content}
        result.append({**source, "id": "r" + _digest(source)[:32], "truncated": False})
    return result


def _store_records(session, scope: str, rows: list[dict]) -> None:
    """幂等保存原话及BM25派生索引；存储本身不授予检查点读取权限。

    当前消息查询与归档共用写入路径。只首次写入时分段，重试/并发通过复合
    主键裁决；原话、片段要么一起提交要么一起回滚，不创建新的可达批次。
    """
    lock_control(session)
    existing = set(
        session.scalars(
            select(TaskHistoryRecord.source_id).where(
                TaskHistoryRecord.scope == scope,
                TaskHistoryRecord.source_id.in_([row["id"] for row in rows]),
            )
        )
    )
    for row in rows:
        replace_references(session, "history", f"{scope}:{row['id']}", row)
        if row["id"] in existing:
            continue
        inserted = session.scalar(
            insert(TaskHistoryRecord)
            .values(
                scope=scope,
                source_id=row["id"],
                payload=row,
                search_terms=index_text(row["text"]).split(),
            )
            .on_conflict_do_nothing()
            .returning(TaskHistoryRecord.source_id)
        )
        if inserted is not None:
            add_history_chunks(session, scope, row)
            existing.add(row["id"])
    session.flush()


def capture_history(context, state: dict, messages: list) -> dict:
    """先保存不可变数据，调用方将返回引用提交到检查点/快照后才获得读取范围。

    重试写同一批次为幂等。失败未提交的孤立批次不向旧状态可见。保留上游
    64批引用边界并明确标记部分历史不可达；物理原件与旧检查点不删除。
    """
    scope = history_scope(context)
    previous = normalize_task_history(state.get("task_history"))
    if previous.get("scope") not in (None, scope):
        raise PermissionError("history scope mismatch")
    rows = context.gateway._redact(visible_records(messages))
    if not rows:
        return previous
    ids = [row["id"] for row in rows]
    batch_id = _digest([scope, ids])
    with context.jobs.sessions.begin() as session:
        lock_control(session)
        require_writable_run(session, session.get(Run, context.run_id))
        _store_records(session, scope, rows)
        session.execute(
            insert(TaskHistoryBatch)
            .values(scope=scope, id=batch_id, run_id=context.run_id, source_ids=ids)
            .on_conflict_do_nothing()
        )
    batches = list(dict.fromkeys([*previous.get("batches", []), batch_id]))
    omitted = 0
    if len(batches) > 64:
        with context.jobs.sessions() as session:
            prior_batches = list(
                session.scalars(
                    select(TaskHistoryBatch).where(
                        TaskHistoryBatch.scope == scope, TaskHistoryBatch.id.in_(batches)
                    )
                )
            )
        kept = {
            source
            for batch in prior_batches
            if batch.id in batches[-64:]
            for source in batch.source_ids
        }
        removed = {
            source
            for batch in prior_batches
            if batch.id not in batches[-64:]
            for source in batch.source_ids
        }
        omitted = len(removed - kept)
    return {
        "scope": scope,
        "batches": batches[-64:],
        "omitted_records": previous.get("omitted_records", 0) + omitted,
        "status": "unavailable"
        if omitted or previous.get("status") == "unavailable"
        else "available",
    }


def lookup_history(
    context,
    state: dict,
    *,
    query: str | None = None,
    source_id: str | None = None,
    role: str | None = None,
) -> dict:
    """当前消息与可达历史统一分段排序；先限制scope/批次/角色，再去重取前8条。

    未归档消息幂等缓存成PG索引行，才能与旧历史一起使用真实BM25统计。
    缓存不会产生批次或改写State；仍只查询当前消息ID和当前检查点可达ID。
    完整原话按稳定source_id读取，片段仅负责定位。
    """
    scope = history_scope(context)
    history = normalize_task_history(state.get("task_history"))
    if history.get("scope") not in (None, scope):
        return {"results": [], "status": "scope_unavailable"}
    parsed = LexicalQuery.parse(query or "")
    if source_id is None and not parsed.terms:
        return {"results": [], "status": "empty_query"}
    status = history.get("status", "available")
    active = {
        row["id"]: row
        for row in context.gateway._redact(visible_records(state.get("messages", [])))
        if role is None or row["role"] == role
    }
    batches = history.get("batches", [])
    with context.jobs.sessions.begin() as session:
        lock_control(session)
        require_writable_run(session, session.get(Run, context.run_id))
        stored = (
            list(
                session.scalars(
                    select(TaskHistoryBatch).where(
                        TaskHistoryBatch.scope == scope, TaskHistoryBatch.id.in_(batches)
                    )
                )
            )
            if batches
            else []
        )
        if len(stored) != len(batches):
            status = "partially_unavailable"
        visible_ids = {item for batch in stored for item in batch.source_ids}
        if source_id:
            row = active.get(source_id)
            if row is None and source_id in visible_ids:
                record = session.get(TaskHistoryRecord, (scope, source_id))
                if record is not None and (role is None or record.payload["role"] == role):
                    row = record.payload
            return context.gateway._redact({"results": [row] if row else [], "status": status})
        _store_records(session, scope, list(active.values()))
        rows = _search_passages(session, scope, visible_ids | active.keys(), parsed, role)
    return context.gateway._redact({"results": rows, "status": status})


def _search_passages(session, scope, visible_ids, query: LexicalQuery, role) -> list[dict]:
    """可读片段统一BM25排序，不再让近期弱匹配无条件盖过旧原话。"""
    chunk = TaskHistoryChunk
    match, score = search_expressions(chunk, query)
    stored = (
        select(chunk.source_id, chunk.chunk_index, chunk.start, chunk.end, score.label("score"))
        .join(
            TaskHistoryRecord,
            (TaskHistoryRecord.scope == chunk.scope)
            & (TaskHistoryRecord.source_id == chunk.source_id),
        )
        .where(chunk.scope == scope, chunk.source_id.in_(visible_ids), match)
    )
    if role:
        stored = stored.where(TaskHistoryRecord.payload["role"].astext == role)
    candidates = stored.subquery()
    ranked = select(
        candidates,
        func.row_number()
        .over(
            partition_by=candidates.c.source_id,
            order_by=(candidates.c.score.desc(), candidates.c.chunk_index),
        )
        .label("position"),
    ).subquery()
    hits = list(
        session.execute(
            select(ranked)
            .where(ranked.c.position == 1)
            .order_by(ranked.c.score.desc(), ranked.c.source_id)
            .limit(8)
        ).mappings()
    )
    persisted_ids = [hit["source_id"] for hit in hits]
    payloads = {}
    if persisted_ids:
        payloads.update(
            {
                row.source_id: row.payload
                for row in session.scalars(
                    select(TaskHistoryRecord).where(
                        TaskHistoryRecord.scope == scope,
                        TaskHistoryRecord.source_id.in_(persisted_ids),
                    )
                )
            }
        )
    result = []
    for hit in hits:
        row = payloads[hit["source_id"]]
        selected = excerpt(row["text"], hit["start"], hit["end"], query)
        result.append(
            {
                **row,
                "score": float(hit["score"]),
                "start": selected.start,
                "end": selected.end,
                "excerpt": selected.text,
                "chunk_index": hit["chunk_index"],
            }
        )
    return result


def bootstrap_history(context, content: dict) -> dict:
    """为旧延续快照补索引，只读其中明确许可的摘要原文产物；不扫描全库。

    新快照直接承接引用。转换发生在新运行的before_agent节点，不回写旧快照。
    某份旧原文缺失/损坏/无权读取时明确标记不完整，保留其他可读消息；数据库
    等基础设施异常仍抛出，不把系统故障伪装成“没有历史”。
    """
    if content.get("task_history"):
        return normalize_task_history(content["task_history"])
    messages = messages_from_dict(content.get("messages", []))
    ids = content.get("artifact_ids", [])
    unavailable = False
    if ids:
        with context.jobs.sessions() as session:
            metadata = dict(
                session.execute(
                    select(RunArtifact.id, RunArtifact.kind).where(RunArtifact.id.in_(ids))
                ).all()
            )
        # 缺失元数据时，带kind条件的查询会静默漏掉引用，不能因此宣称历史完整。
        for missing in sorted(set(ids) - metadata.keys()):
            unavailable = True
            context.jobs.append_event(
                context.run_id,
                "history_unavailable",
                {
                    "artifact_id": missing,
                    "reason": "missing_artifact_metadata",
                },
            )
        originals = [
            identifier for identifier, kind in metadata.items() if kind == "context_original"
        ]
        for artifact_id in originals:
            try:
                raw = context.jobs.artifact_content(artifact_id, root_id=context.root_id)
                messages += messages_from_dict(
                    [{"type": item["type"], "data": item} for item in json.loads(raw)]
                )
            except (KeyError, ValueError, TypeError, PermissionError) as exc:
                unavailable = True
                context.jobs.append_event(
                    context.run_id,
                    "history_unavailable",
                    {"artifact_id": artifact_id, "reason": type(exc).__name__},
                )
    history = capture_history(context, {}, messages)
    if unavailable:
        history["status"] = "unavailable"
    return history
