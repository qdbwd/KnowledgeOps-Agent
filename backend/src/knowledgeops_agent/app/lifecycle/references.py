"""材料访问目录与真实正文引用分开；引用只延长保留，不扩大读取权限。"""

from sqlalchemy import delete, literal, select
from sqlalchemy.dialects.postgresql import insert

from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.persistence.models import ArtifactRetentionReference, RunArtifact


def mentioned_artifacts(value) -> set[str]:
    """识别明确链接、工具参数和旧补读指令；普通编号及可访问目录不作为引用。"""
    from knowledgeops_agent.app.lifecycle.asset_references import candidates

    return {identity for kind, identity in candidates(value) if kind == "material"}


def reachable_artifacts(session):
    """沿统一资产图查询，旧派生表存在一条记录不再单独赋予保留权。"""
    from knowledgeops_agent.app.lifecycle.asset_references import reachable_assets

    reachable = reachable_assets(session)
    return select(RunArtifact.id).where(
        (literal("material:") + RunArtifact.id).in_(select(reachable.c.node)),
    ).cte("reachable_materials")


def replace_references(session, owner_kind: str, owner_id: str, value, *, snapshot: bool = False) -> set[str]:
    """与拥有者事务一起更新引用；清理器使用同一控制锁复查，不在预览后盲删。"""
    if owner_kind not in {"snapshot", "message", "history", "draft", "asset", "checkpoint", "run", "run_input", "material", "workspace", "delivery"}:
        raise ValueError("unsupported artifact reference owner")
    lock_control(session)
    if snapshot:
        # 目录表示该快照允许访问哪些材料，不证明它的工作正文仍在使用全部材料。
        value = {key: child for key, child in value.items() if key != "artifact_ids"}
    from knowledgeops_agent.app.lifecycle.asset_references import replace_asset_references

    nodes = replace_asset_references(session, owner_kind, owner_id, value)
    # 旧表暂作兼容派生镜像，回收决策只读新的实际拥有者图。
    present = {node.split(":", 1)[1] for node in nodes if node.startswith(("material:", "material_file:"))}
    session.execute(delete(ArtifactRetentionReference).where(
        ArtifactRetentionReference.owner_kind == owner_kind,
        ArtifactRetentionReference.owner_id == owner_id,
        ArtifactRetentionReference.artifact_id.not_in(present),
    ))
    for identity in sorted(present):
        session.execute(insert(ArtifactRetentionReference).values(
            artifact_id=identity, owner_kind=owner_kind, owner_id=owner_id, reason="content_reference",
        ).on_conflict_do_nothing())
    return present
