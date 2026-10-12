"""部署资源路径：环境选择位置，正文由版本化文件维护，新运行再冻结。

生产 Compose 或开发 dev.py 已加载 .env。本模块只读取明确命名的路径变量，不加载任意用户目录，
也不把所有结构化配置压成环境变量。显式路径不存在时交由加载器报错。
"""

import os
from pathlib import Path


def source_project_root() -> Path | None:
    """只识别声明的 backend/src 布局；安装包不向上搜索用户目录。"""
    source = Path(__file__).resolve()
    if source.parents[1].name == "src" and source.parents[2].name == "backend":
        return source.parents[3]
    return None


def bundled_resource(relative: str) -> Path:
    """源码部署读根目录；wheel 分发读打包副本，二者来自同一份构建输入。"""
    root = source_project_root()
    project = root / relative if root is not None else None
    return project if project is not None and project.exists() else Path(__file__).parent / relative


def resource_path(variable: str, default: Path) -> Path:
    """返回部署声明的路径；无覆盖时保留包内默认值，方便离线测试和安装分发。"""
    configured = os.environ.get(variable, "").strip()
    return Path(configured).expanduser().resolve() if configured else default
