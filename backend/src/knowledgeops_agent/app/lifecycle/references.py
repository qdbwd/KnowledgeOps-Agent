"""材料访问目录与真实正文引用分开；引用只延长保留，不扩大读取权限。"""

import re

from langchain_core.messages import BaseMessage
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert

from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.persistence.models import ArtifactRetentionReference, Run, RunArtifact

_UUID = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}(?![0-9a-fA-F])")
_IDENTITY_KEYS = {"artifact_id", "original_artifact_id", "summary_artifact_id"}


def mentioned_artifacts(value) -> set[str]:
    """保守识别正文、工具参数及内部链接中的ID；只在PG已存在的材料上落引用。

    UUID出现在原文中也视为引用，避免把用户的说明当目录丢弃。调用方明确
    移除顶层访问目录，不能泛化为忽略所有列表或所有名为artifact_ids的字段。
    """
    result: set[str] = set()
    if isinstance(value, str):
        result.update(match.group().lower() for match in _UUID.finditer(value))
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in _IDENTITY_KEYS and isinstance(child, str):
                result.add(child)
            result.update(mentioned_artifacts(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            result.update(mentioned_artifacts(child))
    elif isinstance(value, BaseMessage):
        result.update(mentioned_artifacts(value.model_dump()))
    return result


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child)
    elif isinstance(value, BaseMessage):
        yield from _strings(value.model_dump())


def reachable_artifacts():
    """实际拥有者和仍可恢复的运行是根，材料间链接仅作为边。

    UNION去重使循环收敛；两个无人使用的旧材料相互引用不因此永久保留。
    该集合只控制保留，不用于扩大读取授权。
    """
    reference = ArtifactRetentionReference
    direct = select(reference.artifact_id).where(
        reference.artifact_id == RunArtifact.id, reference.owner_kind != "material",
    ).correlate(RunArtifact).exists()
    reachable = select(RunArtifact.id).join(Run, Run.id == RunArtifact.run_id).where(
        RunArtifact.expired_at.is_(None), or_(Run.lifecycle_state == "active", direct),
    ).cte("reachable_materials", recursive=True)
    return reachable.union(select(reference.artifact_id).join(
        reachable, reachable.c.id == reference.owner_id,
    ).where(reference.owner_kind == "material"))


def replace_references(session, owner_kind: str, owner_id: str, value, *, snapshot: bool = False) -> set[str]:
    """与拥有者事务一起更新引用；清理器使用同一控制锁复查，不在预览后盲删。"""
    if owner_kind not in {"snapshot", "message", "history", "draft", "asset", "checkpoint", "run", "run_input", "material", "workspace"}:
        raise ValueError("unsupported artifact reference owner")
    lock_control(session)
    if snapshot:
        # 目录表示该快照允许访问哪些材料，不证明它的工作正文仍在使用全部材料。
        value = {key: child for key, child in value.items() if key != "artifact_ids"}
    candidates = mentioned_artifacts(value)
    present = set(session.scalars(select(RunArtifact.id).where(RunArtifact.id.in_(candidates)))) if candidates else set()
    # 用户/工具也可能保存内部文件地址而非artifact ID；地址中的run UUID先缩小查询。
    if candidates:
        strings = list(_strings(value))
        for row in session.scalars(select(RunArtifact).where(
            RunArtifact.run_id.in_(candidates), RunArtifact.content_storage_key.is_not(None),
        )):
            if any(row.content_storage_key in text for text in strings):
                present.add(row.id)
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
