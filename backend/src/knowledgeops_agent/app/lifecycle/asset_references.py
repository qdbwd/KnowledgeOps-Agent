"""明确链接和有语义的字段形成资产边；无根循环、访问目录、偶然ID不成为保留根。"""

import codecs
import hashlib
import json
import re
from pathlib import Path

from langchain_core.messages import BaseMessage
from sqlalchemy import String, and_, cast, delete, literal, or_, select, union
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql.elements import ColumnElement

from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.knowledge.citations import evidence_ids
from knowledgeops_agent.persistence import models as db

_ID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_LINK = re.compile(
    r"(?<![\w/.:])/(?:api/)?(?P<kind>evidence|sources|source-versions|source-segments|deliveries|changes|changesets)/"
    rf"(?P<id>{_ID})(?P<suffix>[^\s\]\)<>`\"']*)"
)
_ARTIFACT = re.compile(rf"(?<![\w/.:])/(?:api/)?runs/[^/\s]+/artifacts/({_ID})")
_MATERIAL_FILE = re.compile(rf"(?<![\w.])artifacts/({_ID})/([0-9a-f]{{64}})\.txt(?![\w.])")
# 兼容既有摘要生成器写出的明确补读指令；普通日志里的裸UUID仍不是引用。
_LEGACY_READ = re.compile(rf"(?<![A-Za-z0-9_])read_tool_result(?:读取|\s+read)?\s+({_ID})")
_FIELDS = {"evidence_id": "evidence", "source_id": "sources", "source_version_id": "source-versions",
           "segment_id": "source-segments", "source_segment_id": "source-segments",
           "delivery_id": "deliveries", "artifact_id": "material", "original_artifact_id": "material",
           "summary_artifact_id": "material", "changeset_id": "changesets", "draft_id": "changesets"}
_CATALOGS = {"artifact_ids", "source_ids", "evidence_ids", "allowed_evidence_ids", "validated_evidence_ids",
             "source_catalog", "evidence_catalog", "material_catalog"}


