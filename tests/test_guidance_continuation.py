"""引导原话必须进入后续摘要/材料，不能只改变一轮控制字段。"""

from langchain_core.messages import HumanMessage
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

runtime_services = _runtime_services


def test_applied_guidance_is_captured_and_marks_covered_user_sequence(runtime_services):
    from knowledgeops_agent.continuation import from_state
    from knowledgeops_agent.guidance import apply_decision

    _, context, run = setup_run(runtime_services)
    queued = context.jobs.enqueue(
        run["conversation_id"], "补充运行时的具体例子", request_key="guide-capture"
    )
    delivery = context.jobs.guide(queued["id"], target_id=run["id"], target_generation=1)
    apply_decision(context, delivery, {"mode": "preview", "published_ids": []})
    assert any(
        row["content"] == "补充运行时的具体例子" for row in context.jobs.messages_for_run(run["id"])
    )
    data = from_state(
        context, {"messages": [HumanMessage("补充运行时的具体例子")], "guidance_seen_sequence": 2}
    )
    context.jobs.finish(run["id"], "worker-test", continuation=data)
    later = context.jobs.enqueue(run["conversation_id"], "继续", request_key="after-guide")
    context.jobs.claim("w2", run_id=later["id"], generation=1)
    assert context.jobs.continuation(later["id"])["covered_sequence"] == 2
