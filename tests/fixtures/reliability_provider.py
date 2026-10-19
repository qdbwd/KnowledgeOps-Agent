"""确定性补问供应商，供真实图、进程与浏览器的离线验收共用。"""

import json

from fixtures.execution_provider import ExecutionProvider
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage


class QuestionsProvider:
    invoke = ExecutionProvider.invoke

    def __init__(self):
        self.requests = []

    def bind_tools(self, tools):
        return self

    def stream(self, messages, **kwargs):
        self.requests.append(messages)
        if any(isinstance(m, ToolMessage) for m in messages):
            yield AIMessageChunk(content="已根据你的回答调整方案。")
            return
        yield AIMessageChunk(content="", tool_call_chunks=[
            {"index": 0, "id": "question-call", "name": "ask_clarification", "args": json.dumps({
                "question": "说明面向初学者还是有经验的读者？", "options": ["初学者", "有经验的读者"],
            })},
            {"index": 1, "id": "premature-write", "name": "write_file", "args": json.dumps({
                "file_path": "/work/premature.txt", "content": "must-not-run",
            })},
        ])


class FileProvider:
    """只提出读/修改操作；文件、故障、恢复与回执由生产实现负责。"""

    def invoke(self, messages, **kwargs):
        payload = json.loads(messages[-1].content)
        if payload.get("policy_version") == "execution-review-v1":
            return AIMessage('{"verdict":"approve","reason":"文件故障恢复验收固定批准"}')
        return ExecutionProvider.invoke(self, messages, **kwargs)

    def __init__(self):
        self.requests = 0

    def bind_tools(self, tools):
        return self

    def stream(self, messages, **kwargs):
        self.requests += 1
        calls = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        if "review-edit" in calls:
            yield AIMessageChunk("已收到文件操作的实际回执，未确认成功的操作没有重试。")
            return
        name, args, call_id = (
            ("edit_file", {"file_path": "/work/review.txt", "old_string": "old", "new_string": "new"}, "review-edit")
            if "review-read" in calls else
            ("read_file", {"file_path": "/work/review.txt"}, "review-read")
        )
        yield AIMessageChunk("", tool_call_chunks=[{"name": name, "args": json.dumps(args), "id": call_id, "index": 0}])


class FileApprovalProvider(FileProvider):
    """浏览器通过真实审批入口决定一次写入；不代替用户填写或批准动作。"""

    def stream(self, messages, **kwargs):
        if any(isinstance(message, ToolMessage) for message in messages):
            yield AIMessageChunk("已收到文件写入回执。")
            return
        yield AIMessageChunk("", tool_call_chunks=[{
            "name": "write_file", "id": "browser-file-approval", "index": 0,
            "args": json.dumps({"file_path": "/work/approval-note.md", "content": "# 文件审批示例\n批准后才写入。"}),
        }])
