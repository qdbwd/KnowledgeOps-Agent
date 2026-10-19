"""操作者按需阅读部署技能包；复用包边界校验，资源只作为数据返回。"""

import base64
from pathlib import Path, PurePosixPath

import yaml

from knowledgeops_agent.harness.skills.packages import (
    _entry,
    _package_resources,
    disabled_skills,
)


def package_detail(root: Path, name: str, *, resource: str | None = None) -> dict:
    # 路径参数不具有文件访问权限；只能命中一个声明了自身身份的顶层包。
    if name in {".", ".."} or name.startswith(".") or "/" in name or "\\" in name:
        raise KeyError("技能不存在")
    directory = root / name
    if not (directory / "SKILL.md").is_file():
        raise KeyError("技能不存在")
    entry = _entry(root, directory, disabled_skills(root))
    rows, _ = _package_resources(root, entry)
    files = {
        str(PurePosixPath(row["resource"]).relative_to(f"/skills/{name}")): row for row in rows
    }
    if resource is not None:
        row = files.get(resource)
        if row is None:
            raise KeyError("技能资源不存在")
        return {
            "path": resource,
            "content": row["content"] if row["encoding"] == "utf-8" else None,
            "binary": row["encoding"] != "utf-8",
        }
    content = files["SKILL.md"]["content"]
    metadata = yaml.safe_load(content.split("---", 2)[1])
    compatibility = metadata.get("compatibility", "")
    return {
        **entry,
        "content": content,
        "compatibility": compatibility if isinstance(compatibility, str) else "",
        "resources": [
            {
                "path": path,
                "binary": row["encoding"] != "utf-8",
                "size": len(row["content"].encode("utf-8"))
                if row["encoding"] == "utf-8"
                else len(base64.b64decode(row["content"])),
            }
            for path, row in files.items()
        ],
    }
