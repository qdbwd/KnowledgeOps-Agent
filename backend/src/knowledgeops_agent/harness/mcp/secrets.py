"""MCP专用环境文件：逐次读取、部署环境优先、原子替换及跨进程互斥。

仅管理显式给定的变量，不把整个应用环境传给服务。数据库/接口保存引用；
此类只向连接构建器提供原值，状态和错误不回显内容。
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import re
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from dotenv.parser import parse_stream

from knowledgeops_agent.core.resources import managed_resource_root, resource_path

VARIABLE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
MAX_ENV_BYTES = 128 * 1024


class MCPSecretStore:
    def __init__(self, root: Path | None = None, *, environment: Mapping[str, str] | None = None):
        self.root = (root or resource_path("MCP_DIRECTORY", managed_resource_root("mcp"))).absolute()
        self.environment = os.environ if environment is None else environment

    def _path(self, name: str) -> Path:
        if any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise ValueError("MCP环境目录不能经过链接")
        path = self.root / name
        if path.is_symlink():
            raise ValueError("MCP环境文件不能是链接")
        return path

    @contextmanager
    def _lock(self, *, write=False):
        self._path(".env")
        if not self.root.exists() and not write:
            yield
            return
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(".env.lock")
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("MCP环境锁不是普通文件")
            fcntl.flock(descriptor, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            yield
        finally:
            os.close(descriptor)

    def _read(self) -> dict[str, str]:
        path = self._path(".env")
        if not path.exists():
            return {}
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("MCP环境配置不是普通文件")
            raw = stream.read(MAX_ENV_BYTES + 1)
        if len(raw) > MAX_ENV_BYTES:
            raise ValueError("MCP环境文件超过容量限制")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("MCP环境文件需要UTF-8编码") from None
        values = {}
        # 使用当前python-dotenv解析器的错误标志，防止容错解析静默丢弃旧密钥。
        # 不使用变量插值，不执行shell，也不记录Binding中的原始行。
        for binding in parse_stream(io.StringIO(text)):
            if binding.error:
                raise ValueError("MCP环境文件格式错误，请先修正；原文件未改变")
            if binding.key is None:
                continue
            if binding.key in values:
                raise ValueError("MCP环境文件有重复变量，请先修正")
            self._validate(binding.key, binding.value)
            if binding.value is None:
                raise ValueError("MCP环境变量缺少值")
            values[binding.key] = binding.value
        return values

    @staticmethod
    def _validate(key, value):
        if not isinstance(key, str) or not VARIABLE.fullmatch(key):
            raise ValueError("MCP变量名需要大写英文、数字或下划线")
        if value is not None and (not isinstance(value, str) or len(value) > 8192 or any(c in value for c in "\r\n\x00")):
            raise ValueError("MCP变量值必须为不超过8192字符的单行文本")

    def update(self, changes: Mapping[str, str | None]) -> None:
        """None表示显式移除；引用是否仍被使用由管理服务在配置锁内判断。"""
        if not isinstance(changes, Mapping) or len(changes) > 128:
            raise ValueError("一次只能配置至多128个MCP变量")
        for key, value in changes.items():
            self._validate(key, value)
        with self._lock(write=True):
            values = self._read()
            for key, value in changes.items():
                if value is None:
                    values.pop(key, None)
                else:
                    values[key] = value
            text = "# KnowledgeOps MCP environment; do not commit or include in normal exports.\n"
            text += "".join(f"{key}={json.dumps(value, ensure_ascii=False)}\n" for key, value in sorted(values.items()))
            data = text.encode("utf-8")
            if len(data) > MAX_ENV_BYTES:
                raise ValueError("MCP环境文件超过容量限制")
            temporary = self._path(f".env.{uuid4().hex}.tmp")
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self._path(".env"))
                descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                if temporary.exists():
                    temporary.unlink()

    def project_values(self) -> dict[str, str]:
        """仅供连接/脱敏服务内部使用，不直接作为HTTP响应。"""
        with self._lock():
            return self._read()

    def resolve(self, references) -> dict[str, str]:
        values = self.project_values()
        result = {}
        for ref in set(references):
            self._validate(ref, None)
            value = self.environment.get(ref, values.get(ref))
            if not value:
                raise ValueError(f"缺少MCP环境变量：{ref}")
            result[ref] = value
        return result

    def status(self, references) -> list[dict]:
        values = self.project_values()
        result = []
        for ref in sorted(set(references)):
            self._validate(ref, None)
            source = "environment" if ref in self.environment else "project" if ref in values else "missing"
            result.append({"reference": ref, "source": source,
                           "configured": bool(self.environment.get(ref, values.get(ref))),
                           "project_value": ref in values})
        return result

    def redact(self, value, references, *, additional=()):
        project = self.project_values() if references else {}
        secrets = sorted({*additional, *(item for ref in references for item in (self.environment.get(ref), project.get(ref)) if item)} - {""},
                         key=len, reverse=True)

        def redact_item(item):
            if isinstance(item, str):
                for secret in secrets:
                    item = item.replace(secret, "[REDACTED]")
                return item
            if isinstance(item, dict):
                return {redact_item(key): redact_item(content) for key, content in item.items()}
            if isinstance(item, list):
                return [redact_item(content) for content in item]
            return item

        return redact_item(value)


def config_references(config: dict) -> set[str]:
    return {*config.get("environment_refs", {}).values(), *config.get("header_refs", {}).values()}


def connection_secrets(config: dict) -> dict[str, str]:
    """只解析这条连接明确引用的变量，其他服务与应用凭据不传入协议层。"""
    references = config_references(config)
    return MCPSecretStore().resolve(references) if references else {}
