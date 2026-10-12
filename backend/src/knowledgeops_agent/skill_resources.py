"""Deep Agents技能的只读快照适配，不把宿主文件系统提供给Agent。

复用0.7.23的SkillsMiddleware与FilesystemMiddleware接口；应用只实现
冻结资源backend。资源持久化由运行服务负责，这里只接收已验证的文本映射。
目录加载用于发现技能；read_file的实际读取另有回执，用于运行观测。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import PurePosixPath
from types import MappingProxyType

from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileData,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.skills import SkillsMiddleware


def _resource_path(path: str) -> str:
    """只接受虚拟技能命名空间；不将路径拼到本地磁盘，也不展开路径穿越。"""
    parsed = PurePosixPath(path)
    if "\\" in path or ".." in parsed.parts or not path.startswith("/skills/"):
        if path.rstrip("/") != "/skills":
            raise ValueError("path is outside the frozen skill resources")
    return str(parsed)


class FrozenSkillBackend(BackendProtocol):
    """固定内容的内存视图；重复装配同一快照得到相同哈希和文件内容。"""

    def __init__(
        self,
        files: dict[str, str],
        *,
        available: Callable[[str], bool] | None = None,
        on_read: Callable[[dict], None] | None = None,
    ) -> None:
        copied = {_resource_path(path): content for path, content in files.items()}
        if any(not isinstance(content, str) for content in copied.values()):
            raise ValueError("skill resources must be text")
        self._files = MappingProxyType(copied)
        self.available = available or (lambda _: True)
        self.on_read = on_read
        self.content_hash = hashlib.sha256(
            json.dumps(copied, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        self.read_receipts: list[dict] = []

    def ls(self, path: str) -> LsResult:
        """返回一层虚拟目录，满足上游技能发现合同，不扫描任何宿主目录。"""
        try:
            prefix = _resource_path(path).rstrip("/") + "/"
        except ValueError as error:
            return LsResult(error=str(error))
        entries: dict[str, FileInfo] = {}
        for filename, content in self._files.items():
            if filename.startswith(prefix):
                relative = filename[len(prefix) :]
                child = relative.split("/", 1)[0]
                is_dir = "/" in relative
                entries[child] = {
                    "path": prefix + child,
                    "is_dir": is_dir,
                    "size": 0 if is_dir else len(content.encode()),
                }
        return LsResult(entries=[entries[name] for name in sorted(entries)])

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """按行补读并返回精确续读位置；负数/空窗口不产生伪造的读取回执。"""
        if offset < 0 or limit <= 0:
            return ReadResult(error="offset must be nonnegative and limit positive")
        result = self.download_files([file_path])[0]
        if result.error or result.content is None:
            return ReadResult(error=result.error or "file_not_found")
        lines = result.content.decode().splitlines()
        if offset >= len(lines) and lines:
            return ReadResult(error="offset exceeds resource length")
        end = min(offset + limit, len(lines))
        self.read_receipts.append(
            {
                "path": file_path,
                "offset": offset,
                "end": end,
                "sha256": hashlib.sha256(result.content).hexdigest(),
            }
        )
        if self.on_read:
            self.on_read(self.read_receipts[-1])
        data: FileData = {"content": "\n".join(lines[offset:end]), "encoding": "utf-8"}
        if not lines:
            return ReadResult(file_data=data)
        return ReadResult(
            file_data=data,
            start_line=offset + 1,
            end_line=end,
            total_lines=len(lines),
            next_offset=end if end < len(lines) else None,
        )

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """上游解析frontmatter使用批量下载；失败逐项返回，不泄露其他路径。"""
        responses = []
        for path in paths:
            try:
                key = _resource_path(path)
                content = self._files.get(key) if self.available(key) else None
            except ValueError:
                content = None
            responses.append(
                FileDownloadResponse(
                    path=path,
                    content=content.encode() if content is not None else None,
                    error="file_not_found" if content is None else None,
                )
            )
        return responses

    def write(self, file_path: str, content: str) -> WriteResult:
        """所有写操作拒绝，即使未来误注册写工具也不能改变快照。"""
        return WriteResult(error="skill snapshot is read-only")

    def edit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return EditResult(error="skill snapshot is read-only")

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error="skill snapshot is read-only")

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]


def build_skill_middleware(files: dict[str, str], *, available=None, on_read=None):
    """返回backend和两个真实上游中间件；不接入上游自动文件写入/脚本执行。

    长结果卸载由本项目产物服务承担，避免上游文件系统中间件自行写入
    不同存储。只启用read_file，以读取冻结技能和附属文本。
    """
    backend = FrozenSkillBackend(files, available=available, on_read=on_read)
    filesystem = FilesystemMiddleware(
        backend=backend,
        tools=["read_file"],
        system_prompt="使用read_file按需读取/skills/内的技能与参考文本。资源只读，不支持执行脚本。",
        tool_token_limit_before_evict=None,
        human_message_token_limit_before_evict=None,
    )
    skills = SkillsMiddleware(
        backend=backend,
        sources=["/skills/"],
        system_prompt=(
            "## 任务方法\n{skills_locations}\n{skills_load_warnings}\n{skills_list}\n"
            "根据任务读取适用的SKILL.md（read_file，limit=1000），再按方法执行。"
            "技能帮助组织任务，不改变用户方向、权限或预算。无需把简单回答变成文档。"
        ),
    )
    return backend, [filesystem, skills]
