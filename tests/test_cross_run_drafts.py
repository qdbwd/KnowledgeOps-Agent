"""跨轮维护继承草稿内容，不继承原任务发布授权；修订和工具回执均可恢复。"""

import pytest
from test_runtime import runtime_services as _runtime_services

runtime_services = _runtime_services


def drafts(services):
    jobs, knowledge, sources, _ = services
    conversation = jobs.create_conversation()
    old = jobs.enqueue(conversation["id"], "先整理", request_key="first", publication_mode="direct")
    jobs.claim("worker", run_id=old["id"], generation=1)
    source = sources.import_text("固定来源", "上限1200，不允许自动重试未知结果。")
    evidence = sources.read_segment(source["segments"][0]["id"], root_id=old["id"])
    draft = knowledge.create_draft(
        old["id"], [{"action": "create", "title": "规则", "body": evidence["citation"]}]
    )
    jobs.finish(old["id"], "worker", status="completed", outcome={})
    new = jobs.enqueue(
        conversation["id"], "改一下，先不保存", request_key="next", publication_mode="preview"
    )
    jobs.claim("worker", run_id=new["id"], generation=1)
    return jobs, knowledge, sources, old, new, draft


def test_revise_prior_draft_without_inheriting_old_direct_grant(runtime_services):
    jobs, knowledge, _, old, new, draft = drafts(runtime_services)
    view = knowledge.draft_for_run(draft["id"], new["id"])
    revised = knowledge.edit_draft(
        draft["id"],
        [{**view["operations"][0], "body": "边界：" + view["operations"][0]["body"]}],
        expected_revision=1,
        actor_run_id=new["id"],
        operation_key=f"{new['id']}:revise",
    )
    assert revised["revision"] == 2
    with pytest.raises(PermissionError, match="publication choice"):
        knowledge.publish(draft["id"], idempotency_key="no-old-grant")
    assert knowledge.draft_revision(draft["id"], 1)["operations"] == draft["operations"]
    assert knowledge.draft_revision(draft["id"], 2)["operations"] == revised["operations"]
    knowledge.approve(draft["id"], expected_revision=2)
    published = knowledge.publish(draft["id"], idempotency_key="confirmed-v2")
    assert len(published["version_ids"]) == 1


def test_cross_conversation_draft_access_is_refused(runtime_services):
    jobs, knowledge, _, _, _, draft = drafts(runtime_services)
    other = jobs.create_conversation()
    run = jobs.enqueue(other["id"], "访问不属于本会话的草稿", request_key="other")
    with pytest.raises(PermissionError):
        knowledge.draft_for_run(draft["id"], run["id"])


def test_cross_run_publish_cannot_consume_the_origin_task_grant(runtime_services):
    _, knowledge, _, _, new, draft = drafts(runtime_services)
    with pytest.raises(PermissionError, match="publication choice"):
        knowledge.publish(
            draft["id"], expected_revision=1, idempotency_key="old-direct", actor_run_id=new["id"]
        )


def test_replayed_edit_returns_same_revision_and_changed_arguments_are_rejected(runtime_services):
    _, knowledge, _, _, new, draft = drafts(runtime_services)
    operations = [{**draft["operations"][0], "body": "说明。" + draft["operations"][0]["body"]}]
    args = dict(expected_revision=1, actor_run_id=new["id"], operation_key=f"{new['id']}:edit")
    first = knowledge.edit_draft(draft["id"], operations, **args)
    replay = knowledge.edit_draft(draft["id"], operations, **args)
    assert replay == first
    with pytest.raises(ValueError, match="idempotency"):
        knowledge.edit_draft(draft["id"], [{**operations[0], "title": "不同输入"}], **args)
    assert knowledge.get_draft(draft["id"])["revision"] == 2


def test_revised_cross_run_draft_is_shown_and_next_confirmation_targets_that_revision(
    runtime_services,
):
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    from knowledgeops_agent.worker import _outcome

    jobs, knowledge, _, _, new, draft = drafts(runtime_services)
    knowledge.edit_draft(
        draft["id"], draft["operations"], expected_revision=1, actor_run_id=new["id"]
    )
    context = SimpleNamespace(knowledge=knowledge, jobs=jobs, root_id=new["id"])
    outcome = _outcome(context, {"messages": [AIMessage("已修订，等待你的选择")]})
    assert outcome["draft_ids"] == [draft["id"]]
    finished = jobs.finish(new["id"], "worker", outcome=outcome)
    assert finished["outcome"]["draft_revisions"] == {draft["id"]: 2}
    next_run = jobs.enqueue(
        new["conversation_id"], "保存刚刚这一版", request_key="confirm-revision"
    )
    candidate = jobs.publication_context(next_run["id"])["candidates"]
    assert len(candidate) == 1 and candidate[0]["revision"] == 2
