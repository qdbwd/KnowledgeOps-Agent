"""固定离线模型，仅供真实图与浏览器工程验证；不访问网络供应商。"""

import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool


def chunk(name, args, identifier):
    return AIMessageChunk(
        content="",
        tool_call_chunks=[
            {
                "name": name,
                "args": json.dumps(args, ensure_ascii=False),
                "id": identifier,
                "index": 0,
            }
        ],
    )


class LearningProvider:
    def __init__(self):
        self.configurations = []
        self.before_main_request = None
        self.intent_calls = 0

    def invoke(self, messages, **kwargs):
        """只解释演示脚本约定的真人句子，不模拟任意自然语言理解成功。"""
        self.intent_calls += 1
        payload = json.loads(messages[-1].text)
        message = payload["message"]
        result = {
            "action": "none",
            "clear": False,
            "message_id": message["id"],
            "root_id": payload["root_id"],
            "quote": "",
            "changeset_id": None,
            "expected_revision": None,
            "confirm_impact": False,
            "ui_conflict": False,
            "continue_task": True,
            "preferences": [],
            "learning": [],
        }
        if "本任务请用SQL例子" in message["content"]:
            catalog = payload["learning_context"]
            task = next(t for t in catalog["tasks"] if t["id"] == catalog["current_task_id"])
            result["learning"] = [
                {
                    "action": "configure",
                    "clear": True,
                    "quote": "本任务请用SQL例子",
                    "target_id": task["id"],
                    "expected_revision": task["revision"],
                    "fields": {"examples": "使用SQL例子"},
                }
            ]
        if "以后解释先给具体例子" in message["content"]:
            result["preferences"] = [
                {
                    "action": "remember",
                    "content": "解释先给具体例子",
                    "quote": "以后解释先给具体例子",
                    "scope": "global",
                    "topic": "",
                    "durability": "durable",
                    "authority": "descriptive",
                    "explicit": True,
                    "target_id": None,
                    "conflicts": [],
                    "category": "preference",
                }
            ]
        return AIMessage(json.dumps(result, ensure_ascii=False))

    def bind_tools(self, tools):
        return SimpleNamespace(
            stream=lambda messages, **kwargs: self.stream(
                messages,
                names={convert_to_openai_tool(t)["function"]["name"] for t in tools},
                **kwargs,
            )
        )

    def stream(self, messages, *, names, **kwargs):
        child = any("你是本次独立" in m.text for m in messages)
        if not child:
            # 首轮真人输入还没有延续快照，消息ID可以由框架分配；
            # 夹具按真实背景的当前任务判断，不能假定每条消息都有前序Run标识。
            background = next(
                m for m in messages if m.additional_kwargs.get("knowledgeops_context_data")
            )
            task_text = json.loads(background.text.split("\n", 1)[1])["current_task"]
            if not task_text.startswith("应用协调器安排的学习单元"):
                yield AIMessageChunk(
                    content="离线示例：事务先组织操作，再一起提交；遇到失败应回滚。真实的任务要求和记忆变更请以应用回执为准。"
                )
                return
        if child:
            pages = [
                json.loads(m.content)
                for m in messages
                if isinstance(m, ToolMessage) and m.name == "read_learning_draft"
            ]
            offset = pages[-1]["next_start"] if pages else 0
            if offset is not None:
                yield chunk("read_learning_draft", {"start": offset}, f"page-{offset}")
                return
            material = json.loads("".join(p["content"] for p in pages))
            reader = any("你是本次独立reader" in m.text for m in messages)
            report = (
                {
                    "sufficient": False,
                    "questions": [
                        {"question": "请说明失败边界", "reason": "只说明成功情况不足以学习"}
                    ],
                    "summary": "补充边界",
                }
                if reader
                else {
                    "verdict": "pass",
                    "findings": [],
                    "summary": "固定离线审读，非真实质量判断",
                    "reader_resolutions": [
                        {
                            "question_id": q["question_id"],
                            "resolved": True,
                            "explanation": "正文已补失败边界",
                        }
                        for q in material["reader_questions"]
                    ],
                }
            )
            yield AIMessageChunk(content=json.dumps(report, ensure_ascii=False))
            return
        start = max(
            i
            for i, m in enumerate(messages)
            if isinstance(m, HumanMessage) and str(m.id).startswith("application-task-")
        )
        observations = [m for m in messages[start + 1 :] if isinstance(m, ToolMessage)]
        task = json.loads(messages[start].text.split("\n", 1)[1])
        background = next(
            m for m in messages if m.additional_kwargs.get("knowledgeops_context_data")
        )
        self.configurations.append(
            json.loads(background.text.split("\n", 1)[1])["learning"]["configuration"]
        )
        if self.before_main_request:
            self.before_main_request(task, observations)
        operation = {
            "action": "create",
            "title": task["title"],
            "body": "这段文本只用于离线演示工程流程。",
        }
        if not observations:
            yield chunk("prepare_changes", {"operations": [operation]}, "draft")
            return
        latest = observations[-1]
        value = json.loads(latest.content)
        if latest.name == "prepare_changes":
            yield chunk(
                "learning_feedback",
                {"role": "reader", "changeset_id": value["id"], "expected_revision": 1},
                "reader",
            )
        elif latest.name == "learning_feedback" and value["role"] == "reader":
            assert value["status"] == "completed", value
            yield chunk("read_changeset", {"changeset_id": value["draft_id"]}, "reread")
        elif latest.name == "read_changeset":
            operations = [
                {
                    **op,
                    "body": op["body"]
                    + "\n\n失败边界：这里的模型响应是固定脚本，不证明真实讲解质量。",
                }
                for op in value["operations"]
            ]
            yield chunk(
                "revise_changeset",
                {
                    "changeset_id": value["id"],
                    "expected_revision": value["revision"],
                    "operations": operations,
                },
                "revise",
            )
        elif latest.name == "revise_changeset":
            yield chunk(
                "learning_feedback",
                {
                    "role": "reviewer",
                    "changeset_id": value["id"],
                    "expected_revision": value["revision"],
                },
                "review",
            )
        elif latest.name == "learning_feedback":
            assert value["status"] == "completed", value
            yield chunk(
                "publish_changes",
                {"changeset_id": value["draft_id"], "expected_revision": value["draft_revision"]},
                "publish",
            )
        else:
            assert latest.name == "publish_changes", latest
            yield AIMessageChunk(content="该单元的草稿已修订、审读并保存。")
