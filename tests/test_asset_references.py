"""存活根沿真实正文引用保留资产；Evidence、裸ID和无根循环不能无限保留。"""

from sqlalchemy import select

from knowledgeops_agent.app.lifecycle.asset_references import (
    reachable_assets,
    replace_asset_references,
)
from knowledgeops_agent.app.lifecycle.catalog import install_reference_tracking
from knowledgeops_agent.app.runs.service import RunService
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.sources.service import SourceService


def reachable(sessions, **kwargs):
    with sessions() as session:
        graph = reachable_assets(session, **kwargs)
        return set(session.scalars(select(graph.c.node)))


def fixture(sessions, path):
    install_reference_tracking(sessions)
    jobs = RunService(sessions)
    chat = jobs.create_conversation("待删除会话")
    run = jobs.enqueue(chat["id"], "读取资料", request_key="asset-ref-run")
    sources = SourceService(sessions, path)
    source = sources.import_text("临时原文", "# 只有本聊天使用", conversation_id=chat["id"])
    evidence = sources.read_segment(source["segments"][0]["id"], root_id=run["id"])
    return jobs, chat, run, sources, source, evidence


def test_evidence_existence_and_bare_ids_are_not_roots(business_sessions, tmp_path):
    jobs, chat, run, _, source, evidence = fixture(business_sessions, tmp_path)
    other = jobs.create_conversation("保留会话")
    with business_sessions.begin() as session:
        session.add(db.ChatMessage(conversation_id=other["id"], run_id=run["id"], role="user",
            content=f"日志ID {source['version_id']} 另有证据ID {evidence['id']}"))
        assert replace_asset_references(session, "run_input", run["id"], {
            "artifact_ids": [source["version_id"]], "evidence_ids": [evidence["id"]],
        }) == set()
    assert "source_version:" + source["version_id"] in reachable(business_sessions)
    assert "source_version:" + source["version_id"] not in reachable(business_sessions, excluding_conversation=chat["id"])


def test_actual_citation_in_other_conversation_keeps_fixed_body(business_sessions, tmp_path):
    jobs, chat, run, _, source, evidence = fixture(business_sessions, tmp_path)
    other = jobs.create_conversation("另一个会话")
    with business_sessions.begin() as session:
        message = db.ChatMessage(conversation_id=other["id"], run_id=run["id"], role="assistant", content=evidence["citation"])
        session.add(message)
        session.flush()
        identity = message.id
    assert "source_version:" + source["version_id"] in reachable(business_sessions, excluding_conversation=chat["id"])
    with business_sessions.begin() as session:
        session.delete(session.get(db.ChatMessage, identity))
    assert "source_version:" + source["version_id"] not in reachable(business_sessions, excluding_conversation=chat["id"])


def test_unversioned_source_link_also_protects_versions_added_later(business_sessions, tmp_path):
    jobs, chat, run, sources, _, _ = fixture(business_sessions, tmp_path)
    first = sources.import_text("持续更新材料", "第一版", origin="fixture:rolling", conversation_id=chat["id"])
    other = jobs.create_conversation("使用来源入口")
    with business_sessions.begin() as session:
        session.add(db.ChatMessage(conversation_id=other["id"], run_id=run["id"], role="assistant",
                                   content=f"[来源](/sources/{first['id']})"))
    second = sources.import_text("持续更新材料", "第二版", origin="fixture:rolling", conversation_id=chat["id"])
    visible = reachable(business_sessions, excluding_conversation=chat["id"])
    assert {"source_version:" + first["version_id"], "source_version:" + second["version_id"]} <= visible


def test_unrooted_source_cycle_does_not_keep_itself_alive(business_sessions, tmp_path):
    _, chat, _, sources, left, _ = fixture(business_sessions, tmp_path)
    right = sources.import_text("另一个来源", "相互关联", conversation_id=chat["id"])
    with business_sessions.begin() as session:
        # 相当于原文中真实的固定版本链接。只建派生边，不篡改已封存的正文。
        for source, target in ((left, right), (right, left)):
            replace_asset_references(session, "asset", "source-segment:" + source["segments"][0]["id"],
                                     f"[来源](/sources/{target['id']}?version={target['version_id']})")
    nodes = {"source_version:" + source["version_id"] for source in (left, right)}
    assert not nodes & reachable(business_sessions, excluding_conversation=chat["id"])
    sources.save_source(left["id"])
    assert nodes <= reachable(business_sessions, excluding_conversation=chat["id"])


def test_source_deletion_preview_does_not_drop_another_conversations_use(business_sessions, tmp_path):
    jobs, chat, _, sources, source, _ = fixture(business_sessions, tmp_path)
    sources.save_source(source["id"])
    second = jobs.create_conversation("共享拥有者")
    sources.retain_source(source["id"], conversation_id=second["id"])
    node = "source_version:" + source["version_id"]
    assert node in reachable(business_sessions, excluding_source=source["id"], excluding_conversation=chat["id"])
    with business_sessions.begin() as session:
        session.get(db.Conversation, second["id"]).deletion_state = "deleting"
    assert node not in reachable(business_sessions, excluding_source=source["id"], excluding_conversation=chat["id"])


def test_fixed_delivery_content_keeps_its_real_source_until_delivery_is_removed(business_sessions, tmp_path):
    from knowledgeops_agent.sources.deliveries import DeliveryService

    _, chat, run, _, source, evidence = fixture(business_sessions, tmp_path / "uploads")
    (tmp_path / "outputs").mkdir()
    path = tmp_path / "outputs/报告.md"
    # 链接跨过流式扫描块边界，仍然必须被完整识别。
    path.write_text("# 报告\n" + " " * (65536 - 27) + evidence["citation"], encoding="utf-8")
    delivery = DeliveryService(business_sessions, tmp_path).present(
        ["outputs/报告.md"], run_id=run["id"], root_id=run["id"], call_id="report",
    )[0]
    assert "source_version:" + source["version_id"] in reachable(business_sessions, excluding_conversation=chat["id"])
    assert "source_version:" + source["version_id"] not in reachable(
        business_sessions, excluding_conversation=chat["id"], excluding_delivery=delivery["id"],
    )


def test_real_workspace_links_are_roots_but_delivery_store_is_not_scanned_twice(business_sessions, tmp_path):
    from knowledgeops_agent.app.lifecycle.workspace import refresh_workspace_references
    from knowledgeops_agent.persistence.files import FileStore

    _, chat, _, _, source, evidence = fixture(business_sessions, tmp_path / "uploads")
    store = FileStore(tmp_path)
    store.replace("work/笔记.md", evidence["citation"].encode())
    store.replace("deliveries/blobs/example", evidence["citation"].encode())
    with business_sessions.begin() as session:
        refresh_workspace_references(session, store)
    assert "source_version:" + source["version_id"] in reachable(business_sessions, excluding_conversation=chat["id"])
    store.path("work/笔记.md").unlink()
    with business_sessions.begin() as session:
        refresh_workspace_references(session, store)
    assert "source_version:" + source["version_id"] not in reachable(business_sessions, excluding_conversation=chat["id"])
