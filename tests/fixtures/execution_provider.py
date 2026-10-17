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
