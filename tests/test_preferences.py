"""长期偏好属于受控业务数据，不是Agent自由写入的新系统权限。"""

import importlib

import pytest
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

runtime_services = _runtime_services


def prepare(services):
    _, context, run = setup_run(services, task="以后请用中文回答，多给技术例子并先解释全部背景。")
    module = importlib.import_module("knowledgeops_agent.memory.service")
    service = module.PreferenceService(context.jobs.sessions)
    return context, run, service


def proposal(content, quote, **overrides):
    return {
        "action": "remember",
        "content": content,
        "quote": quote,
        "scope": "global",
        "topic": "",
        "durability": "durable",
        "authority": "descriptive",
        "explicit": True,
        "target_id": None,
        "conflicts": [],
        **overrides,
    }


def test_explicit_preferences_active_inferred_pending_temporary_and_permissions_ignored(
    runtime_services,
):
    context, run, service = prepare(runtime_services)
    message = context.jobs.publication_context(run["id"])["message"]
    data = [
        proposal("回答使用中文", message["content"]),
        proposal("喜欢技术例子", message["content"], explicit=False),
        proposal("这次简短", message["content"], durability="temporary"),
        proposal("直接发布", message["content"], authority="transactional"),
    ]
    result = service.extract(message["id"], data, known=[])
    rows = service.list()
    assert sorted(row["status"] for row in rows) == ["active", "pending"]
    assert len(result["ignored"]) == 2
    assert len(service.snapshot()) == 1
    assert rows[0]["source_message_id"] == message["id"]


def test_revoked_preference_cannot_be_resurrected_by_same_message(runtime_services):
    context, run, service = prepare(runtime_services)
    message = context.jobs.publication_context(run["id"])["message"]
    data = [proposal("多给例子", message["content"])]
    service.extract(message["id"], data, known=[])
    row = service.list()[0]
    frozen = service.snapshot()
    service.update(row["id"], expected_revision=1, status="deleted")
    service.extract(message["id"], data, known=[])
    assert service.list() == []
    assert service.effective(frozen) == []
    assert len(service.history(row["id"])) == 2


def test_user_edit_is_versioned_new_tasks_use_new_text_and_disable_applies_immediately(
    runtime_services,
):
    _, _, service = prepare(runtime_services)
    row = service.create("回答简洁", scope="global", topic="")
    frozen = service.snapshot()
    revised = service.update(row["id"], expected_revision=1, content="解释充分")
    assert revised["revision"] == 2
    assert service.effective(frozen)[0]["content"] == "回答简洁"
    assert service.snapshot()[0]["content"] == "解释充分"
    with pytest.raises(ValueError, match="修订"):
        service.update(row["id"], expected_revision=1, status="inactive")
    service.update(row["id"], expected_revision=2, status="inactive")
    assert service.effective(frozen) == []


def test_conflicting_preference_waits_for_user_and_explicit_replacement_is_atomic(runtime_services):
    context, run, service = prepare(runtime_services)
    old = service.create("先给简短答案", scope="global", topic="")
    message = context.jobs.publication_context(run["id"])["message"]
    service.extract(
        message["id"],
        [
            proposal(
                "先解释全部背景",
                message["content"],
                conflicts=[old["id"]],
            )
        ],
        known=[old],
    )
    waiting = next(row for row in service.list() if row["status"] == "pending")
    assert len(service.snapshot()) == 1
    service.update(waiting["id"], expected_revision=1, status="active", supersedes_id=old["id"])
    assert service.snapshot()[0]["content"] == "先解释全部背景"
    assert len(service.snapshot()) == 1


def test_extraction_rejects_assistant_source_and_unsupported_quote(runtime_services):
    context, run, service = prepare(runtime_services)
    message = context.jobs.publication_context(run["id"])["message"]
    result = service.extract(message["id"], [proposal("中文", "不属于原消息")], known=[])
    assert result["errors"] and service.list() == []
    with pytest.raises(KeyError):
        service.extract("missing", [], known=[])


