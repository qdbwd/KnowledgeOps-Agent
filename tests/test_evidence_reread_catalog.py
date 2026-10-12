"""证据目录在历史压缩后仍提供可执行的固定版本重读定位，不增加引用权限。"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from langchain_core.messages import SystemMessage
from sqlalchemy import func, select
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.db import Evidence

runtime_services = _runtime_services


def test_compacted_controls_can_reread_new_material_without_reconstructing_ids(runtime_services):
    module, context, run = setup_run(runtime_services)
    material = context.sources.import_text("本轮讨论快照", "甲" * 2400 + "尾段原文。")
    first = context.sources.read_segment(material["segments"][0]["id"], root_id=run["id"])
    last = context.sources.read_segment(material["segments"][1]["id"], root_id=run["id"])
    foreign = context.sources.read_segment(material["segments"][1]["id"], root_id="other-root")
    sessions = context.sources.sessions
    with sessions() as session:
        before = session.scalar(select(func.count()).select_from(Evidence))

    class Request:
        runtime = SimpleNamespace(context=context)
        state = {
            "messages": [SystemMessage("历史摘要：读过本轮讨论快照，片段 ID 已缩写。")],
            "final_response_feedback": [],
        }
        system_message = None
        messages = state["messages"]
        tools = [module.read_source]

        def override(self, **kwargs):
            return kwargs

    request = module.RuntimeControlMiddleware().wrap_model_call(Request(), lambda value: value)
    controls = json.loads(
        request["system_message"].content.split("程序提供的本轮控制信息：\n", 1)[1]
    )
    assert material["id"] not in controls["source_ids"]
    background = json.loads(request["messages"][0].content.split("\n", 1)[1])
    catalog = {item["id"]: item for item in background["evidence_catalog"]}
    assert set(catalog) == {first["id"], last["id"]}
    assert catalog[last["id"]]["read_tool"] == "read_source"
    assert catalog[last["id"]]["read_args"] == {"segment_id": material["segments"][1]["id"]}
    assert catalog[last["id"]]["observed_range"] == {"start": 2400, "end": 2405}
    assert catalog[first["id"]]["observed_range"] == {"start": 0, "end": 2400}
    assert catalog[last["id"]]["citation"] == last["citation"]
    assert all("text" not in item and "body" not in item for item in catalog.values())
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Evidence)) == before

    reread = module.read_source.func(
        **catalog[last["id"]]["read_args"], runtime=SimpleNamespace(context=context)
    )
    assert reread["text"] == "尾段原文。"
    assert reread["root_id"] == run["id"]
    assert reread["id"] not in {first["id"], last["id"], foreign["id"]}
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Evidence)) == before + 1
    with pytest.raises(ValueError, match="evidence must be read by this task"):
        context.knowledge.create_draft(
            run["id"], [{"action": "create", "title": "越界引用", "body": foreign["citation"]}]
        )
    assert context.gateway.ledger.records(root_id=run["id"]) == []


def test_knowledge_reread_keeps_observed_version_and_nonzero_start(runtime_services):
    module, context, run = setup_run(runtime_services)
    knowledge = context.knowledge
    draft = knowledge.create_draft(
        "fixture",
        [{"action": "create", "title": "历史知识", "body": "甲" * 6000 + "旧版尾段。"}],
    )
    knowledge.approve(draft["id"])
    receipt = knowledge.publish(draft["id"], idempotency_key="fixture-create")
    document = knowledge.get_document(receipt["document_ids"][0])
    evidence = knowledge.read_document(document["document_id"], root_id=run["id"], start=6000)
    updated = knowledge.create_draft(
        "fixture",
        [
            {
                "action": "update",
                "document_id": document["document_id"],
                "base_version_id": document["version_id"],
                "title": "历史知识",
                "body": "乙" * 6000 + "新版尾段。",
            }
        ],
    )
    knowledge.approve(updated["id"])
    knowledge.publish(updated["id"], idempotency_key="fixture-update")
    current = knowledge.get_document(document["document_id"])
    assert current["version_id"] != document["version_id"]

    catalog = context.sources.evidence_catalog(run["id"])
    assert len(catalog) == 1
    entry = catalog[0]
    assert entry["id"] == evidence["id"]
    assert entry["read_tool"] == "read_knowledge"
    assert entry["read_args"] == {
        "document_id": document["document_id"],
        "version_id": document["version_id"],
        "start": 6000,
    }
    assert entry["observed_range"] == {"start": 6000, "end": 6005}
    assert "text" not in entry and "body" not in entry
    reread = module.read_knowledge.func(
        **entry["read_args"], runtime=SimpleNamespace(context=context)
    )
    assert reread["text"] == "旧版尾段。"
    assert reread["version_id"] == document["version_id"]
    assert reread["start"] == 6000 and reread["end"] == 6005
    assert reread["evidence_id"] != evidence["evidence_id"]
    assert context.sources.get_evidence(reread["evidence_id"])["root_id"] == run["id"]
    assert context.gateway.ledger.records(root_id=run["id"]) == []


def test_reread_catalog_stays_root_scoped_bounded_and_body_free(runtime_services):
    _, context, run = setup_run(runtime_services)
    material = context.sources.import_text("批量只读定位", "原文不进入目录。")
    segment_id = material["segments"][0]["id"]
    ids = []
    with context.sources.sessions.begin() as session:
        for index in range(202):
            evidence = Evidence(
                root_id=run["id"] if index < 201 else "other-root",
                source_segment_id=segment_id,
                start_offset=0,
                end_offset=8,
                quote="原文不进入目录。",
                created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index),
            )
            session.add(evidence)
            session.flush()
            ids.append(evidence.id)
    catalog = context.sources.evidence_catalog(run["id"])
    assert [entry["id"] for entry in catalog] == list(reversed(ids[1:201]))
    for entry in catalog:
        assert set(entry) == {"id", "title", "citation", "read_tool", "read_args", "observed_range"}
        assert entry["read_tool"] == "read_source"
        assert entry["read_args"] == {"segment_id": segment_id}
        assert entry["observed_range"] == {"start": 0, "end": 8}
    with context.sources.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Evidence)) == 202
