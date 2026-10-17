"""只替代收费模型；工具和续接结果仍由真实图、MCP 服务与 PG 产生。"""

import json

from fixtures.execution_provider import ExecutionProvider
from langchain_core.messages import AIMessageChunk, ToolMessage


class InputProvider:
    invoke = ExecutionProvider.invoke

    def __init__(self, path, *, url_mode=False):
        self.path, self.url_mode = str(path), url_mode
        self.requests = 0
        self.messages = []

    def bind_tools(self, tools):
        self.name = next((tool.name for tool in tools if tool.name.startswith("mcp_")), None)
        return self

    def stream(self, messages, **kwargs):
        self.requests += 1
        self.messages = messages
        if any(isinstance(message, ToolMessage) and message.tool_call_id == "durable-input" for message in messages):
            yield AIMessageChunk("已收到服务回执，请以服务返回的核对结果为准。")
        elif self.name is None:
            yield AIMessageChunk("", tool_call_chunks=[{
                "name": "tool_search", "args": '{"query":"collect"}',
                "id": "discover-mcp-input", "index": 0,
            }])
        else:
            yield AIMessageChunk("", tool_call_chunks=[{
                "name": self.name, "args": json.dumps({"log_path": self.path, "url_mode": self.url_mode}),
                "id": "durable-input", "index": 0,
            }])
