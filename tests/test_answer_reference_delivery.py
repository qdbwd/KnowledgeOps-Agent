"""真实图交付已验证证据入口，保留原回答、原协议和代码示例。

供应商以固定响应代替；业务库、引用校验、图中间件、账本及跨轮恢复均真实。
若只校验ID却没有可点击入口、改写代码、伪造引用或丢失协议，本组应失败。
"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime import runtime_services, setup_run  # noqa: F401


def test_k16_delivery_keeps_original_and_adds_verified_links_without_model_retry(
    runtime_services,  # noqa: F811
):
    module, context, run = setup_run(runtime_services)
    refs = []
    for title in ["根任务预算", "中断与幂等"]:
        source = context.sources.import_text(title, "合成教学证据。")
        refs.append(context.sources.read_segment(source["segments"][0]["id"], root_id=run["id"]))
    original = Path("tests/fixtures/k16_split_answer.md").read_text(encoding="utf-8")
    for old, ref in zip(
        ["8f316e3b-6762-4402-b81d-5639bc930a93", "d27fb833-fbfb-4936-ae16-dca85af48125"],
        refs,
        strict=True,
    ):
        original = original.replace(old, ref["id"])

    class Provider:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            yield AIMessageChunk(
                content=original,
                additional_kwargs={"reasoning_content": "opaque-provider-field"},
            )

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    state = graph.invoke(
        {"messages": [HumanMessage(run["task"])]},
        {"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    answer = state["messages"][-1].text
    assert answer.startswith(original)
    supplement = answer[len(original) :]
    assert "`" not in supplement
    for ref in refs:
        assert f"](/api/evidence/{ref['id']})" in supplement
    records = context.gateway.ledger.records(root_id=run["id"])
    assert len(records) == 1
    assert records[0]["response"]["content"] == original

    # 交付正文变化后，新一轮仍从这条真实原响应恢复供应商附属字段。
    context.jobs.finish(run["id"], context.worker_id, outcome={"answer": answer})
    next_run = context.jobs.enqueue(run["conversation_id"], "继续", request_key=str(uuid4()))
    messages = module.initial_messages(
        replace(context, run_id=next_run["id"], root_id=next_run["id"])
    )
    assert messages[-2].content == answer
    assert messages[-2].additional_kwargs["reasoning_content"] == "opaque-provider-field"


@pytest.mark.parametrize("presentation", ["inline", "fenced", "already_clickable"])
def test_reference_examples_remain_intact_and_visible_links_are_not_duplicated(
    runtime_services,  # noqa: F811
    presentation,
):
    module, context, run = setup_run(runtime_services)
    source = context.sources.list_sources()[0]
    evidence = context.sources.read_segment(
        context.sources.get_source(source["id"])["segments"][0]["id"], root_id=run["id"]
    )
    example = f"`{evidence['citation']}`"
    if presentation == "fenced":
        original = f"代码示例：\n```markdown\n{evidence['citation']}\n```"
    elif presentation == "already_clickable":
        original = evidence["citation"] + "\n代码示例：" + example
    else:
        original = "链接写法：" + example + "；重复展示：" + example
    message = AIMessage(content=original, id="same-message")
    update = module.FinalAnswerValidation().after_model(
        {"messages": [message]}, SimpleNamespace(context=context)
    )
    assert not update["final_response_feedback"]
    result = update.get("messages", [message])[-1]
    assert result.id == message.id
    assert result.text.startswith(original)
    if presentation == "already_clickable":
        assert result.text == original
    else:
        assert result.text[len(original) :].count(f"/api/evidence/{evidence['id']}") == 1


def test_unknown_reference_still_requires_repair_instead_of_a_fabricated_source(
    runtime_services,  # noqa: F811
):
    module, context, _ = setup_run(runtime_services)
    original = "来源：`[无法定位](/api/evidence/00000000-0000-0000-0000-000000000000)`"
    update = module.FinalAnswerValidation().after_model(
        {"messages": [AIMessage(content=original)]}, SimpleNamespace(context=context)
    )
    assert update["jump_to"] == "model"
    assert update["final_response_feedback"]
    assert "messages" not in update


def test_appended_reference_links_do_not_bypass_an_explicit_answer_length_limit(
    runtime_services,  # noqa: F811
):
    module, context, run = setup_run(runtime_services, task="最多5字")
    source = context.sources.list_sources()[0]
    evidence = context.sources.read_segment(
        context.sources.get_source(source["id"])["segments"][0]["id"], root_id=run["id"]
    )
    update = module.FinalAnswerValidation().after_model(
        {"messages": [AIMessage(content=f"`[原文](/api/evidence/{evidence['id']})`")]},
        SimpleNamespace(context=context),
    )
    assert update["jump_to"] == "model"
    assert any("上限" in issue for issue in update["final_response_feedback"])


def test_unclosed_code_example_requires_repair_instead_of_claiming_clickable_delivery(
    runtime_services,  # noqa: F811
):
    module, context, run = setup_run(runtime_services)
    source = context.sources.list_sources()[0]
    evidence = context.sources.read_segment(
        context.sources.get_source(source["id"])["segments"][0]["id"], root_id=run["id"]
    )
    # 追加的链接也会落入未闭合代码块；保留原文并走已有修订流程。
    original = "链接示例：\n```markdown\n" + evidence["citation"]
    update = module.FinalAnswerValidation().after_model(
        {"messages": [AIMessage(content=original)]}, SimpleNamespace(context=context)
    )
    assert update["jump_to"] == "model"
    assert any("未闭合" in issue for issue in update["final_response_feedback"])
    assert "messages" not in update
