"""本地工作目录：复用 Deep Agents 文件工具及 LocalShellBackend。

执行位置是 worker 容器，不创建 AIO 容器。虚拟根限制文件工具，但不是 shell
的操作系统隔离；这一边界与 DeerFlow LocalSandbox 相同。应用密钥不继承到
子进程环境。正式知识工作树可用通用工具编辑，版本/索引由同步中间件维护。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from deepagents.backends.local_shell import LocalShellBackend
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    WriteResult,
)
from deepagents.middleware.filesystem import (
    EXECUTE_TOOL_DESCRIPTION,
    WRITE_FILE_TOOL_DESCRIPTION,
    FilesystemMiddleware,
    FsToolName,
)

from knowledgeops_agent.local_execution import execute_local_command
from knowledgeops_agent.skill_resources import FrozenSkillBackend

WORKSPACE_TOOLS: tuple[FsToolName, ...] = (
    "ls",
    "read_file",
    "glob",
    "grep",
    "write_file",
    "edit_file",
    "delete",
    "execute",
)
MANAGED_DIRECTORIES = {"uploads", "artifacts"}
# 对照 DeerFlow write_file 的 80 KiB 单次整篇写入上限，避免巨大的工具参数。
# 沿用 SDK 的精确编辑/命令接口处理增量写入，不自建另一套写文件协议。
MAX_WRITE_BYTES = 80 * 1024


class WorkspaceBackend(LocalShellBackend):
    """SDK 负责文件协议与实现；此适配补充 Skills 路由和应用管理区保护。

    root 是容器内实际挂载点；文件工具的 /outputs/a.md 对应 root/outputs/a.md。
    shell 从 root 开始，每次新建进程，不保留上一条命令的 cd/export。执行内核
    在运行中检查取消/租约/活跃时间预算，退出时清理进程组；文件保留在挂载区。
    """

    def __init__(
        self,
        root: Path,
        *,
        skills: FrozenSkillBackend,
        guard: Callable | None = None,
        max_timeout: int = 60,
    ):
        root = root.resolve()
        for name in ("outputs", "work", ".home"):
            (root / name).mkdir(parents=True, exist_ok=True)
        self.skills = skills
        self.guard = guard or (lambda: None)
        self.max_timeout = max_timeout
        self.command_env = {
            "PATH": f"{sys.prefix}/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(root / ".home"),
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
        super().__init__(
            root_dir=root,
            virtual_mode=True,
            timeout=max_timeout + 5,
            max_output_bytes=64000,
            inherit_env=False,
            env=self.command_env,
        )

    def _protected(self, path: str) -> bool:
        """普通写工具不能改动发布版本、原件、运行产物和冻结技能。"""
        parts = PurePosixPath("/" + path.lstrip("/")).parts
        if len(parts) > 1 and parts[1] in {*MANAGED_DIRECTORIES, "skills"}:
            return True
        if (
            len(parts) > 1
            and parts[1] == "knowledge"
            and (len(parts) < 3 or parts[2] != "documents")
        ):
            return True
        # 同根符号链接也不能把 outputs 内的普通编辑引向管理区。
        resolved = self._resolve_path(path)
        relative = resolved.relative_to(self.cwd)
        return bool(
            relative.parts
            and (
                relative.parts[0] in MANAGED_DIRECTORIES
                or (
                    relative.parts[0] == "knowledge"
                    and (len(relative.parts) < 2 or relative.parts[1] != "documents")
                )
            )
        )

    def read(self, file_path: str, offset: int = 0, limit: int = 2000):
        if file_path.startswith("/skills/"):
            return self.skills.read(file_path, offset, limit)
        return super().read(file_path, offset, limit)

    def ls(self, path: str):
        if path.rstrip("/") == "/skills" or path.startswith("/skills/"):
            return self.skills.ls(path)
        return super().ls(path)

    def download_files(self, paths: list[str]):
        """read_file 的多模态探测也必须经过同一个只读技能路由。"""
        result: list[FileDownloadResponse] = []
        for path in paths:
            if path.startswith("/skills/"):
                result.extend(self.skills.download_files([path]))
            else:
                result.extend(super().download_files([path]))
        return result

    def write(self, file_path: str, content: str):
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            return WriteResult(
                error="write_file 单次最多80KiB；请分段生成，使用 edit_file 增量编辑或 execute 脚本追加。"
            )
        if self._protected(file_path):
            return WriteResult(
                error="此管理区只读；知识正文请编辑 /knowledge/documents，普通文件写入 /work 或 /outputs。"
            )
        return super().write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False):
        if self._protected(file_path):
            return EditResult(error="此管理区只读；可编辑的知识正文在 /knowledge/documents。")
        return super().edit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str):
        if self._protected(file_path):
            return DeleteResult(
                error="此管理区只读；删除 /knowledge/documents 下的正文会申请归档。"
            )
        return super().delete(file_path)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """保留 SDK 执行合同，接入有界输出与可取消的本地进程管理。

        时间限制不能由模型无限提高；shell 使用相对路径 work/...、outputs/...
        或真实挂载路径，文件工具的虚拟绝对路径不是 shell 的 chroot。
        """
        seconds = self.max_timeout if timeout is None else timeout
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, int)
            or not 1 <= seconds <= self.max_timeout
        ):
            return ExecuteResponse(
                output=f"timeout 必须在 1..{self.max_timeout} 秒内", exit_code=1, truncated=False
            )
        if not isinstance(command, str) or not command.strip():
            return ExecuteResponse(output="command 必须是非空字符串", exit_code=1, truncated=False)
        return execute_local_command(
            command,
            cwd=self.cwd,
            env=self.command_env,
            timeout=seconds,
            guard=self.guard,
        )


def build_workspace_middleware(backend: WorkspaceBackend, selected: set[str]):
    """只声明策略允许的上游工具，沿用已有 Runtime 的预算、撤回和结果外置。"""
    tools = [name for name in WORKSPACE_TOOLS if name in selected]
    return FilesystemMiddleware(
        backend=backend,
        tools=tools,
        max_execute_timeout=backend.max_timeout,
        grep_max_count=100,
        tool_token_limit_before_evict=None,
        human_message_token_limit_before_evict=None,
        custom_tool_descriptions={
            "execute": EXECUTE_TOOL_DESCRIPTION.replace(
                "in an isolated sandbox",
                "in the local worker container (not an isolated security sandbox)",
            )
            + "\nRun finite foreground commands. Ordinary background children are stopped when this call finishes. "
            "Cancellation is checked during execution. stdout/stderr are combined and retained up to 64,000 bytes; "
            "redirect long logs to work/<name>.log and page them with read_file. "
            "Commands have no interactive stdin and cannot retain cd/export between calls.",
            "write_file": WRITE_FILE_TOOL_DESCRIPTION
            + "\nSingle-call content limit: 80 KiB UTF-8. For longer content, use incremental edit_file operations or execute a script to append chunks.",
        },
        system_prompt=(
            f"本地文件工作区实际挂载在 {backend.cwd}。文件工具以此为虚拟根，/skills/ 为冻结只读技能。"
            "先 ls/glob 定位、read_file 分页阅读，再 edit_file 精确修改；write_file 新建文件。"
            "grep 是文本内容检索，不是知识库 BM25 或历史检索的替代。"
            "普通工作文件放 /work，交付文件放 /outputs。正式知识工作文件在 /knowledge/documents，可用相同的读写工具编辑，支持子目录。"
            "写入、编辑、删除或脚本修改这些 Markdown 后，应用自动登记变更并按用户授权同步版本、引用和索引；以同步回执为准。"
            "删除正式工作文件表示归档；重命名目前按归档旧文档并新建处理，不能假称保留同一文档ID。"
            "knowledge/versions、knowledge/current、uploads、artifacts 是应用管理区，不直接编辑。"
            "目录外手动改动或中断未同步的文件可用 sync_knowledge_files 明确指定同步；预览要求会保留草稿待确认。"
            "execute 在 worker 的 Linux 环境执行，每次新 shell；使用 work/...、outputs/... 等相对路径，"
            f"或上述实际挂载路径，单次至多 {backend.max_timeout} 秒。它不是隔离容器，"
            "取消/失去任务租约/活跃时间预算耗尽时会停止命令；不要用 & 启动需要跨调用存活的后台服务。"
            "输出过长只返回截断内容；需完整日志时显式重定向到 work 下再分页读取。"
            "不得读取应用源码/配置/密钥，不得通过脚本改写管理区或绕过知识发布。"
            "当前模型接入以文本为主；PDF 等材料先 import_file，再用材料读取工具核对来源。"
        ),
    )
