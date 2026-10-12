"""连接通用文件工具与应用来源/交付；文件编辑本身不是知识发布。"""

from pathlib import Path
from typing import Any
from urllib.parse import quote

from langchain.tools import ToolRuntime, tool

from knowledgeops_agent.file_store import FileStore


def workspace_file(root: Path, file_path: str, *, delivery: bool = False) -> Path:
    """HTTP 与工具共用路径边界；下载只开放 work/outputs 中的普通文件。"""
    key = file_path.lstrip("/")
    if delivery and key.split("/", 1)[0] not in {"work", "outputs"}:
        raise PermissionError("只允许交付 work/outputs 文件，知识文档使用知识库导出")
    target = FileStore(root).path(key)
    if delivery and target.relative_to(root.resolve()).parts[0] not in {"work", "outputs"}:
        raise PermissionError("下载链接不能指向管理区")
    if not target.is_file():
        raise KeyError("workspace file not found")
    return target


def _root(context) -> Path:
    binding = (context.resources or {}).get("workspace")
    if context.run_id != context.root_id or not binding or context.workspace_root is None:
        raise PermissionError("本运行没有文件工作区")
    context.jobs.guard(context.run_id, worker_id=context.worker_id)
    return context.workspace_root


@tool
def import_file(file_path: str, runtime: ToolRuntime[Any]) -> dict:
    """将工作区已有 Markdown/TXT/PDF 文件登记为可引用材料，不直接发布知识。

    file_path 是虚拟路径，如 /work/资料.pdf。最多 10 MiB；文本须 UTF-8。
    返回来源与片段 ID，随后 inspect_material/read_source 阅读才能得到引用。
    PDF 复用本地逐页提取/OCR，有进度及取消检查，不调用收费模型。
    """
    context = runtime.context
    target = workspace_file(_root(context), file_path)
    if target.suffix.lower() not in {".md", ".txt", ".pdf"}:
        raise ValueError("可导入 Markdown、TXT 或 PDF；其他格式先用脚本转换")
    with target.open("rb") as stream:
        data = stream.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("maximum import size is 10 MiB")
    if target.suffix.lower() == ".pdf":
        from knowledgeops_agent.pdf_sources import parse_pdf

        source = context.sources.register_pdf(target.name, data)
        return parse_pdf(
            context.sources,
            source["version_id"],
            check=lambda: context.jobs.guard(context.run_id, worker_id=context.worker_id),
            progress=lambda info: context.jobs.append_event(context.run_id, "page_parsed", info),
        )
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("text file must use UTF-8 encoding") from None
    return context.sources.import_text(target.name, content, kind="file", raw_bytes=data)


@tool
def present_files(file_paths: list[str], runtime: ToolRuntime[Any]) -> list[dict]:
    """核对已生成的 work/outputs 文件，返回真实下载 URL；回答使用返回链接。

    不创建文件、不发布知识、不把一般产物当证据。至多 20 个虚拟路径；文件
    后续改动会改变下载内容，需固定引用的材料请 import_file 后 read_source。
    """
    if not 1 <= len(file_paths) <= 20:
        raise ValueError("请提供 1 至 20 个文件路径")
    root = _root(runtime.context)
    result = []
    for path in file_paths:
        target = workspace_file(root, path, delivery=True)
        key = target.relative_to(root.resolve()).as_posix()
        result.append(
            {
                "path": "/" + key,
                "name": target.name,
                "bytes": target.stat().st_size,
                "url": "/api/workspace/download?path=" + quote(key, safe=""),
            }
        )
    return result
