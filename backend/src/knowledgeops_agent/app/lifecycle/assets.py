"""会话所有权解除后的精确资产回收；不按目录猜测归属，不把已读目录当永久引用。"""

import hashlib
import json

from sqlalchemy import String, cast, delete, func, literal, or_, select

from knowledgeops_agent.app.lifecycle.asset_references import reachable_assets
from knowledgeops_agent.app.lifecycle.catalog import ReferenceCatalog
from knowledgeops_agent.app.lifecycle.files import _digest, _unlink
from knowledgeops_agent.app.lifecycle.workspace import scan_workspace_references
from knowledgeops_agent.persistence import models as db


def conversation_sources(conversation_id):
    roots = select(db.Run.id).where(db.Run.conversation_id == conversation_id)
    return select(db.SourceUse.source_id).where(or_(
        (db.SourceUse.owner_kind == "conversation") & (db.SourceUse.owner_id == conversation_id),
        (db.SourceUse.owner_kind == "run") & db.SourceUse.owner_id.in_(roots),
    ))


def conversation_drafts(conversation_id):
    roots = select(db.Run.id).where(db.Run.conversation_id == conversation_id)
    return select(db.ChangeSet).where(db.ChangeSet.root_id.in_(roots))


def _node(prefix, column):
    return cast(literal(prefix) + column, String(200))


def asset_plan(session, conversation_id):
    """只读预览，并将目标身份/修订及当前工作文件引用纳入预览版本。"""
    sources = select(db.SourceVersion).where(db.SourceVersion.source_id.in_(conversation_sources(conversation_id)))
    drafts = conversation_drafts(conversation_id)
    source_total = session.scalar(select(func.count()).select_from(sources.subquery()))
    draft_total = session.scalar(select(func.count()).select_from(drafts.subquery()))
    if not source_total and not draft_total:
        return {"versions_delete": 0, "versions_keep": 0, "drafts_delete": 0, "drafts_keep": 0,
                "ready": True, "fingerprint": "empty"}
    if not ReferenceCatalog.ready(session):
        return {"versions_delete": 0, "versions_keep": source_total, "drafts_delete": 0,
                "drafts_keep": draft_total, "ready": False, "fingerprint": "pending"}
    values = scan_workspace_references(session.info.get("file_store"))
    graph = reachable_assets(session, excluding_conversation=conversation_id, workspace_values=values)
    nodes = select(graph.c.node)
    # 仅传递身份和修订，不把材料/草稿正文装入删除预览或作业。
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode())
    result: dict = {"versions_delete": 0, "versions_keep": 0, "drafts_delete": 0, "drafts_keep": 0, "ready": True}
    for query, model, prefix, category in (
        (sources, db.SourceVersion, "source_version:", "versions"),
        (drafts, db.ChangeSet, "draft:", "drafts"),
    ):
        kept = _node(prefix, model.id).in_(nodes)
        if model is db.ChangeSet:
            kept = kept | (db.ChangeSet.status == "published")
        columns = [model.id, kept.label("kept")]
        columns += [db.SourceVersion.content_hash] if model is db.SourceVersion else [db.ChangeSet.revision, db.ChangeSet.retention_intent]
        for row in session.execute(query.with_only_columns(*columns).order_by(model.id)).yield_per(100):
            result[category + ("_keep" if row.kept else "_delete")] += 1
            digest.update(json.dumps(list(row), sort_keys=True).encode())
    result["fingerprint"] = digest.hexdigest()
    return result


def remove_draft(session, draft):
    """实际拥有者图已经证明无依赖后，删除临时草稿及其派生边和修订。"""
    if draft.status == "published":
        raise ValueError("published identity must retain its idempotency receipt")
    session.execute(delete(db.ChangeSetRevision).where(db.ChangeSetRevision.changeset_id == draft.id))
    session.execute(delete(db.AssetReference).where(or_(db.AssetReference.origin_node == "draft:" + draft.id,
        db.AssetReference.target_node == "draft:" + draft.id)))
    session.execute(delete(db.ArtifactRetentionReference).where(db.ArtifactRetentionReference.owner_kind == "draft",
        or_(db.ArtifactRetentionReference.owner_id == "current:" + draft.id,
            db.ArtifactRetentionReference.owner_id.startswith(draft.id + ":"))))
    session.delete(draft)


