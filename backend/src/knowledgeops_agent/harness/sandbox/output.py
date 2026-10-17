"""短命令输出的流式归档；ContextVar只桥接同一次SDK execute，不进入AgentState。

复用既有RunArtifact/FileStore和分页补读。临时文件位于应用管理区；正常结束、
超时及控制异常都关闭并登记，整个worker被强杀时仍由原unknown回执阻止重放。
"""

from __future__ import annotations

import codecs
import json
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar

from knowledgeops_agent.persistence.models import CommandExecution
from knowledgeops_agent.persistence.temporary import ManagedTemporary

active_output: ContextVar[CommandOutput | None] = ContextVar("command_output", default=None)


class CommandOutput:
    DEFAULT_LIMIT = 10 * 1024 * 1024
    PREVIEW_BYTES = 4000

    def __init__(self, context, store, limit_bytes: int, ticket: dict | None):
        self.context, self.ticket = context, ticket
        self.limit = limit_bytes
        self.temporary = ManagedTemporary(store.root, kind="command-output", owner_id=context.run_id)
        self.path, self.stream = self.temporary.path, self.temporary.stream
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        secrets = sorted(set(context.gateway.sensitive_values), key=len, reverse=True)
        self.pattern = re.compile("|".join(re.escape(secret) for secret in secrets)) if secrets else None
        self.overlap = max((len(secret) - 1 for secret in secrets), default=0)
        self.pending = ""
        self.received_bytes = self.saved_bytes = 0
        self.storage_truncated = False
        self.preview = bytearray()
        self.metadata: dict = {}

    def append(self, chunk: bytes) -> None:
        self.received_bytes += len(chunk)
        self._write(self.decoder.decode(chunk), final=False)

    def _write(self, text: str, *, final: bool) -> None:
        value = self.pending + text
        cut = len(value) if final else max(0, len(value) - self.overlap)
        if self.pattern:
            # 留住跨块可能组成凭据的后缀；不能先截断再替换而泄露凭据前半截。
            for match in self.pattern.finditer(value):
                if match.start() < cut < match.end():
                    cut = match.start()
                    break
            output = self.pattern.sub("[REDACTED]", value[:cut])
        else:
            output = value[:cut]
        self.pending = value[cut:]
        data = output.encode("utf-8")
        if len(self.preview) < self.PREVIEW_BYTES:
            self.preview.extend(data[:self.PREVIEW_BYTES - len(self.preview)])
        remaining = 0 if self.storage_truncated else max(0, self.limit - self.saved_bytes)
        kept = data[:remaining].decode("utf-8", errors="ignore").encode("utf-8")
        if len(kept) < len(data):
            self.storage_truncated = True
        if kept:
            self.stream.write(kept)
            self.saved_bytes += len(kept)

    @property
    def truncated(self) -> bool:
        return self.storage_truncated or self.saved_bytes > len(self.preview)

    def finish(self, *, exit_code: int | None, stop_reason: str | None = None,
               background_stopped: bool = False) -> None:
        if self.metadata or self.stream.closed:
            return
        self._write(self.decoder.decode(b"", final=True), final=True)
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        artifact_id = self.context.jobs.save_artifact_file(self.context.run_id, "command_output", self.path)
        self.metadata = {
            "artifact_id": artifact_id, "received_bytes": self.received_bytes,
            "saved_bytes": self.saved_bytes, "limit_bytes": self.limit,
            "storage_truncated": self.storage_truncated,
            "preview_truncated": self.truncated,
            "exit_code": exit_code, "stop_reason": stop_reason,
            "background_stopped": background_stopped,
            "truncation_reason": "storage_limit" if self.storage_truncated else None,
        }
        if self.ticket:
            with self.context.jobs.sessions.begin() as session:
                row = session.get(CommandExecution, self.ticket["id"])
                if row is not None and row.attempt == self.ticket["attempt"]:
                    row.result = {**(row.result or {}), "output": self.metadata}
        self.context.jobs.append_event(self.context.run_id, "command_output_saved", self.metadata)

    def read(self) -> str:
        preview = self.preview.decode("utf-8", errors="replace")
        return (preview or "<no output>") + "\n" + json.dumps({
            **self.metadata, "read_tool": "read_tool_result",
            "instruction": "仅以上内容为预览；用artifact_id分页补读已保存输出。storage_truncated=true表示超过保存上限的尾部未保存；状态标记以程序元数据为准。",
        }, ensure_ascii=False)


@contextmanager
def output_scope(context, ticket: dict | None = None, *, limit_bytes: int | None = None):
    with context.jobs.sessions() as session:
        store = session.info.get("file_store")
    if store is None:
        # 历史无工作区的库级调用仍走原SDK合同，正式应用已配置FileStore。
        yield None
        return
    limit = limit_bytes if limit_bytes is not None else (context.resources or {}).get(
        "workspace", {}).get("output_limit_bytes", CommandOutput.DEFAULT_LIMIT)
    output = CommandOutput(context, store, limit, ticket)
    token = active_output.set(output)
    failure = None
    try:
        yield output
    except BaseException as exc:
        failure = type(exc).__name__
        raise
    finally:
        try:
            if not output.metadata:
                output.finish(exit_code=None, stop_reason=failure or "execution_not_started")
        finally:
            if not output.stream.closed:
                output.stream.close()
            output.temporary.close(remove=bool(output.metadata))
            active_output.reset(token)
