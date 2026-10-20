"""首次部署模板与用户目录分离；初始化决定持久保存，删除后不重新播种。

仅由部署初始化/维护命令调用。文件锁串行化完整初始化，具体包提交仍复用
SkillManagement的配置锁和恢复日志；不执行脚本或安装任何技能依赖。
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from sqlalchemy import select

from knowledgeops_agent.harness.skills.management import SkillManagement
from knowledgeops_agent.harness.skills.storage import SkillStorage
from knowledgeops_agent.harness.tools.configuration import lock_configuration
from knowledgeops_agent.persistence.models import (
    ExtensionOperation,
    Run,
    SkillPackageState,
    new_id,
    utcnow,
)

CONTROL_FILES = {"README.md", "LICENSE-DeerFlow", "availability.json", ".management"}


def initialize_skills(sessions, root: Path, seeds: Path) -> dict:
    manager = SkillManagement(sessions, root)
    with sessions.begin() as session:
        lock_configuration(session)
        manager.storage.initialize()
    path = manager.storage.checked(".management/bootstrap.lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        manager.recover()
        with sessions.begin() as session:
            lock_configuration(session)
            bootstrap = session.scalar(select(ExtensionOperation).where(ExtensionOperation.kind == "skill_bootstrap"))
            if bootstrap is None:
                existing = (
                    any(item.name not in CONTROL_FILES for item in root.iterdir())
                    or session.scalar(select(SkillPackageState.name).limit(1)) is not None
                    or session.scalar(select(Run.id).limit(1)) is not None
                )
                planned = {}
                if not existing:
                    source = SkillStorage(seeds)
                    for directory in sorted(seeds.iterdir()):
                        if directory.name.startswith(".") or not (directory / "SKILL.md").is_file():
                            continue
                        package = source.read(directory)
                        if package.missing or not package.compatible or package.name != directory.name:
                            raise ValueError("初始技能模板无效；初始化未写入用户包")
                        planned[package.name] = package.digest
                bootstrap = ExtensionOperation(
                    id=new_id(), kind="skill_bootstrap", target="skills", status="ready",
                    parameters={"mode": "adopt" if existing else "seed", "seeds": planned}, result={},
                )
                session.add(bootstrap)
                session.flush()
            identity, plan, status = bootstrap.id, dict(bootstrap.parameters), bootstrap.status
        if status == "succeeded":
            return {"mode": plan["mode"], "initialized": True, "changed": []}
        changed = []
        if plan["mode"] == "adopt":
            changed = manager.adopt_existing()
        else:
            source = SkillStorage(seeds)
            for name, digest in plan["seeds"].items():
                with sessions() as session:
                    # 包的任何已登记状态（包括移除）都优先于模板，不能再次初始化。
                    if session.get(SkillPackageState, name) is not None:
                        continue
                package = source.read(source.package(name))
                if package.digest != digest:
                    raise ValueError("未完成的初始化模板已变化，请恢复原模板后重试")
                staged = manager.stage(list(package.files.items()), _bootstrap_id=identity)
                manager.commit(staged["id"])
                changed.append(name)
        with sessions.begin() as session:
            lock_configuration(session)
            bootstrap = session.get(ExtensionOperation, identity)
            bootstrap.status, bootstrap.updated_at = "succeeded", utcnow()
            bootstrap.result = {"mode": plan["mode"]}
        return {"mode": plan["mode"], "initialized": True, "changed": changed}
    finally:
        os.close(descriptor)
