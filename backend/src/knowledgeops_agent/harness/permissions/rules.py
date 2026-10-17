"""Shell 的可复用授权匹配；直接使用 tree-sitter-bash，不自行猜 Shell 语法。

借鉴 Codex execpolicy 的 argv 前缀匹配边界，但不移植其 Rust 策略引擎。
解析失败/复合语法只影响规则复用，具体命令仍可由真人批准一次。
"""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

import tree_sitter_bash
from tree_sitter import Language, Parser

_LANGUAGE = Language(tree_sitter_bash.language())
_STATIC_NODES = {
    "program",
    "command",
    "command_name",
    "word",
    "string",
    "string_content",
    "raw_string",
}
_INTERPRETERS = re.compile(
    r"(?:ba|da|z|k|fi|tc|c)?sh|python[\d.]*|pypy[\d.]*|node(?:js)?|ruby|perl|php|lua[\d.]*|pwsh|powershell|cmd(?:\.exe)?|env|xargs|eval|exec|source|\."
)


def command_argv(command: str) -> list[str] | None:
    """只接受一个静态前台命令；不展开变量/glob，不把复合操作折成一个 argv。"""
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        return None
    raw = command.strip().encode("utf-8")
    root = Parser(_LANGUAGE).parse(raw).root_node
    if root.has_error or len(root.children) != 1 or root.children[0].type != "command":
        return None
    # program 的匿名分号、& 等也须拒绝；仅检查 named_children 会漏掉后台执行。
    stack = [root]
    while stack:
        node = stack.pop()
        if node.is_named and node.type not in _STATIC_NODES:
            return None
        if node.type == "word" and any(
            c in raw[node.start_byte : node.end_byte]
            for c in (b"*", b"?", b"[", b"{", b"}", b"~", b"\\")
        ):
            return None
        stack.extend(node.children)
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None
    return argv if argv and argv[0] else None


def reusable_command(command: str) -> list[str] | None:
    """解释器、动态脚本不生成复用候选；单次批准不受此限制。"""
    argv = command_argv(command)
    if not argv:
        return None
    program = PurePosixPath(argv[0])
    if _INTERPRETERS.fullmatch(program.name) or "/" in argv[0]:
        return None
    return argv


def rule_matches(rule: dict, command: str, *, cwd: str) -> bool:
    """cwd 精确绑定；前缀按参数边界匹配，不使用字符串 startswith。"""
    if not rule.get("enabled", True) or rule["cwd"] != cwd:
        return False
    argv = command_argv(command)
    if argv is None:
        return False
    # allow 不能由 API 手工填一条解释器前缀绕过候选规则限制。
    if rule.get("effect", "allow") == "allow" and reusable_command(command) is None:
        return False
    expected = rule["argv"]
    return argv == expected if rule["match"] == "exact" else argv[: len(expected)] == expected


def validate_rule(*, workspace: str, cwd: str, argv: list[str], match: str, effect: str) -> None:
    """配置写入口验证；模型调用不会触发此函数创建权限。"""
    root, directory = PurePosixPath(workspace), PurePosixPath(cwd)
    if (
        not root.is_absolute()
        or not directory.is_absolute()
        or ".." in directory.parts
        or ".." in root.parts
        or not directory.is_relative_to(root)
    ):
        raise ValueError("命令规则的 cwd 必须位于指定工作区")
    if match not in {"exact", "prefix"} or effect not in {"allow", "ask", "deny"}:
        raise ValueError("无效的命令规则")
    if (
        not argv
        or len(argv) > 128
        or any(not isinstance(v, str) or "\x00" in v or len(v) > 8000 for v in argv)
        or not argv[0]
    ):
        raise ValueError("规则需要非空、有限长度的命令参数")
    command = shlex.join(argv)
    if command_argv(command) != argv or (effect == "allow" and reusable_command(command) is None):
        raise ValueError("解释器或复杂脚本不能配置为可复用放行规则，请逐次批准")