def candidates(value):
    """只解释应用链接及约定的身份字段，普通文本中的裸UUID不是引用。"""
    if isinstance(value, BaseMessage):
        yield from candidates(value.model_dump())
    elif isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                structured = json.loads(value)
            except RecursionError as error:
                raise ValueError("structured reference content exceeds the parser depth") from error
            except ValueError:
                structured = None
            if isinstance(structured, (dict, list)):
                yield from candidates(structured)
                return  # 不再把已经排除的访问目录当普通文本重新扫描。
        for match in _LINK.finditer(value):
            kind, identity = match["kind"], match["id"].lower()
            # 指定版本时不跟随来源的最新版本；不带版本的来源链接保留来源各版本。
            fixed = re.search(rf"[?&]version(?:_id)?=({_ID})", match["suffix"])
            yield ("source-versions", fixed[1].lower()) if kind == "sources" and fixed else (kind, identity)
        for identity in evidence_ids(value):
            yield "evidence", identity
        for identity in _ARTIFACT.findall(value):
            yield "material", identity.lower()
        for identity in _LEGACY_READ.findall(value):
            yield "material", identity.lower()
        for run_id, digest in _MATERIAL_FILE.findall(value):
            yield "material-file", f"artifacts/{run_id.lower()}/{digest}.txt"
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in _CATALOGS:
                continue
            if key in _FIELDS and isinstance(child, str):
                yield _FIELDS[key], child
            else:
                yield from candidates(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from candidates(child)


def delivery_reference_value(session, row):
    """有界流式核对交付文本；只返回引用字段，不把整份大文件塞进引用目录。"""
    text_suffixes = {".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
                     ".html", ".htm", ".css", ".csv", ".toml", ".xml", ".sh", ".sql"}
    if not row.mime_type.startswith("text/") and Path(row.name).suffix.lower() not in text_suffixes:
        return []
    store = session.info.get("delivery_store") or session.info.get("file_store")
    if store is None:
        raise ValueError("delivery reference indexing requires the configured workspace")
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    digest, size, tail, found = hashlib.sha256(), 0, "", set()
    with store.path(row.storage_key).open("rb") as stream:
        while chunk := stream.read(65536):
            size += len(chunk)
            if size > row.size_bytes or size > 100 * 1024 * 1024:
                raise ValueError("delivery size changed while indexing references")
            digest.update(chunk)
            value = tail + decoder.decode(chunk)
            found.update(candidates(value))
            tail = value[-4096:]
    found.update(candidates(tail + decoder.decode(b"", final=True)))
    if size != row.size_bytes or digest.hexdigest() != row.sha256:
        raise ValueError("delivery integrity check failed while indexing references")
    fields = {kind: field for field, kind in _FIELDS.items()}
    fields["changes"] = "draft_id"
    return [({"path": identity} if kind == "material-file" else {fields[kind]: identity})
            for kind, identity in sorted(found) if kind in fields or kind == "material-file"]


def _targets(session, value):
    found: dict[str, set[str]] = {}
    for kind, identity in candidates(value):
        found.setdefault(kind, set()).add(identity)
    versions = set(found.get("source-versions", set()))
    segments = set(found.get("source-segments", set()))
    if found.get("evidence"):
        segments.update(session.scalars(select(db.Evidence.source_segment_id).where(
            db.Evidence.id.in_(found["evidence"]), db.Evidence.source_segment_id.is_not(None))))
    if segments:
        versions.update(session.scalars(select(db.SourceSegment.source_version_id).where(db.SourceSegment.id.in_(segments))))
    nodes = {"source_version:" + identity for identity in session.scalars(
        select(db.SourceVersion.id).where(db.SourceVersion.id.in_(versions)))} if versions else set()
    if found.get("sources"):
        nodes.update("source:" + identity for identity in session.scalars(
            select(db.Source.id).where(db.Source.id.in_(found["sources"]))))
    for aliases, model, prefix in (
        (("deliveries",), db.DeliveredFile, "delivery:"),
        (("changes", "changesets"), db.ChangeSet, "draft:"),
        (("material",), db.RunArtifact, "material:"),
    ):
        ids = set().union(*(found.get(alias, set()) for alias in aliases))
        if ids:
            nodes.update(prefix + identity for identity in session.scalars(select(model.id).where(model.id.in_(ids))))
    if found.get("material-file"):
        nodes.update("material_file:" + identity for identity in session.scalars(select(db.RunArtifact.id).where(
            db.RunArtifact.content_storage_key.in_(found["material-file"]), db.RunArtifact.expired_at.is_(None))))
    return nodes


def origin_node(session, kind: str, identity: str) -> str:
    if kind == "asset" and identity.startswith("source-body:"):
        return "source_version:" + identity.removeprefix("source-body:")
    if kind == "asset" and identity.startswith("source-segment:"):
        segment = session.get(db.SourceSegment, identity.removeprefix("source-segment:"))
        if segment is None:
            raise ValueError("source segment is missing while indexing references")
        return "source_version:" + segment.source_version_id
    if kind == "draft":
        return "draft:" + identity.removeprefix("current:").split(":", 1)[0]
    if kind in {"run_input", "run", "checkpoint"}:
        return "run:" + identity
    return kind + ":" + identity


def replace_asset_references(session, kind: str, identity: str, value) -> set[str]:
    """作为既有正文追踪的一部分，与拥有者正文提交/回滚同生效。"""
    lock_control(session)
    nodes = _targets(session, value)
    session.execute(delete(db.AssetReference).where(
        db.AssetReference.owner_kind == kind, db.AssetReference.owner_id == identity,
        db.AssetReference.target_node.not_in(nodes),
    ))
    origin = origin_node(session, kind, identity)
    for node in sorted(nodes):
        session.execute(insert(db.AssetReference).values(owner_kind=kind, owner_id=identity,
            origin_node=origin, target_node=node).on_conflict_do_nothing())
    return nodes


def _node(prefix, value):
    return cast(literal(prefix) + value, String(200)).label("node")


def reachable_assets(session, *, excluding_conversation: str | None = None, excluding_source: str | None = None,
                     excluding_delivery: str | None = None, workspace_values: dict | None = None,
                     excluding_source_owner: tuple[str, str] = ("library", "default"), durable_only: bool = False):
    """从独立资产、存活会话和仍需恢复的运行出发，SQL递归寻找实际可达内容。

    删除预览可排除拟解除的拥有者；未删除的另一个会话和正式知识继续是根。
    UNION去重使只有相互引用、没有存活根的材料循环自然不可达。
    """
    from knowledgeops_agent.harness.context.history import _digest

    active_conversations = select(db.Conversation.id).where(db.Conversation.deletion_state == "active")
    if excluding_conversation:
        active_conversations = active_conversations.where(db.Conversation.id != excluding_conversation)
    root_owners = select(db.Run.id).where(db.Run.parent_id.is_(None), or_(
        db.Run.conversation_id.is_(None), db.Run.conversation_id.in_(active_conversations)))
    live_runs = select(db.Run.id).where(db.Run.lifecycle_state == "active", db.Run.root_id.in_(root_owners))
    used_sources = select(db.SourceUse.source_id).where(or_(
        db.SourceUse.owner_kind == "library",
        and_(db.SourceUse.owner_kind == "conversation", db.SourceUse.owner_id.in_(active_conversations)),
        and_(db.SourceUse.owner_kind == "run", or_(db.SourceUse.owner_id.in_(live_runs),
            ~select(db.Run.id).where(db.Run.id == db.SourceUse.owner_id).correlate(db.SourceUse).exists())),
    ))
    if durable_only:
        used_sources = used_sources.where(db.SourceUse.owner_kind == "library")
    if excluding_source:
        used_sources = used_sources.where(~and_(db.SourceUse.source_id == excluding_source,
            db.SourceUse.owner_kind == excluding_source_owner[0], db.SourceUse.owner_id == excluding_source_owner[1]))
    unknown = ~db.Source.ownership_known
    if excluding_source and excluding_source_owner[0] == "library":
        # 明确删除旧材料即解除未知的独立保存意图；真实的其他使用关系仍然有效。
        unknown = and_(unknown, db.Source.id != excluding_source)
    roots = [select(_node("source_version:", db.SourceVersion.id)).join(db.Source).where(or_(
        unknown, db.Source.id.in_(used_sources))),
        select(_node("delivery:", db.DeliveredFile.id)).where(db.DeliveredFile.deleted_at.is_(None)),
        select(_node("asset:knowledge:", db.KnowledgeVersion.id)),
        select(_node("source_version:", db.SourceSegment.source_version_id)).join(
            db.Evidence, db.Evidence.source_segment_id == db.SourceSegment.id).join(
            db.Citation, db.Citation.evidence_id == db.Evidence.id),
        select(_node("message:", db.ChatMessage.id)).where(db.ChatMessage.conversation_id.in_(active_conversations)),
        select(_node("snapshot:", db.ContinuationSnapshot.id)).where(db.ContinuationSnapshot.conversation_id.in_(active_conversations)),
        select(_node("run:", db.Run.id)).where(db.Run.id.in_(live_runs)),
        select(_node("material:", db.RunArtifact.id)).where(db.RunArtifact.run_id.in_(live_runs), db.RunArtifact.expired_at.is_(None)),
    ]
    if excluding_delivery:
        roots[1] = roots[1].where(db.DeliveredFile.id != excluding_delivery)
    if durable_only:
        roots = roots[:4]  # 会话、检查点、运行和访问记录不是用户独立保存的资产。
    # 临时草稿仍可被独立学习任务使用；已发布回执也必须能继续指向业务身份。
    draft_roots = [
        db.ChangeSet.retention_intent.in_(["saved", "legacy"]), db.ChangeSet.status == "published",
        db.ChangeSet.id.in_(select(db.LearningUnit.draft_id).where(db.LearningUnit.draft_id.is_not(None))),
    ]
    if not durable_only:
        draft_roots.extend([
        db.ChangeSet.root_id.in_(select(db.Run.id).where(db.Run.conversation_id.in_(active_conversations))),
        db.ChangeSet.root_id.in_(live_runs),
        db.ChangeSet.publication_root_id.in_(live_runs),
        ])
    roots.append(select(_node("draft:", db.ChangeSet.id)).where(or_(*draft_roots)))
    # 其余独立业务拥有者必须仍存在。派生索引、事件和Evidence表本身均不在此列。
    for model, prefix in ((db.PreferenceRevision, "memory:"), (db.LearningTaskRevision, "learning:"),
                          (db.LearningTask, "learning-current:"), (db.LearningUnit, "learning-unit:"),
                          (db.LearningProfileRevision, "profile:"), (db.LearningProfileImport, "profile-import:"),
                          (db.LearningCollaboration, "collaboration:")):
        columns = list(model.__mapper__.primary_key)
        identity: ColumnElement[str] = cast(getattr(model, str(columns[0].key)), String)
        for column in columns[1:]:
            identity = identity + literal(":") + cast(getattr(model, str(column.key)), String)
        roots.append(select(_node("asset:" + prefix, identity)))
    history = select(_node("history:", db.TaskHistoryRecord.scope + literal(":") + db.TaskHistoryRecord.source_id))
    if excluding_conversation:
        scopes = {_digest(["conversation", excluding_conversation])}
        scopes.update(_digest(["child", child]) for child in session.scalars(select(db.Run.id).where(
            db.Run.root_id.in_(select(db.Run.id).where(db.Run.conversation_id == excluding_conversation)),
            db.Run.parent_id.is_not(None))))
        history = history.where(db.TaskHistoryRecord.scope.not_in(scopes))
    if not durable_only:
        roots.append(history)
    # 这类边由受管工作区的真实正文扫描产生；扫描会移除已不存在的拥有者。
    if workspace_values is None:
        roots.append(select(db.AssetReference.origin_node.label("node")).where(db.AssetReference.owner_kind == "workspace"))
    else:
        for node in sorted(_targets(session, list(workspace_values.values()))):
            roots.append(select(cast(literal(node), String(200)).label("node")))
    # 不带版本的来源链接跟随实体；新增版本不需要重写历史拥有者的引用记录。
    prefix = "durable_" if durable_only else ""
    edges = union(select(db.AssetReference.origin_node.label("origin"), db.AssetReference.target_node.label("target")),
        select(_node("source:", db.SourceVersion.source_id), _node("source_version:", db.SourceVersion.id)),
        select(_node("material:", db.RunArtifact.id), _node("source_version:", db.RunArtifact.promoted_version_id))
            .where(db.RunArtifact.promoted_version_id.is_not(None)),
        select(_node("material_file:", db.RunArtifact.id), _node("material:", db.RunArtifact.id))
            .where(db.RunArtifact.content_storage_key.is_not(None), db.RunArtifact.expired_at.is_(None)),
    ).cte(prefix + "asset_edges")
    seed = union(*roots).cte(prefix + "asset_roots")
    reachable = select(seed.c.node).cte(prefix + "reachable_assets", recursive=True)
    return reachable.union(select(edges.c.target).join(reachable, edges.c.origin == reachable.c.node))
