"""运行回执可以过期，会话协议和已经持久消费的子结果不能随之丢失。"""

import json
from dataclasses import replace

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.harness.context.continuation import from_state
from knowledgeops_agent.persistence import models as db

runtime_services = _runtime_services


def test_chat_fallback_retains_original_protocol_without_model_ledger(runtime_services, monkeypatch):
    module, context, first = setup_run(runtime_services)
    state = {"messages": [
        HumanMessage("原问题"),
        AIMessage("已核对的答案", additional_kwargs={"reasoning_content": "private-protocol-value"}),
    ]}
    context.jobs.finish(first["id"], context.worker_id, outcome={"answer": "已核对的答案"},
                        continuation=from_state(context, state))
    visible = context.jobs.messages(first["conversation_id"])
    assert "private-protocol-value" not in json.dumps(visible)
    assert "protocol_payload" not in json.dumps(visible)
    with context.jobs.sessions.begin() as session:
        message = session.scalar(select(db.ChatMessage).where(db.ChatMessage.run_id == first["id"], db.ChatMessage.role == "assistant"))
        assert message.protocol_payload["message"]["data"]["additional_kwargs"]["reasoning_content"] == "private-protocol-value"
        # 模拟不具备有效延续快照的旧聊天入口，仅允许从会话持久层重建。
        session.get(db.Conversation, first["conversation_id"]).latest_snapshot_id = None
    second = context.jobs.enqueue(first["conversation_id"], "继续", request_key="lifecycle-next")
    context.jobs.claim(context.worker_id, run_id=second["id"], generation=1)
    def removed_ledger(**_):
        raise AssertionError("不能依赖已经过期的完整模型回执")
    monkeypatch.setattr(context.gateway.ledger, "records", removed_ledger)
    messages = module.initial_messages(replace(context, run_id=second["id"], root_id=second["id"]))
    answer = next(message for message in messages if isinstance(message, AIMessage))
    assert answer.content == "已核对的答案"
    assert answer.additional_kwargs["reasoning_content"] == "private-protocol-value"


def test_only_matching_final_message_is_saved_as_private_protocol(runtime_services):
    _, context, first = setup_run(runtime_services)
    state = {"messages": [HumanMessage("原问题"), AIMessage("另一个答案")]}
    context.jobs.finish(first["id"], context.worker_id, outcome={"answer": "应用状态回执"},
                        continuation=from_state(context, state))
    with context.jobs.sessions() as session:
        message = session.scalar(select(db.ChatMessage).where(db.ChatMessage.run_id == first["id"], db.ChatMessage.role == "assistant"))
        assert message.protocol_payload is None


def test_final_state_acknowledges_only_seen_notifications_from_this_root(runtime_services):
    _, context, first = setup_run(runtime_services)
    with context.jobs.sessions.begin() as session:
        notices = []
        for event in ("seen", "unseen"):
            row = db.ChildNotification(root_id=first["id"], run_id=first["id"], generation=1,
                                       event_key=event, payload={"status": "completed"})
            session.add(row)
            session.flush()
            notices.append(row.id)
    state = {"messages": [AIMessage("已处理收到的通知")], "child_notifications_seen": [notices[0], "not-present"]}
    # before_model返回的新列表还未成为完成回执，不能提前确认。
    with context.jobs.sessions() as session:
        assert session.get(db.ChildNotification, notices[0]).consumed_at is None
    context.jobs.finish(first["id"], context.worker_id, outcome={"answer": "已处理收到的通知"},
                        continuation=from_state(context, state))
    with context.jobs.sessions() as session:
        assert session.get(db.ChildNotification, notices[0]).consumed_at is not None
        assert session.get(db.ChildNotification, notices[1]).consumed_at is None
