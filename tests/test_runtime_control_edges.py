"""补齐引导、资源撤回及本机HTTP解析的交界条件。"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from test_api_worker import client as _client
from test_api_worker import services as _services
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

client = _client
services = _services
runtime_services = _runtime_services


def test_malformed_origin_is_rejected_without_server_exception(client):
    assert (
        client.post("/api/conversations", json={}, headers={"Origin": "http://["}).status_code
        == 403
    )


def test_a_previously_read_skill_cannot_keep_influencing_model_after_revocation(runtime_services):
    from knowledgeops_agent.db import ResourceRevocation
    from knowledgeops_agent.run_resources import ResourceService

    _, context, _ = setup_run(runtime_services)
    service = ResourceService(context.jobs.sessions)
    manifest = service.ensure(context)
    skill = manifest["skills"][0]
    context.jobs.append_event(
        context.run_id, "skill_read", {"path": skill["resource"], "sha256": skill["sha256"]}
    )
    with context.jobs.sessions.begin() as session:
        session.add(
            ResourceRevocation(
                kind="skill",
                resource=skill["resource"],
                content_hash=skill["sha256"],
                reason="撤回",
            )
        )
    with pytest.raises(RuntimeError, match="技能.*撤回"):
        service.validate(context, manifest)


@pytest.mark.parametrize("guidance_text", ["改为最多100字", "改为100字以内"])
def test_latest_explicit_guidance_length_replaces_original_task_limit(
    runtime_services, guidance_text
):
    from knowledgeops_agent.db import Run
    from knowledgeops_agent.guidance import apply_decision
    from knowledgeops_agent.response_validation import FinalAnswerValidation

    _, context, run = setup_run(runtime_services)
    with context.jobs.sessions.begin() as session:
        session.get(Run, run["id"]).task = "最多10字"
    queued = context.jobs.enqueue(
        run["conversation_id"], guidance_text, request_key="longer-answer"
    )
    delivery = context.jobs.guide(queued["id"], target_id=run["id"], target_generation=1)
    apply_decision(context, delivery, {"mode": "preview"})
    response = FinalAnswerValidation().after_model(
        {
            "messages": [AIMessage("这是一段二十字以上且不超过一百字的说明。")],
            "guidance_seen_sequence": 2,
        },
        SimpleNamespace(context=context),
    )
    assert response["final_response_feedback"] == []
    assert "jump_to" not in response
