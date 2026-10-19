"""学习配置的单一写入入口：文件存人工正文，PG存版本与来源。

自动部分由有效记忆确定性生成，不用模型改写人工文档。运行冻结内容与身份，
普通编辑下一Run生效；停用和记忆撤回在下次调用前过滤，历史文件保留。
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

from sqlalchemy import select, text

from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.core.resources import package_resource
from knowledgeops_agent.memory.service import MemoryService
from knowledgeops_agent.persistence.models import (
    LearningProfile,
    LearningProfileImport,
    LearningProfileRevision,
    LearningTask,
    new_id,
)

DEFAULT_PATH = package_resource("prompts/learning-profile.md")


def _validate_content(content: str) -> None:
    if len(content) > 20000 or "\x00" in content:
        raise ValueError("学习配置正文最多20000字符，且不能包含空字符")


class LearningProfileService:
    def __init__(self, sessions):
        self.sessions = sessions

    @staticmethod
    def _store(session):
        store = session.info.get("file_store")
        if store is None:
            raise ValueError("学习配置需要已配置的项目工作目录")
        return store

    @staticmethod
    def _locked(session, profile_id, expected_revision=None):
        lock_control(session)
        row = session.scalar(
            select(LearningProfile).where(LearningProfile.id == profile_id).with_for_update()
        )
        if row is None:
            raise KeyError("学习配置不存在")
        if expected_revision is not None and row.revision != expected_revision:
            raise ValueError("学习配置修订已变化，请刷新后重试")
        return row

    def _write(self, session, row, content, source):
        _validate_content(content)
        session.flush()
        digest = hashlib.sha256(content.encode()).hexdigest()
        # 失败事务留下的文件不能占住未来修订的路径；带内容哈希保持可重试。
        key = f"profiles/versions/{row.id}/{row.revision}-{digest}.md"
        self._store(session).put(key, content, session=session, purpose="profile")
        revision = LearningProfileRevision(
            profile_id=row.id,
            revision=row.revision,
            storage_key=key,
            sha256=digest,
            source=source,
            enabled=row.enabled,
        )
        session.add(revision)
        session.flush()
        return self._view(session, row, revision)

    def _view(self, session, row, revision=None):
        version = revision or session.get(LearningProfileRevision, (row.id, row.revision))
        if version is None:
            raise ValueError("学习配置版本缺失，不能用空正文替代")
        return {
            "id": row.id,
            "task_id": row.task_id,
            "revision": version.revision,
            "enabled": version.enabled,
            "revocation_epoch": row.revocation_epoch,
            "content": self._store(session).read(version.storage_key, version.sha256),
            "sha256": version.sha256,
            "source": version.source,
            "created_at": version.created_at.isoformat(),
        }

    def _ensure(self, session, task_id=None):
        # 与引用维护/会话来源清理统一顺序：先控制锁，再身份锁和资产行锁。
        lock_control(session)
        identity = "task:" + task_id if task_id else "global"
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "profile:" + identity},
        )
        if task_id and session.get(LearningTask, task_id) is None:
            raise KeyError("学习任务不存在")
        row = session.scalar(
            select(LearningProfile).where(LearningProfile.identity_key == identity)
        )
        if row is None:
            row = LearningProfile(identity_key=identity, task_id=task_id)
            session.add(row)
            self._write(session, row, "", {"kind": "default", "note": "尚无人工配置"})
        return row

    def get(self, *, task_id=None) -> dict:
        with self.sessions.begin() as session:
            return self._view(session, self._ensure(session, task_id))

    def by_id(self, profile_id: str) -> dict:
        with self.sessions() as session:
            row = session.get(LearningProfile, profile_id)
            if row is None:
                raise KeyError("学习配置不存在")
            return self._view(session, row)

    def update(
        self,
        profile_id: str,
        *,
        expected_revision: int,
        content: str | None = None,
        enabled: bool | None = None,
        source: dict | None = None,
    ) -> dict:
        with self.sessions.begin() as session:
            row = self._locked(session, profile_id, expected_revision)
            old = self._view(session, row)
            if enabled is not None and enabled != row.enabled:
                row.enabled = enabled
                # 再启用也不恢复旧快照，防止关闭期间的运行引用旧的资格。
                row.revocation_epoch += 1
            row.revision += 1
            return self._write(
                session,
                row,
                old["content"] if content is None else content,
                source or {"kind": "manual"},
            )

    def history(self, profile_id: str) -> list[dict]:
        with self.sessions() as session:
            row = session.get(LearningProfile, profile_id)
            if row is None:
                raise KeyError("学习配置不存在")
            return [
                self._view(session, row, version)
                for version in session.scalars(
                    select(LearningProfileRevision)
                    .where(LearningProfileRevision.profile_id == profile_id)
                    .order_by(LearningProfileRevision.revision)
                )
            ]

    def rollback(self, profile_id: str, *, expected_revision: int, target_revision: int) -> dict:
        with self.sessions.begin() as session:
            row = self._locked(session, profile_id, expected_revision)
            old = session.get(LearningProfileRevision, (profile_id, target_revision))
            if old is None:
                raise KeyError("学习配置旧版本不存在")
            content = self._store(session).read(old.storage_key, old.sha256)
            if row.enabled != old.enabled:
                row.enabled, row.revocation_epoch = old.enabled, row.revocation_epoch + 1
            row.revision += 1
            return self._write(
                session, row, content, {"kind": "manual", "restored_revision": target_revision}
            )

    def preview_import(
        self, profile_id: str, *, expected_revision: int, filename: str, content: str
    ) -> dict:
        """只存固定预览文本。不解析执行Skill、shell、工具配置或网络地址。"""
        _validate_content(content)
        filename = PurePosixPath(filename.replace("\\", "/")).name
        if not filename.lower().endswith(".md") or len(filename) > 200:
            raise ValueError("请选择Markdown或SKILL.md文本文件")
        with self.sessions.begin() as session:
            self._locked(session, profile_id, expected_revision)
            preview_id = new_id()
            key = f"profiles/imports/{preview_id}.md"
            digest = self._store(session).put(key, content, session=session, purpose="profile_import")
            session.add(
                LearningProfileImport(
                    id=preview_id,
                    profile_id=profile_id,
                    expected_revision=expected_revision,
                    filename=filename,
                    storage_key=key,
                    sha256=digest,
                )
            )
            return {
                "id": preview_id,
                "profile_id": profile_id,
                "expected_revision": expected_revision,
                "filename": filename,
                "content": content,
                "sha256": digest,
                "text_only": True,
                "notice": "应用后替换这一层的人工正文；不会执行脚本、启用工具或修改其他层配置。",
            }

    def apply_import(self, profile_id: str, *, preview_id: str, expected_hash: str) -> dict:
        with self.sessions.begin() as session:
            row = self._locked(session, profile_id)
            preview = session.scalar(
                select(LearningProfileImport)
                .where(LearningProfileImport.id == preview_id)
                .with_for_update()
            )
            if (
                preview is None
                or preview.profile_id != profile_id
                or preview.sha256 != expected_hash
            ):
                raise ValueError("导入预览身份或内容已变化，请重新预览")
            if preview.applied_revision is not None:
                return self._view(
                    session,
                    row,
                    session.get(LearningProfileRevision, (row.id, preview.applied_revision)),
                )
            if row.revision != preview.expected_revision:
                raise ValueError("学习配置修订已变化，请重新预览，不能覆盖后来编辑")
            content = self._store(session).read(preview.storage_key, preview.sha256)
            row.revision += 1
            result = self._write(
                session,
                row,
                content,
                {"kind": "import", "filename": preview.filename, "preview_id": preview.id},
            )
            preview.applied_revision = row.revision
            return result

    def freeze(self, *, task_id: str | None = None, query: str, memory: dict | None = None) -> dict:
        """有效角色由默认说明+两层人工配置+有来源的自动条目组成，无额外模型调用。"""
        with self.sessions.begin() as session:
            task = session.get(LearningTask, task_id) if task_id else None
            if task_id and task is None:
                raise KeyError("学习任务不存在")
            profiles = [self._view(session, self._ensure(session))]
            if task_id:
                profiles.append(self._view(session, self._ensure(session, task_id)))
            topic = task.topic if task else ""
        selected = memory or MemoryService(self.sessions).freeze(query[:500], topic=topic)
        active = {p["task_id"]: p for p in profiles if p["enabled"]}
        automatic = []
        for item in selected["selected"]:
            layer = active.get(None if item["scope"] == "global" else task_id)
            if layer and item["category"] in {"preference", "behavior", "cognitive", "correction"}:
                automatic.append({**item, "profile_id": layer["id"]})
        return {
            "contract": "learning-profile-v1",
            "task_id": task_id,
            "task_configuration": task.configuration if task and task_id in active else {},
            "default": DEFAULT_PATH.read_text(encoding="utf-8"),
            "profiles": [p for p in profiles if p["enabled"]],
            "automatic": automatic,
            "memory_ids": [r["id"] for r in automatic],
        }

    def effective(self, frozen: dict) -> dict:
        """恢复沿用正文；动态撤回只移除对应部分，避免另一份自动摘要重新带回。"""
        with self.sessions() as session:
            current = {
                row.id: row
                for row in session.scalars(
                    select(LearningProfile).where(
                        LearningProfile.id.in_([p["id"] for p in frozen["profiles"]])
                    )
                )
            }
        valid = [
            p
            for p in frozen["profiles"]
            if p["id"] in current
            and current[p["id"]].enabled
            and current[p["id"]].revocation_epoch == p["revocation_epoch"]
        ]
        valid_ids = {p["id"] for p in valid}
        automatic = MemoryService(self.sessions).effective(
            [m for m in frozen["automatic"] if m["profile_id"] in valid_ids]
        )
        return {
            **frozen,
            "profiles": valid,
            "task_configuration": frozen.get("task_configuration", {})
            if any(p["task_id"] == frozen.get("task_id") for p in valid)
            else {},
            "automatic": automatic,
            "memory_ids": [m["id"] for m in automatic],
        }

    def export(self, profile_id: str) -> str:
        """导出可移植人工正文；自动记忆保持带来源的单独视图，不变成第二份人工真相。"""
        row = self.by_id(profile_id)
        return row["content"]

    def export_effective(self, *, task_id: str | None = None) -> str:
        """完整角色说明是可携带的快照；不作为第二份可自动同步的配置真相。"""
        value = self.effective(self.freeze(task_id=task_id, query="学习解释与知识整理"))
        lines = [
            "# 学习配置与读者角色",
            "",
            "这是当前有效配置快照，来源版本见各节。",
            "",
            "## 默认职责",
            "",
            value["default"],
        ]
        for profile in value["profiles"]:
            title = "任务人工配置" if profile["task_id"] else "通用人工配置"
            lines.extend(
                ["", f"## {title}（修订{profile['revision']}）", "", profile["content"] or "（无）"]
            )
        lines.extend(["", "## 记忆生成部分", ""])
        for memory in value["automatic"]:
            lines.append(f"- {memory['content']}（来源：{memory['id']}，修订{memory['revision']}）")
        lines.extend(["", "## 任务具体要求", ""])
        for key, entry in value.get("task_configuration", {}).items():
            lines.append(f"- {key}：{entry['value']}")
        return "\n".join(lines) + "\n"
