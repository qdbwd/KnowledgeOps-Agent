"""完整技能包管理：预览不激活；配置锁内替换，事务日志跨进程恢复。

DeerFlow 的暂存/提交与完整包边界用作设计参照；这里复用本项目PG配置锁、
ToolPolicy撤回代次及冻结资源，不导入上游应用或创建独立技能执行框架。
"""

from __future__ import annotations

import io
import json
import zipfile
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select

from knowledgeops_agent.harness.skills.imports import inspect_package, package_digest
from knowledgeops_agent.harness.skills.packages import SkillPackageError, package_catalog
from knowledgeops_agent.harness.skills.presentation import present_skill
from knowledgeops_agent.harness.skills.storage import FORMAT, SkillStorage, sync_directory
from knowledgeops_agent.harness.tools.configuration import lock_configuration
from knowledgeops_agent.harness.tools.policy import read_policy, save_policy
from knowledgeops_agent.persistence.models import (
    ExtensionOperation,
    SkillPackageState,
    new_id,
    utcnow,
)


def package_view(row: SkillPackageState) -> dict:
    return {"name": row.name, "revision": row.revision, "content_hash": row.content_hash, "removed": row.removed}


class SkillManagement:
    def __init__(self, sessions, root: Path):
        self.sessions = sessions
        self.storage = SkillStorage(root)
        self.root = self.storage.root

    def _checkpoint(self, _event: str) -> None:
        """事务关键点供故障注入；正常运行不执行额外动作。"""

    def catalog(self) -> list[dict]:
        with self.sessions.begin() as session:
            lock_configuration(session)
            self._recover_in(session)
            policy = read_policy(session)
            states = {row.name: row for row in session.scalars(select(SkillPackageState))}
            return [self._present(item, states.get(item["name"]), policy) for item in package_catalog(self.root)
                    if states.get(item["name"]) is None or not states[item["name"]].removed]

    @staticmethod
    def _present(item, state, policy):
        return {**present_skill(item), "available": item["enabled"],
                "enabled": item["enabled"] and policy.get("skill_enabled", {}).get(item["name"], True),
                "policy_revision": policy["revision"], "revision": state.revision if state else 0,
                "managed": state is not None}

    def detail(self, name: str, *, resource: str | None = None) -> dict:
        from knowledgeops_agent.harness.skills.details import package_detail

        with self.sessions.begin() as session:
            lock_configuration(session)
            self._recover_in(session)
            state = session.get(SkillPackageState, name)
            if state is not None and state.removed:
                raise KeyError("技能不存在")
            detail = package_detail(self.root, name, resource=resource)
            return detail if resource is not None else self._present(detail, state, read_policy(session))

    def stage(self, files, *, name=None, description=None, expected_revision=0, _bootstrap_id=None) -> dict:
        inspected = inspect_package(files, name=name, description=description)
        with self.sessions.begin() as session:
            lock_configuration(session)
            self.storage.initialize()
            self._recover_in(session)
            previous = session.get(SkillPackageState, inspected.name) if inspected.name else None
            if expected_revision == 0 and previous is not None and previous.removed:
                # 重新导入沿用移除身份与撤回代次；页面不必先展示已删除条目。
                expected_revision = previous.revision
            parameters = {"content_hash": inspected.digest}
            if _bootstrap_id is not None:
                bootstrap = session.get(ExtensionOperation, _bootstrap_id)
                if (bootstrap is None or bootstrap.kind != "skill_bootstrap" or bootstrap.status != "ready"
                    or bootstrap.parameters.get("mode") != "seed"
                    or bootstrap.parameters.get("seeds", {}).get(inspected.name) != inspected.digest):
                    raise ValueError("初始技能不在本次部署清单中")
                parameters["bootstrap_id"] = _bootstrap_id
            if previous is not None and not previous.removed and previous.revision == expected_revision:
                actual = package_digest(self.storage.read_files(self.storage.package(previous.name)))
                parameters.update(current_hash=actual, local_changes=actual != previous.content_hash)
            operation = ExtensionOperation(
                id=new_id(), kind="skill_import", target=inspected.name, status="uploading",
                expected_revision=expected_revision,
                parameters=parameters, result={}, expires_at=utcnow() + timedelta(days=1),
            )
            session.add(operation)
            session.flush()
            identity = operation.id
        # 先持久登记归属，进程在写上传文件时退出也能按到期时间回收。
        with self.sessions.begin() as session:
            lock_configuration(session)
            operation = self._get_operation(session, identity)
            self.storage.write_candidate(operation.id, inspected)
            operation.status = "ready"
            return self._operation_view(operation, inspected)

    @staticmethod
    def _operation_view(operation, inspected=None):
        value = {"id": operation.id, "kind": operation.kind, "target": operation.target,
                 "status": operation.status, "result": deepcopy(operation.result),
                 "error_code": operation.error_code, "expires_at": operation.expires_at}
        if inspected is not None:
            value["preview"] = {**inspected.preview(), "local_changes": operation.parameters.get("local_changes", False)}
        return value

    def operation(self, identity: str) -> dict:
        with self.sessions.begin() as session:
            lock_configuration(session)
            self._recover_in(session)
            operation = self._get_operation(session, identity)
            inspected = None
            if operation.kind == "skill_import" and operation.status in {"ready", "failed"}:
                inspected = self.storage.read(self.storage.operation_path("staging", identity) / "candidate")
            return self._operation_view(operation, inspected)

    def trial_read(self, identity: str, *, path: str = "SKILL.md", offset: int = 0) -> dict:
        """复用Agent的冻结文件读取器试读；不创建Run、模型请求或命令进程。"""
        from knowledgeops_agent.harness.skills.resources import FrozenSkillBackend

        with self.sessions.begin() as session:
            lock_configuration(session, shared=True)
            operation = self._get_operation(session, identity)
            if operation.kind != "skill_import" or operation.status not in {"ready", "failed"}:
                raise ValueError("当前上传不可试读，请重新上传")
            inspected = self.storage.read(self.storage.operation_path("staging", identity) / "candidate")
            if inspected.missing:
                raise ValueError("请先补填名称和用途")
            if path not in inspected.files:
                raise KeyError("技能资源不存在")
            backend = FrozenSkillBackend({f"/skills/{inspected.name}/{name}": data for name, data in inspected.files.items()})
            result = backend.read(f"/skills/{inspected.name}/{path}", offset=offset, limit=1000)
            if result.error or result.file_data is None:
                raise ValueError("该资源无法按文本试读，请确认文件类型和读取位置")
            return {"path": path, "content": result.file_data["content"], "next_offset": result.next_offset,
                    "total_lines": result.total_lines, "execution_tested": False}

    def discard(self, identity: str) -> dict:
        with self.sessions.begin() as session:
            lock_configuration(session)
            self._recover_in(session)
            operation = self._get_operation(session, identity)
            if operation.kind != "skill_import" or operation.status == "succeeded":
                raise ValueError("已提交的包请通过移除入口管理")
            self.storage.remove_owned(self.storage.operation_path("staging", identity))
            operation.status, operation.updated_at = "cancelled", utcnow()
            return self._operation_view(operation)

    @staticmethod
    def _get_operation(session, identity):
        operation = session.get(ExtensionOperation, identity)
        if operation is None or operation.kind not in {"skill_import", "skill_remove"}:
            raise KeyError("技能操作不存在")
        return operation

    def _current(self, session, name: str, expected: int, *, content_hash=None) -> tuple[SkillPackageState | None, Path]:
        row = session.get(SkillPackageState, name)
        if (row.revision if row else 0) != expected:
            raise ValueError("技能包修订已变化，请刷新后重新确认")
        current = self.storage.package(name)
        if row is None or row.removed:
            if current.exists():
                raise ValueError("发现未接管的本地文件，请先接管已有技能目录")
        else:
            try:
                actual = package_digest(self.storage.read_files(current))
            except (ValueError, OSError) as exc:
                raise ValueError("本地文件已更改或损坏，请核对后重新导入") from exc
            if actual != (content_hash or row.content_hash):
                raise ValueError("本地文件在上次检查后已更改，请重新检查并确认")
        return row, current

    def commit(self, identity: str) -> dict:
        try:
            with self.sessions.begin() as session:
                lock_configuration(session)
                self._recover_in(session)
                operation = self._get_operation(session, identity)
                if operation.status == "succeeded":
                    return deepcopy(operation.result["package"])
                if operation.status not in {"ready", "failed"} or (
                    operation.expires_at is not None and operation.expires_at <= utcnow()
                ):
                    raise ValueError("上传已过期或操作已取消，请重新上传")
                inspected = None
                if operation.kind == "skill_import":
                    inspected = self.storage.read(self.storage.operation_path("staging", identity) / "candidate")
                    if inspected.missing:
                        raise ValueError("请先补填技能名称和用途，再确认导入")
                    if inspected.digest != operation.parameters["content_hash"] or inspected.name != operation.target:
                        raise ValueError("待导入文件已变化，请重新检查")
                row, current = self._current(session, operation.target, operation.expected_revision,
                                             content_hash=operation.parameters.get("current_hash"))
                existed = row is not None and not row.removed
                record = {
                    "format": FORMAT, "operation_id": identity, "name": operation.target,
                    "kind": operation.kind, "had_current": existed,
                    "old_hash": operation.parameters.get("current_hash", row.content_hash) if row is not None and existed else None,
                    "new_hash": inspected.digest if inspected else None,
                    "old_revision": operation.expected_revision,
                }
                journal = self.storage.operation_path("journal", identity)
                self.storage.write_json(journal, record)
                if existed:
                    self.storage.move(current, self.storage.operation_path("rollback", identity))
                self._checkpoint("old_moved")
                if inspected:
                    self.storage.move(self.storage.operation_path("staging", identity) / "candidate", current)
                self._checkpoint("new_moved")
                row = self._save_state(session, row, operation, inspected, existed)
                operation.status, operation.error_code = "succeeded", None
                operation.result = {"package": package_view(row)}
                operation.updated_at = utcnow()
                operation.attempts += 1
                result = deepcopy(operation.result["package"])
            self._checkpoint("database_committed")
            self.recover()
            return result
        except (OSError, SkillPackageError) as exc:
            # 数据库未提交就回滚目录；若清理本身失败，日志保留给下一次恢复。
            self.recover()
            with self.sessions.begin() as session:
                lock_configuration(session)
                operation = self._get_operation(session, identity)
                if operation.status != "succeeded":
                    operation.status, operation.error_code = "failed", type(exc).__name__
                    operation.updated_at = utcnow()
            raise ValueError("技能包写入失败，原版本已恢复；可重试或重新上传") from exc

    @staticmethod
    def _save_state(session, row, operation, inspected, existed):
        if row is None:
            row = SkillPackageState(name=operation.target, revision=0, content_hash="", removed=False)
            session.add(row)
        row.revision += 1
        row.removed = inspected is None
        if inspected:
            row.content_hash = inspected.digest
        row.updated_at = utcnow()
        current_policy = read_policy(session)
        updated = deepcopy(current_policy)
        if row.removed:
            epochs = updated.setdefault("skill_epochs", {})
            epochs[row.name] = epochs.get(row.name, 0) + 1
            updated.setdefault("skill_enabled", {})[row.name] = False
        elif (not existed and not operation.parameters.get("bootstrap_id")) or not inspected.compatible:
            # 导入不授予使用许可；不兼容更新会收窄许可，普通兼容更新保留开关。
            if existed and updated.get("skill_enabled", {}).get(row.name, True):
                epochs = updated.setdefault("skill_epochs", {})
                epochs[row.name] = epochs.get(row.name, 0) + 1
            updated.setdefault("skill_enabled", {})[row.name] = False
        if updated != current_policy:
            save_policy(session, current_policy, updated)
        return row

    def remove(self, name: str, *, expected_revision: int) -> dict:
        with self.sessions.begin() as session:
            lock_configuration(session)
            self.storage.initialize()
            self._recover_in(session)
            row, _ = self._current(session, name, expected_revision)
            if row is None:
                raise KeyError("技能不存在")
            if row.removed:
                return package_view(row)
            operation = ExtensionOperation(id=new_id(), kind="skill_remove", target=name, status="ready",
                                           expected_revision=expected_revision, parameters={}, result={},
                                           expires_at=utcnow() + timedelta(days=1))
            session.add(operation)
            identity = operation.id
        return self.commit(identity)

    def recover(self) -> None:
        with self.sessions.begin() as session:
            lock_configuration(session)
            self._recover_in(session)

    def _recover_in(self, session) -> None:
        directory = self.storage.checked(".management/journal")
        if not directory.exists():
            return
        for journal in sorted(directory.glob("*.json")):
            identity = journal.stem
            journal = self.storage.operation_path("journal", identity)
            with journal.open("rb") as stream:
                record = json.loads(stream.read(4096))
            operation = self._get_operation(session, identity)
            if (record.get("format") != FORMAT or record.get("operation_id") != identity
                or record.get("name") != operation.target or record.get("kind") != operation.kind
                or record.get("old_revision") != operation.expected_revision):
                raise SkillPackageError("技能恢复日志不匹配，保留文件等待核对")
            self._recover_record(record, operation.status == "succeeded")
            journal.unlink()
            sync_directory(journal.parent)

    def _matches(self, path: Path, digest: str | None) -> bool:
        return digest is not None and path.exists() and package_digest(self.storage.read_files(path)) == digest

    def _recover_record(self, record: dict, committed: bool) -> None:
        identity = record["operation_id"]
        current = self.storage.package(record["name"])
        backup = self.storage.operation_path("rollback", identity)
        stage = self.storage.operation_path("staging", identity)
        candidate = stage / "candidate"
        if committed:
            if record["kind"] == "skill_import":
                if not self._matches(current, record["new_hash"]):
                    raise SkillPackageError("已提交技能内容不匹配，保留恢复文件")
            elif current.exists():
                raise SkillPackageError("已移除技能目录重新出现，保留待核对")
            if backup.exists():
                if not self._matches(backup, record["old_hash"]):
                    raise SkillPackageError("技能恢复副本被修改，停止清理")
                self.storage.remove_owned(backup)
            self.storage.remove_owned(stage)
            return
        if backup.exists():
            if not self._matches(backup, record["old_hash"]):
                raise SkillPackageError("技能恢复副本不匹配")
            if current.exists():
                if not self._matches(current, record["new_hash"]):
                    raise SkillPackageError("未提交技能内容不匹配，停止恢复")
                self.storage.move(current, candidate)
            self.storage.move(backup, current)
        elif record["had_current"]:
            if not self._matches(current, record["old_hash"]):
                raise SkillPackageError("旧技能内容丢失或变化，停止恢复")
        elif current.exists():
            if not self._matches(current, record["new_hash"]):
                raise SkillPackageError("未提交目录不属于本操作")
            self.storage.move(current, candidate)

    def adopt_existing(self) -> list[str]:
        """部署接续时显式接管已有完整包，既不重写正文也不改变原来的启停策略。"""
        adopted = []
        with self.sessions.begin() as session:
            lock_configuration(session)
            self.storage.initialize()
            self._recover_in(session)
            for directory in sorted(self.root.iterdir()):
                if directory.name.startswith(".") or not (directory / "SKILL.md").is_file():
                    continue
                if session.get(SkillPackageState, directory.name) is not None:
                    continue
                inspected = self.storage.read(directory)
                if inspected.missing or inspected.name != directory.name:
                    raise ValueError("已有技能包需要先修正名称与用途")
                session.add(SkillPackageState(name=directory.name,
                                             content_hash=package_digest(self.storage.read_files(directory))))
                adopted.append(directory.name)
        return adopted

    def export(self, name: str) -> bytes:
        with self.sessions.begin() as session:
            lock_configuration(session)
            self._recover_in(session)
            row = session.get(SkillPackageState, name)
            if row is None or row.removed:
                raise KeyError("技能不存在")
            files = self.storage.read_files(self.storage.package(name))
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
                for path, data in files.items():
                    bundle.writestr(f"{name}/{path}", data)
            return output.getvalue()

    def expire_staging(self) -> int:
        """维护器调用；仅回收DB登记的过期上传，不扫描用户目录认领未知文件。"""
        count = 0
        with self.sessions.begin() as session:
            lock_configuration(session)
            self._recover_in(session)
            rows = session.scalars(select(ExtensionOperation).where(
                ExtensionOperation.kind.in_(["skill_import", "skill_remove"]),
                ExtensionOperation.status.in_(["uploading", "ready", "failed"]),
                ExtensionOperation.expires_at <= utcnow(),
            ).limit(20))
            for row in rows:
                self.storage.remove_owned(self.storage.operation_path("staging", row.id))
                temporary = self.storage.operation_path("journal", row.id).with_suffix(".json.tmp")
                temporary = self.storage.checked(temporary.relative_to(self.root).as_posix())
                if temporary.is_file():
                    temporary.unlink()
                    sync_directory(temporary.parent)
                row.status, row.updated_at = "expired", utcnow()
                count += 1
        return count

    def maintain(self, *, expire: bool = True, retention_days: int = 30) -> dict:
        """沿用应用维护循环；恢复不受自动回收开关影响，终态操作按期限有界清理。"""
        self.recover()
        if not expire:
            return {"expired": 0, "pruned": 0}
        expired = self.expire_staging()
        with self.sessions.begin() as session:
            lock_configuration(session)
            ids = list(session.scalars(select(ExtensionOperation.id).where(
                ExtensionOperation.kind.in_(["skill_import", "skill_remove"]),
                ExtensionOperation.status.in_(["succeeded", "cancelled", "expired"]),
                ExtensionOperation.updated_at < utcnow() - timedelta(days=retention_days),
            ).limit(20)))
            if ids:
                session.execute(delete(ExtensionOperation).where(ExtensionOperation.id.in_(ids)))
        return {"expired": expired, "pruned": len(ids)}
