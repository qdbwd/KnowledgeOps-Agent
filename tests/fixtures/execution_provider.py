"""只供离线审批验收使用的确定性供应商，不属于生产工具或模式。"""

import json

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage


class ExecutionProvider:
    def __init__(self, command="printf 'marker' >> work/harness-marker.txt"):
        self.command = command

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        payload = json.loads(messages[-1].content)
        if payload.get("policy_version") == "execution-review-v1":
            return AIMessage(json.dumps({"verdict": getattr(self, "review_verdict", "approve"),
                                         "reason": "离线执行机制验收的固定判定"}))
        return AIMessage(
            json.dumps(
                {
                    "action": "none",
                    "clear": True,
                    "message_id": payload["message"]["id"],
                    "root_id": payload["root_id"],
                    "quote": "",
                    "changeset_id": None,
                    "expected_revision": None,
                    "confirm_impact": False,
                    "ui_conflict": False,
                    "continue_task": True,
                }
            )
        )

    def stream(self, messages, **kwargs):
        if any(isinstance(message, ToolMessage) for message in messages):
            yield AIMessageChunk("已收到操作回执，请以文件和运行记录为准。")
        else:
            yield AIMessageChunk(
                "",
                tool_call_chunks=[
                    {
                        "name": "execute",
                        "args": json.dumps({"command": self.command}),
                        "id": "harness-command",
                        "index": 0,
                    }
                ],
            )


class MCPReviewProvider(ExecutionProvider):
    """通过真实延迟工具目录和MCP执行，审查响应固定用于人工接续验收。"""

    def __init__(self, verdict="deny"):
        self.review_verdict = verdict
        self.name = None

    def bind_tools(self, tools):
        self.name = next((tool.name for tool in tools if tool.name.startswith("mcp_") and "get_note" in tool.name), None)
        return self

    def stream(self, messages, **kwargs):
        if any(isinstance(message, ToolMessage) and message.tool_call_id == "review-browser-mcp" for message in messages):
            yield AIMessageChunk("已收到MCP操作回执。")
        elif self.name is None:
            yield AIMessageChunk("", tool_call_chunks=[{
                "name": "tool_search", "args": '{"query":"get_note"}', "id": "find-review-mcp", "index": 0,
            }])
        else:
            yield AIMessageChunk("", tool_call_chunks=[{
                "name": self.name, "args": '{"topic":"核对后继续的指定内容"}', "id": "review-browser-mcp", "index": 0,
            }])