def _remove_version(session, version):
    """先解除DB身份并返回精确文件清单；作业提交后下一阶段才碰物理原件。"""
    original = None
    if version.storage_key:
        if session.info.get("source_store") is None:
            raise ValueError("source cleanup requires the configured source store")
        digest = _digest(version.original_hash or version.content_hash)
        if version.storage_key != f"{digest[:2]}/{digest}":
            raise ValueError("source original is outside its content-addressed storage")
        original = [version.storage_key, digest]
    segments = select(db.SourceSegment.id).where(db.SourceSegment.source_version_id == version.id)
    session.execute(delete(db.Evidence).where(db.Evidence.source_segment_id.in_(segments)))
    session.execute(delete(db.AssetReference).where(or_(db.AssetReference.origin_node == "source_version:" + version.id,
        db.AssetReference.target_node == "source_version:" + version.id)))
    session.execute(delete(db.ArtifactRetentionReference).where(db.ArtifactRetentionReference.owner_kind == "asset",
        or_(db.ArtifactRetentionReference.owner_id == "source-body:" + version.id,
            db.ArtifactRetentionReference.owner_id.in_(select(literal("source-segment:") + db.SourceSegment.id).where(
                db.SourceSegment.source_version_id == version.id)))))
    session.execute(delete(db.SourceSegment).where(db.SourceSegment.source_version_id == version.id))
    source_id = version.source_id
    session.delete(version)
    session.flush()
    if not session.scalar(select(db.SourceVersion.id).where(db.SourceVersion.source_id == source_id).limit(1)):
        session.execute(delete(db.AssetReference).where(db.AssetReference.target_node == "source:" + source_id))
        session.execute(delete(db.Source).where(db.Source.id == source_id))
    return original


def collect_source_files(session, originals):
    """元数据已提交移除；控制锁下重查共享者，后续新导入也会阻止删除其原件。

    文件删除后进程崩溃只会重放此清单，不会把已经失去原件的旧版本重新开放。
    """
    store = session.info.get("source_store")
    if originals and store is None:
        raise ValueError("source cleanup requires the configured source store")
    for key, digest in originals:
        if key != f"{_digest(digest)[:2]}/{digest}":
            raise ValueError("invalid source cleanup manifest")
        if not session.scalar(select(db.SourceVersion.id).where(db.SourceVersion.storage_key == key).limit(1)):
            _unlink(store, key, digest)


def collect_conversation_assets(session, conversation_id, *, limit):
    """每次先删一批无依赖草稿，再重算材料可达性，最后解除该会话的使用关系。

    调用者必须持工作区锁及控制锁，会话已封闭且运行详情已清理。正式知识、其他
    存活会话、独立资产都仍是根；临时材料互引不阻止没有存活根的循环被回收。
    """
    if not ReferenceCatalog.ready(session):
        raise ValueError("reference catalog is incomplete")
    graph = reachable_assets(session, excluding_conversation=conversation_id,
                             workspace_values=scan_workspace_references(session.info.get("file_store")))
    nodes = select(graph.c.node)
    drafts = list(session.scalars(conversation_drafts(conversation_id).where(
        db.ChangeSet.retention_intent == "temporary", db.ChangeSet.status != "published", ~_node("draft:", db.ChangeSet.id).in_(nodes),
    ).order_by(db.ChangeSet.id).limit(limit)))
    for draft in drafts:
        remove_draft(session, draft)
    if drafts:
        return {"more": True, "drafts_deleted": len(drafts), "versions_deleted": 0}
    versions = list(session.scalars(select(db.SourceVersion).join(db.Source).where(
        db.SourceVersion.source_id.in_(conversation_sources(conversation_id)), db.Source.ownership_known,
        ~_node("source_version:", db.SourceVersion.id).in_(nodes),
    ).order_by(db.SourceVersion.id).limit(limit)))
    originals = []
    for version in versions:
        if original := _remove_version(session, version):
            originals.append(original)
    if versions:
        return {"more": True, "drafts_deleted": 0, "versions_deleted": len(versions), "originals": originals}
    session.execute(delete(db.SourceUse).where(or_(
        (db.SourceUse.owner_kind == "conversation") & (db.SourceUse.owner_id == conversation_id),
        (db.SourceUse.owner_kind == "run") & db.SourceUse.owner_id.in_(select(db.Run.id).where(
            db.Run.conversation_id == conversation_id)),
    )))
    return {"more": False, "drafts_deleted": 0, "versions_deleted": 0}
