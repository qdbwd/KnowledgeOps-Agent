"""独立保留只改变保留意图，不能发布草稿或赋予发布权限。"""

import pytest
from test_cross_run_drafts import drafts
from test_cross_run_drafts import runtime_services as runtime_services

from knowledgeops_agent.persistence import models as db


def test_save_draft_preserves_body_revision_and_publication_boundary(runtime_services):
    _, knowledge, _, _, new, draft = drafts(runtime_services)
    assert draft["retention_intent"] == "temporary"
    revised = knowledge.edit_draft(draft["id"], draft["operations"], expected_revision=1, actor_run_id=new["id"])
    with pytest.raises(ValueError, match="已变化"):
        knowledge.save_draft(draft["id"], expected_revision=1)
    saved = knowledge.save_draft(draft["id"], expected_revision=2)
    assert saved["operations"] == revised["operations"] and saved["revision"] == 2
    assert saved["retention_intent"] == "saved" and saved["status"] == "draft"
    assert knowledge.save_draft(draft["id"], expected_revision=2) == saved
    with pytest.raises(PermissionError, match="publication choice"):
        knowledge.publish(draft["id"], idempotency_key="save-is-not-publish", actor_run_id=new["id"])


def test_late_save_cannot_change_an_accepted_conversation_deletion(runtime_services):
    jobs, knowledge, _, old, _, draft = drafts(runtime_services)
    with jobs.sessions.begin() as session:
        session.get(db.Conversation, old["conversation_id"]).deletion_state = "deleting"
    with pytest.raises(ValueError, match="会话"):
        knowledge.save_draft(draft["id"], expected_revision=1)
    assert knowledge.get_draft(draft["id"])["retention_intent"] == "temporary"


def test_direct_human_draft_save_is_explicit_and_part_of_idempotent_input(runtime_services):
    _, knowledge, _, _, new, _ = drafts(runtime_services)
    operations = [{"action": "create", "title": "人工独立草稿", "body": "用户明确创建"}]
    result = knowledge.create_draft(new["id"], operations, operation_key="manual-asset", independently_saved=True)
    assert result["retention_intent"] == "saved"
    assert knowledge.create_draft(new["id"], operations, operation_key="manual-asset", independently_saved=True)["id"] == result["id"]
    with pytest.raises(ValueError, match="another draft request"):
        knowledge.create_draft(new["id"], operations, operation_key="manual-asset")
