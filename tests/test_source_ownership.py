"""保存意图是可信入口的事实；相同材料可被多个会话共同使用。"""

import pytest
from sqlalchemy import select

from knowledgeops_agent.app.runs.service import RunService
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.sources.service import SourceService


def test_conversation_material_is_not_library_until_explicitly_saved(business_sessions, tmp_path):
    jobs = RunService(business_sessions)
    conversation = jobs.create_conversation("临时学习")
    sources = SourceService(business_sessions, tmp_path)
    material = sources.import_text("临时资料", "仅用于本次聊天", conversation_id=conversation["id"])
    assert sources.list_sources() == []
    assert [item["id"] for item in sources.list_sources(conversation_id=conversation["id"], include_library=False)] == [material["id"]]
    assert material["independently_saved"] is False
    sources.save_source(material["id"])
    assert sources.list_sources()[0]["independently_saved"] is True
    assert sources.get_version(material["version_id"])["version_id"] == material["version_id"]


def test_dedupe_adds_each_owner_without_promoting_to_library(business_sessions, tmp_path):
    jobs = RunService(business_sessions)
    left, right = jobs.create_conversation("左"), jobs.create_conversation("右")
    sources = SourceService(business_sessions, tmp_path)
    a = sources.import_text("共用", "相同内容", conversation_id=left["id"])
    b = sources.import_text("共用", "相同内容", conversation_id=right["id"])
    assert a["version_id"] == b["version_id"]
    sources.import_text("共用", "相同内容", conversation_id=left["id"])
    with business_sessions() as session:
        uses = list(session.scalars(select(db.SourceUse).where(db.SourceUse.source_id == a["id"])))
        assert {(row.owner_kind, row.owner_id) for row in uses} == {
            ("conversation", left["id"]), ("conversation", right["id"])}
    assert sources.list_sources() == []


def test_sealed_conversation_cannot_receive_material_or_late_reads(business_sessions, tmp_path):
    jobs = RunService(business_sessions)
    conversation = jobs.create_conversation("关闭的聊天")
    run = jobs.enqueue(conversation["id"], "阅读", request_key="source-owner")
    sources = SourceService(business_sessions, tmp_path)
    saved = sources.import_text("库中材料", "继续保留")
    with business_sessions.begin() as session:
        session.get(db.Conversation, conversation["id"]).deletion_state = "deleting"
    with pytest.raises(ValueError, match="会话"):
        sources.import_text("迟到附件", "不可登记", conversation_id=conversation["id"])
    with pytest.raises(ValueError, match="会话"):
        sources.read_segment(saved["segments"][0]["id"], root_id=run["id"])
    with business_sessions() as session:
        assert session.scalar(select(db.Evidence.id)) is None


def test_legacy_ownership_is_not_inferred_from_origin_or_filename(business_sessions, tmp_path):
    sources = SourceService(business_sessions, tmp_path)
    with business_sessions.begin() as session:
        old = db.Source(identity_key="old:conversation:abc", title="临时文件.txt", kind="file",
                        origin="conversation:abc")
        session.add(old)
        session.flush()
        identity = old.id
    result = sources.list_sources()
    assert result[0]["id"] == identity and result[0]["ownership_known"] is False


def test_selected_source_acquires_conversation_use_in_enqueue_transaction(business_sessions, tmp_path):
    jobs = RunService(business_sessions)
    conversation = jobs.create_conversation("选择材料")
    sources = SourceService(business_sessions, tmp_path)
    source = sources.import_text("独立资料", "已有正文")
    jobs.enqueue(conversation["id"], "阅读", request_key="with-source", source_ids=[source["id"]])
    assert sources.list_sources(conversation_id=conversation["id"], include_library=False)[0]["id"] == source["id"]
    with pytest.raises(KeyError):
        jobs.enqueue(conversation["id"], "无效材料", request_key="missing-source", source_ids=["absent"])
    assert len(jobs.list_runs(conversation_id=conversation["id"])) == 1