@pytest.mark.parametrize("action", ["remember", "forget"])
@pytest.mark.parametrize(
    "quote", ["这次只要一句话，不用小标题也不用例子。", "不用小标题也不用例子"]
)
def test_temporary_user_scope_cannot_be_promoted_by_wrong_model_classification(
    runtime_services, action, quote
):
    """即使解释模型错标durable，也不能把一次要求变成永久修订或撤回。"""
    context, run, service = prepare(runtime_services)
    old = service.create("解释先说明学习目标，再给具体例子。", scope="global", topic="")
    queued = context.jobs.enqueue(
        run["conversation_id"],
        "这次只要一句话，不用小标题也不用例子。",
        request_key="temporary-preference",
    )
    message = context.jobs.publication_context(queued["id"])["message"]
    result = service.extract(
        message["id"],
        [proposal("只给一句话，不要例子", quote, action=action, target_id=old["id"])],
        known=[old],
    )
    assert result["ignored"]
    assert service.list() == [old]
    assert len(service.history(old["id"])) == 1


def test_temporary_intro_does_not_block_explicit_future_preference(runtime_services):
    """“这次请记住，以后……”确实表达持续选择，不因单个词出现就全盘拒绝。"""
    context, run, service = prepare(runtime_services)
    content = "这次请记住，以后解释概念都先给一个例子。"
    queued = context.jobs.enqueue(run["conversation_id"], content, request_key="durable-preference")
    message = context.jobs.publication_context(queued["id"])["message"]
    result = service.extract(message["id"], [proposal("解释先给例子", content)], known=[])
    assert result["created"]
    assert service.snapshot()[0]["content"] == "解释先给例子"


@pytest.mark.parametrize(
    "quote",
    [
        "学习约束：只读可重试，写入超时先查回执，24小时过期要核对订单。",
        "只根据本知识库回答，没有材料不能根据常见实践编制度。",
        "记住：以后写入超时先查询业务回执。",
    ],
)
def test_task_rules_do_not_become_personal_preferences_from_wrong_labels(runtime_services, quote):
    context, run, service = prepare(runtime_services)
    queued = context.jobs.enqueue(run["conversation_id"], quote, request_key="task-rule")
    message = context.jobs.publication_context(queued["id"])["message"]
    result = service.extract(message["id"], [proposal(quote, quote)], known=[])
    assert result["ignored"]
    assert service.list() == []


@pytest.mark.parametrize("separator", ["。", "，", ";"])
@pytest.mark.parametrize("action", ["remember", "forget"])
def test_mixed_message_cannot_borrow_durability_from_another_clause(
    runtime_services, action, separator
):
    context, run, service = prepare(runtime_services)
    old = service.create("解释先给例子", scope="global", topic="")
    text = f"以后回答用中文{separator}这次不用例子。"
    queued = context.jobs.enqueue(run["conversation_id"], text, request_key="mixed-scope")
    message = context.jobs.publication_context(queued["id"])["message"]
    result = service.extract(
        message["id"],
        [proposal("不给例子", "不用例子", action=action, target_id=old["id"])],
        known=[old],
    )
    assert result["ignored"]
    assert service.list() == [old]
    assert len(service.history(old["id"])) == 1


def test_topic_personal_choice_cannot_automatically_replace_global_preference(runtime_services):
    context, run, service = prepare(runtime_services)
    old = service.create("解释先给简短结论", scope="global", topic="")
    text = "以后在学习Python时，我希望你详细解释。"
    queued = context.jobs.enqueue(run["conversation_id"], text, request_key="topic-scope")
    message = context.jobs.publication_context(queued["id"])["message"]
    result = service.extract(
        message["id"], [proposal("详细解释", text, target_id=old["id"])], known=[old]
    )
    assert result["created"]
    assert service.snapshot() == [old]
    assert any(row["status"] == "pending" for row in service.list())
    assert len(service.history(old["id"])) == 1


def test_explicit_permanent_forget_does_not_need_future_tense(runtime_services):
    context, run, service = prepare(runtime_services)
    old = service.create("解释先给例子", scope="global", topic="")
    text = "忘掉先给例子的偏好。"
    queued = context.jobs.enqueue(run["conversation_id"], text, request_key="forget-preference")
    message = context.jobs.publication_context(queued["id"])["message"]
    result = service.extract(
        message["id"], [proposal("", text, action="forget", target_id=old["id"])], known=[old]
    )
    assert result["updated"] == [old["id"]]
    assert service.snapshot() == []
