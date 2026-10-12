# Adapted from bytedance/deer-flow commit f71999d99c15630b673e46236cb65dce5d0ef531.
# MIT license: see LICENSE in this directory. Changes documented in README-sandbox.md.
"""摘取 LocalSandbox 的有界管道捕获：读尽输出，但只保留限定字节。

本项目仅运行 Linux/UTF-8，读取都在同一线程，故省去跨平台解码和线程锁。
bytearray 替代 chunks 列表，避免大量单字节输出产生过多 Python 对象。
"""


class BoundedPipeCapture:
    def __init__(self, limit_bytes: int):
        self._limit_bytes = limit_bytes
        self._data = bytearray()
        self._total_bytes = 0

    def append(self, chunk: bytes) -> None:
        self._total_bytes += len(chunk)
        remaining = self._limit_bytes - len(self._data)
        if remaining > 0:
            self._data.extend(chunk[:remaining])

    @property
    def truncated(self) -> bool:
        return self._total_bytes > len(self._data)

    def read(self) -> str:
        # 截断可能切在中文字符中间，保留可显示的其余文本，不令工具解码崩溃。
        output = self._data.decode("utf-8", errors="replace")
        if self.truncated:
            output += (
                f"\n[output truncated after {len(self._data)} of {self._total_bytes} bytes; "
                "remaining output discarded. For complete logs, redirect to work/<name>.log "
                "and use read_file to read it in pages.]"
            )
        return output
