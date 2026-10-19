"""以真实 PostgreSQL 验证发布边界，任何测试清理都限定到 knowledgeops_test。"""

from __future__ import annotations

import importlib
import io
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import event, select


@pytest.fixture
def services(tmp_path, business_sessions):
    db = importlib.import_module("knowledgeops_agent.persistence.models")
    sources = importlib.import_module("knowledgeops_agent.sources.service")
    knowledge = importlib.import_module("knowledgeops_agent.knowledge.service")
    sessions = business_sessions
    yield (
        db,
        sources.SourceService(sessions, tmp_path),
        knowledge.KnowledgeService(sessions),
        sessions,
    )


def evidence_for(sources, root="root", body="版本检查防止并发写入覆盖。"):
    source = sources.import_text("并发控制材料", body)
    evidence = sources.read_segment(source["segments"][0]["id"], root_id=root)
    return source, evidence


def draft_article(service, evidence, *, root="root", title="并发更新"):
    return service.create_draft(
        root,
        [
            {
                "action": "create",
                "title": title,
                "body": f"# {title}\n\n版本检查保护并发修改。{evidence['citation']}",
            }
        ],
    )


def publish_article(service, sources, root="root"):
    source, evidence = evidence_for(sources, root)
    service.grant_direct(root)
    draft = draft_article(service, evidence, root=root)
    receipt = service.publish(draft["id"], idempotency_key=f"publish-{root}")
    document = service.get_document(receipt["document_ids"][0])
    return source, evidence, document


def metadata_article(service, sources):
    """建立带真实关系的文章，供正文修改与元数据清空合同验证。"""
    _, _, related = publish_article(service, sources, root="related")
    _, evidence = evidence_for(sources, "metadata")
    service.grant_direct("metadata")
    draft = service.create_draft(
        "metadata",
        [
            {
                "action": "create",
                "title": "事务中的幂等",
                "body": "正文。" + evidence["citation"],
                "summary": "保留摘要",
                "category": "工程/事务",
                "tags": ["幂等", "PostgreSQL"],
                "relations": [{"target_id": related["document_id"], "label": "关联文章"}],
            }
        ],
    )
    receipt = service.publish(draft["id"], idempotency_key="metadata-create")
    return service.get_document(receipt["document_ids"][0])


def test_body_update_inherits_omitted_metadata_and_exposes_it_on_read(services):
    _, sources, knowledge, _ = services
    original = metadata_article(knowledge, sources)
    read = knowledge.read_document(original["document_id"], root_id="metadata")
    for key in ("summary", "category", "tags", "relations", "citation_ids", "archived"):
        assert read[key] == original[key]
    assert read["is_current_version"] is True
    draft = knowledge.create_draft(
        "metadata",
        [
            {
                "action": "update",
                "document_id": original["document_id"],
                "base_version_id": original["version_id"],
                "title": original["title"],
                "body": original["body"] + "\n增加已确认的解释。",
            }
        ],
        require_prior_read=True,
    )
    knowledge.publish(draft["id"], idempotency_key="metadata-update")
    updated = knowledge.get_document(original["document_id"])
    for key in ("summary", "category", "tags", "relations"):
        assert updated[key] == original[key]
        assert draft["after"][original["document_id"]][key] == original[key]
    assert updated["version"] == 2


def test_read_version_exposes_historical_citations_without_reading_or_reauthorizing_them(services):
    """引用旧不等于链接失效；读取文章只允许原基础版本继承，不能变成任意引用授权。"""
    db, sources, knowledge, sessions = services
    _, evidence, original = publish_article(knowledge, sources)
    archived = knowledge.create_draft(
        "root",
        [
            {
                "action": "archive",
                "document_id": original["id"],
                "base_version_id": original["version_id"],
            }
        ],
    )
    knowledge.publish(archived["id"], idempotency_key="archive-citation-fixture")
    historical = knowledge.read_document(
        original["id"], root_id="reader", version_id=original["version_id"]
    )
    current = knowledge.read_document(original["id"], root_id="reader")
    assert historical["is_current_version"] is False
    assert historical["archived"] is False
    assert current["is_current_version"] is True and current["archived"] is True
    assert historical["citation_ids"] == current["citation_ids"] == [evidence["id"]]
    assert sources.get_evidence(evidence["id"])["text"] == evidence["text"]
    # 两次读取只登记两条知识正文证据，不把其下游材料冒充本轮已读。
    with sessions() as session:
        reads = list(session.scalars(select(db.Evidence).where(db.Evidence.root_id == "reader")))
        assert len(reads) == 2
        assert all(row.knowledge_version_id is not None for row in reads)
    knowledge.create_draft(
        "reader",
        [
            {
                "action": "update",
                "document_id": original["id"],
                "base_version_id": current["version_id"],
                "title": current["title"],
                "body": current["text"] + "\n补充说明。",
            }
        ],
        require_prior_read=True,
    )
    with pytest.raises(ValueError, match="evidence must be read"):
        knowledge.create_draft(
            "reader", [{"action": "create", "title": "无关新文章", "body": evidence["citation"]}]
        )


def test_explicit_empty_metadata_clears_fields_and_differs_from_omitted_request(services):
    _, sources, knowledge, _ = services
    original = metadata_article(knowledge, sources)
    operation = {
        "action": "update",
        "document_id": original["document_id"],
        "base_version_id": original["version_id"],
        "title": original["title"],
        "body": original["body"],
    }
    draft = knowledge.create_draft("metadata", [operation], operation_key="same-tool-call")
    explicit = {**operation, "summary": "", "category": "", "tags": [], "relations": []}
    with pytest.raises(ValueError, match="another draft request"):
        knowledge.create_draft("metadata", [explicit], operation_key="same-tool-call")
    revised = knowledge.edit_draft(draft["id"], [explicit], expected_revision=1)
    # 用户编辑使旧任务的自动许可失效，按UI同一流程确认这份修订后发布。
    knowledge.approve(revised["id"], expected_revision=revised["revision"])
    knowledge.publish(revised["id"], idempotency_key="clear-metadata")
    current = knowledge.get_document(original["document_id"])
    assert [current[key] for key in ("summary", "category", "tags", "relations")] == [
        "",
        "",
        [],
        [],
    ]


def test_old_draft_update_requires_review_instead_of_guessing_empty_field_intent(services):
    db, sources, knowledge, sessions = services
    original = metadata_article(knowledge, sources)
    operation = {
        "action": "update",
        "document_id": original["document_id"],
        "base_version_id": original["version_id"],
        "title": original["title"],
        "body": original["body"],
        "summary": "",
        "category": "",
        "tags": [],
        "relations": [],
    }
    draft = knowledge.create_draft("metadata", [operation])
    with sessions.begin() as session:
        row = session.get(db.ChangeSet, draft["id"])
        row.operation_format = 1
    with pytest.raises(ValueError, match="legacy draft"):
        knowledge.publish(draft["id"], idempotency_key="legacy-publish")
    revised = knowledge.edit_draft(draft["id"], [operation], expected_revision=1)
    assert revised["requires_impact_confirmation"]
    with pytest.raises(PermissionError):
        knowledge.publish(draft["id"], idempotency_key="legacy-publish")
    knowledge.approve(draft["id"], expected_revision=2, confirm_impact=True)
    knowledge.publish(draft["id"], idempotency_key="legacy-publish")
    assert knowledge.get_document(original["document_id"])["summary"] == ""


def test_source_import_is_deduplicated_and_evidence_has_exact_location(services):
    db, sources, _, sessions = services
    one, evidence = evidence_for(sources)
    two = sources.import_text("相同材料另一个显示名", "版本检查防止并发写入覆盖。")
    assert one["version_id"] == two["version_id"]
    assert evidence["text"] == "版本检查防止并发写入覆盖。"
    assert evidence["start"] == 0 and evidence["end"] == 13
    with sessions() as session:
        assert len(session.scalars(select(db.SourceVersion)).all()) == 1


def test_preview_cannot_publish_then_approval_atomically_updates_search(services):
    db, sources, knowledge, sessions = services
    _, evidence = evidence_for(sources)
    draft = draft_article(knowledge, evidence)
    with pytest.raises(PermissionError):
        knowledge.publish(draft["id"], idempotency_key="attempt-before-approval")
    assert knowledge.list_documents() == []
    knowledge.approve(draft["id"])
    receipt = knowledge.publish(draft["id"], idempotency_key="approved-operation")
    repeated = knowledge.publish(draft["id"], idempotency_key="approved-operation")
    assert repeated == receipt
    hit = knowledge.search("并发更新")[0]
    assert hit["document_id"] == receipt["document_ids"][0]
    assert hit["version"] == 1
    assert "版本检查" in hit["snippet"]
    with sessions() as session:
        assert len(session.scalars(select(db.KnowledgeVersion)).all()) == 1
        assert len(session.scalars(select(db.PublishReceipt)).all()) == 1


def test_fabricated_and_other_run_evidence_cannot_be_published(services):
    _, sources, knowledge, _ = services
    _, evidence = evidence_for(sources, root="another-run")
    with pytest.raises(ValueError, match="evidence"):
        draft_article(knowledge, evidence)
    with pytest.raises(ValueError, match="evidence"):
        knowledge.create_draft(
            "root",
            [{"action": "create", "title": "伪造", "body": "[来源](/api/evidence/not-real)"}],
        )


def test_evidence_url_delimiters_are_consistent_for_draft_publish_and_evaluation(services):
    """代码展示和中文标点后的正文不能混入引用 ID；各消费端使用同一边界。"""
    from types import SimpleNamespace

    _, sources, knowledge, _ = services
    _, evidence = evidence_for(sources)
    body = f"原文 `/api/evidence/{evidence['id']}`。另见 /api/evidence/{evidence['id']}，可追溯。"
    draft = knowledge.create_draft(
        "root", [{"action": "create", "title": "引用展示", "body": body}]
    )
    knowledge.grant_direct("root")
    receipt = knowledge.publish(draft["id"], idempotency_key="inline-citation")
    assert knowledge.get_document(receipt["document_ids"][0])["citation_ids"] == [evidence["id"]]
    if not (Path(__file__).resolve().parents[1] / "evaluation").is_dir():
        pytest.skip("本地评估目录不随仓库发布，正文发布的引用边界已核对")
    from evaluation.checks import collect_citations

    rows, valid = collect_citations(SimpleNamespace(sources=sources), [body])
    assert valid and [row["id"] for row in rows] == [evidence["id"]]


def test_verified_citation_markers_are_rendered_in_preview_publish_and_export(services):
    """K02 格式中的真实 ID 经归属验证后成为正文链接和持久引用。"""
    _, sources, knowledge, _ = services
    _, evidence = evidence_for(sources)
    body = f"材料结论[citation:{evidence['id']}]。"
    draft = knowledge.create_draft(
        "root", [{"action": "create", "title": "来源可追溯", "body": body}]
    )
    rendered = f"材料结论[来源](/api/evidence/{evidence['id']})。"
    assert draft["operations"][0]["body"] == rendered
    assert next(iter(draft["after"].values()))["body"] == rendered
    knowledge.grant_direct("root")
    receipt = knowledge.publish(draft["id"], idempotency_key="render-verified-marker")
    document = knowledge.get_document(receipt["document_ids"][0])
    assert document["body"] == rendered
    assert document["citation_ids"] == [evidence["id"]]
    with zipfile.ZipFile(io.BytesIO(knowledge.export_markdown())) as archive:
        article = archive.read(f"documents/{document['id']}.md").decode()
        assert "[citation:" not in article
        assert f"../sources/{evidence['id']}.md" in article


@pytest.mark.parametrize("marker", ["[citation]", "[citation:]", "[citation:not-real]"])
def test_unmapped_citation_markers_are_rejected_even_beside_a_valid_link(services, marker):
    """K24 没有 ID 的占位符不能通过另一个真实链接获得默认来源。"""
    _, sources, knowledge, _ = services
    _, evidence = evidence_for(sources)
    with pytest.raises(ValueError, match="citation|evidence"):
        knowledge.create_draft(
            "root",
            [{"action": "create", "title": "未定位", "body": marker + evidence["citation"]}],
        )
    assert knowledge.list_drafts(root_id="root") == []


def test_citation_markers_cannot_bypass_task_provenance(services):
    _, sources, knowledge, _ = services
    _, evidence = evidence_for(sources, root="another-task")
    with pytest.raises(ValueError, match="evidence"):
        knowledge.create_draft(
            "root",
            [{"action": "create", "title": "越权引用", "body": f"[citation:{evidence['id']}]"}],
        )


def test_publication_revalidates_legacy_draft_citation_placeholders(services):
    """已有草稿即使跳过新版准备步骤，也不能把占位正文发布。"""
    db, sources, knowledge, sessions = services
    _, evidence = evidence_for(sources)
    draft = draft_article(knowledge, evidence)
    with sessions.begin() as session:
        saved = session.get(db.ChangeSet, draft["id"])
        saved.operations = [{**saved.operations[0], "body": "结论[citation]"}]
    knowledge.grant_direct("root")
    with pytest.raises(ValueError, match="citation"):
        knowledge.publish(draft["id"], idempotency_key="legacy-placeholder")
    assert knowledge.list_documents() == []


def test_literal_placeholder_examples_are_preserved(services):
    _, sources, knowledge, _ = services
    _, evidence = evidence_for(sources)
    body = "示例：`[citation]`。\n\n```text\n[citation:not-real]\n```\n" + evidence["citation"]
    draft = knowledge.create_draft(
        "root", [{"action": "create", "title": "格式说明", "body": body}]
    )
    assert draft["operations"][0]["body"] == body


def test_version_conflict_preserves_new_content_and_index(services):
    _, sources, knowledge, _ = services
    _, evidence, document = publish_article(knowledge, sources)
    original_version = document["version_id"]
    first = knowledge.create_draft(
        "root",
        [
            {
                "action": "update",
                "document_id": document["id"],
                "base_version_id": original_version,
                "title": "事务隔离",
                "body": f"事务必须整体提交。{evidence['citation']}",
            }
        ],
    )
    stale = knowledge.create_draft(
        "root",
        [
            {
                "action": "update",
                "document_id": document["id"],
                "base_version_id": original_version,
                "title": "过期更新",
                "body": f"旧编辑内容。{evidence['citation']}",
            }
        ],
    )
    knowledge.publish(first["id"], idempotency_key="first-update")
    with pytest.raises(ValueError, match="version conflict"):
        knowledge.publish(stale["id"], idempotency_key="stale-update")
    current = knowledge.get_document(document["id"])
    assert current["title"] == "事务隔离" and current["version"] == 2
    assert knowledge.search("事务隔离")[0]["version_id"] == current["version_id"]
    assert knowledge.search("过期更新") == []


def test_archive_and_restore_create_versions_and_keep_historical_evidence(services):
    _, sources, knowledge, _ = services
    _, evidence, document = publish_article(knowledge, sources)
    old_version = document["version_id"]
    archive = knowledge.create_draft(
        "root",
        [
            {
                "action": "archive",
                "document_id": document["id"],
                "base_version_id": old_version,
            }
        ],
    )
    knowledge.publish(archive["id"], idempotency_key="archive")
    assert knowledge.search("并发更新") == []
    assert sources.get_evidence(evidence["id"])["text"] == "版本检查防止并发写入覆盖。"
    archived = knowledge.get_document(document["id"])
    restore = knowledge.create_draft(
        "root",
        [
            {
                "action": "restore",
                "document_id": document["id"],
                "base_version_id": archived["version_id"],
                "restore_version_id": old_version,
            }
        ],
    )
    knowledge.publish(restore["id"], idempotency_key="restore")
    current = knowledge.get_document(document["id"])
    assert current["version"] == 3 and current["archived"] is False
    assert knowledge.get_document(document["id"], version_id=old_version)["version"] == 1


def test_merge_requires_impact_confirmation_even_with_direct_grant(services):
    _, sources, knowledge, _ = services
    _, evidence, one = publish_article(knowledge, sources)
    two_draft = draft_article(knowledge, evidence, title="第二篇")
    two_receipt = knowledge.publish(two_draft["id"], idempotency_key="second-article")
    two = knowledge.get_document(two_receipt["document_ids"][0])
    operations = [
        {"action": "archive", "document_id": doc["id"], "base_version_id": doc["version_id"]}
        for doc in (one, two)
    ]
    operations.append(
        {"action": "create", "title": "合并文章", "body": f"合并后的知识。{evidence['citation']}"}
    )
    draft = knowledge.create_draft("root", operations)
    assert draft["requires_impact_confirmation"] is True
    with pytest.raises(PermissionError):
        knowledge.publish(draft["id"], idempotency_key="no-impact-confirmation")
    knowledge.approve(draft["id"], confirm_impact=True)
    knowledge.publish(draft["id"], idempotency_key="confirmed-merge")
    assert [doc["title"] for doc in knowledge.list_documents()] == ["合并文章"]


def test_merge_into_existing_document_still_requires_impact_confirmation(services):
    """合并可以复用目标文档 ID；直接保存授权不能代替影响范围确认。"""
    _, sources, knowledge, _ = services
    _, evidence, destination = publish_article(knowledge, sources)
    other = draft_article(knowledge, evidence, title="待合入文章")
    receipt = knowledge.publish(other["id"], idempotency_key="second-article")
    source = knowledge.get_document(receipt["document_ids"][0])
    draft = knowledge.create_draft(
        "root",
        [
            {
                "action": "update",
                "document_id": destination["id"],
                "base_version_id": destination["version_id"],
                "title": "合并后的现有文章",
                "body": f"合并后的知识。{evidence['citation']}",
            },
            {
                "action": "archive",
                "document_id": source["id"],
                "base_version_id": source["version_id"],
            },
        ],
    )
    with pytest.raises(PermissionError):
        knowledge.publish(draft["id"], idempotency_key="before-impact-confirmation")
    assert draft["requires_impact_confirmation"]
    assert knowledge.get_document(destination["id"])["version"] == 1
    assert not knowledge.get_document(source["id"])["archived"]
    knowledge.approve(draft["id"], confirm_impact=True)
    knowledge.publish(draft["id"], idempotency_key="confirmed-existing-merge")
    assert knowledge.get_document(destination["id"])["version"] == 2
    assert knowledge.get_document(source["id"])["archived"]


def test_mid_transaction_failure_rolls_back_all_documents_and_index(services):
    db, sources, knowledge, sessions = services
    _, evidence = evidence_for(sources)
    knowledge.grant_direct("root")
    draft = knowledge.create_draft(
        "root",
        [
            {"action": "create", "title": title, "body": f"测试事务。{evidence['citation']}"}
            for title in ("事务一", "事务二")
        ],
    )
    engine = sessions.kw["bind"]
    seen = 0

    def break_second_version(conn, cursor, statement, parameters, context, executemany):
        nonlocal seen
        if statement.startswith("INSERT INTO knowledge_versions"):
            seen += 1
            if seen == 2:
                raise RuntimeError("injected database interruption")

    event.listen(engine, "before_cursor_execute", break_second_version)
    try:
        with pytest.raises(RuntimeError, match="injected database"):
            knowledge.publish(draft["id"], idempotency_key="atomic-operation")
    finally:
        event.remove(engine, "before_cursor_execute", break_second_version)
    assert seen == 2
    assert knowledge.list_documents() == []
    assert knowledge.search("事务") == []
    with sessions() as session:
        assert list(session.scalars(select(db.PublishReceipt))) == []
    knowledge.publish(draft["id"], idempotency_key="atomic-operation")
    assert len(knowledge.list_documents()) == 2


def test_editing_draft_invalidates_old_approval_and_stale_editor(services):
    _, sources, knowledge, _ = services
    _, evidence = evidence_for(sources)
    draft = draft_article(knowledge, evidence)
    knowledge.approve(draft["id"], expected_revision=1)
    changes = [{**draft["operations"][0], "title": "修订后的标题"}]
    edited = knowledge.edit_draft(draft["id"], changes, expected_revision=1)
    assert edited["revision"] == 2 and edited["status"] == "draft"
    with pytest.raises(PermissionError):
        knowledge.publish(draft["id"], idempotency_key="stale-approval")
    with pytest.raises(ValueError, match="revision"):
        knowledge.approve(draft["id"], expected_revision=1)
    with pytest.raises(ValueError, match="revision"):
        knowledge.edit_draft(draft["id"], changes, expected_revision=1)
    knowledge.approve(draft["id"], expected_revision=2)
    assert (
        knowledge.publish(draft["id"], idempotency_key="current-approval")["outcome"] == "published"
    )


def test_replayed_draft_tool_returns_same_draft_but_rejects_different_payload(services):
    _, sources, knowledge, _ = services
    _, evidence = evidence_for(sources)
    operations = [{"action": "create", "title": "幂等", "body": evidence["citation"]}]
    one = knowledge.create_draft("root", operations, operation_key="run:tool-1")
    again = knowledge.create_draft("root", operations, operation_key="run:tool-1")
    assert one["id"] == again["id"]
    assert len(knowledge.list_drafts()) == 1
    with pytest.raises(ValueError, match="idempotency"):
        knowledge.create_draft("other", operations, operation_key="run:tool-1")


def test_agent_update_requires_reading_entire_base_version(services):
    _, sources, knowledge, _ = services
    _, _, doc = publish_article(knowledge, sources)
    operation = {
        "action": "update",
        "document_id": doc["id"],
        "base_version_id": doc["version_id"],
        "title": "补充后的文章",
        "body": doc["body"],
    }
    with pytest.raises(ValueError, match="read"):
        knowledge.create_draft("new-root", [operation], require_prior_read=True)
    half = max(1, len(doc["body"]) // 2)
    knowledge.read_document(doc["id"], root_id="new-root", start=0, length=half)
    with pytest.raises(ValueError, match="read"):
        knowledge.create_draft("new-root", [operation], require_prior_read=True)
    evidence = knowledge.read_document(doc["id"], root_id="new-root", start=half)
    assert evidence["knowledge_version_id"] == doc["version_id"]
    assert sources.get_evidence(evidence["id"])["text"] == doc["body"][half:]
    assert (
        knowledge.create_draft("new-root", [operation], require_prior_read=True)["status"]
        == "draft"
    )


def test_split_retaining_original_still_requires_impact_confirmation(services):
    _, sources, knowledge, _ = services
    _, evidence, doc = publish_article(knowledge, sources)
    split = knowledge.create_draft(
        "root",
        [
            {
                "action": "update",
                "document_id": doc["id"],
                "base_version_id": doc["version_id"],
                "title": "主题一",
                "body": evidence["citation"],
            },
            {"action": "create", "title": "主题二", "body": evidence["citation"]},
        ],
    )
    assert split["requires_impact_confirmation"] is True
    with pytest.raises(PermissionError):
        knowledge.publish(split["id"], idempotency_key="unconfirmed-split")


def test_markdown_export_contains_resolvable_evidence_and_manifest(services):
    import io
    import json
    import zipfile

    _, sources, knowledge, _ = services
    _, evidence, doc = publish_article(knowledge, sources)
    with zipfile.ZipFile(io.BytesIO(knowledge.export_markdown())) as archive:
        article = archive.read(f"documents/{doc['id']}.md").decode("utf-8")
        assert f"../sources/{evidence['id']}.md" in article
        assert "/api/evidence/" not in article
        source = archive.read(f"sources/{evidence['id']}.md").decode("utf-8")
        assert "版本检查防止并发写入覆盖。" in source
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["documents"][0]["version_id"] == doc["version_id"]


def test_rejected_draft_cannot_publish_even_with_root_direct_grant(services):
    _, sources, knowledge, _ = services
    _, evidence = evidence_for(sources)
    knowledge.grant_direct("root")
    draft = draft_article(knowledge, evidence)
    knowledge.reject(draft["id"], expected_revision=1)
    with pytest.raises(PermissionError):
        knowledge.publish(draft["id"], idempotency_key="rejected-draft")


def test_publish_replay_rejects_key_owned_by_other_completed_changeset(services):
    _, sources, knowledge, _ = services
    _, evidence = evidence_for(sources)
    knowledge.grant_direct("root")
    first = draft_article(knowledge, evidence)
    second = draft_article(knowledge, evidence, title="第二篇")
    knowledge.publish(first["id"], idempotency_key="key-one")
    knowledge.publish(second["id"], idempotency_key="key-two")
    with pytest.raises(ValueError, match="another changeset"):
        knowledge.publish(first["id"], idempotency_key="key-two")


def test_restore_preview_shows_historical_body_not_current_body(services):
    _, sources, knowledge, _ = services
    _, evidence, original = publish_article(knowledge, sources)
    update = knowledge.create_draft(
        "root",
        [
            {
                "action": "update",
                "document_id": original["id"],
                "base_version_id": original["version_id"],
                "title": "新标题",
                "body": "新的正文。" + evidence["citation"],
            }
        ],
    )
    receipt = knowledge.publish(update["id"], idempotency_key="update")
    restore = knowledge.create_draft(
        "root",
        [
            {
                "action": "restore",
                "document_id": original["id"],
                "base_version_id": receipt["version_ids"][0],
                "restore_version_id": original["version_id"],
            }
        ],
    )
    assert restore["before"][original["id"]]["title"] == "新标题"
    assert restore["after"][original["id"]]["body"] == original["body"]
    assert restore["after"][original["id"]]["title"] == original["title"]
    assert restore["after"][original["id"]]["archived"] is False


def test_export_closes_related_archived_documents_and_nested_evidence(services):
    import io
    import json
    import zipfile

    _, sources, knowledge, _ = services
    _, source_evidence, original = publish_article(knowledge, sources)
    knowledge_evidence = knowledge.read_document(original["id"], root_id="root")
    draft = knowledge.create_draft(
        "root",
        [
            {
                "action": "create",
                "title": "引用原文的文章",
                "body": f"参见[原文](/knowledge/{original['id']})。{knowledge_evidence['citation']}",
                "relations": [{"target_id": original["id"], "label": "基础概念"}],
            }
        ],
    )
    related = knowledge.publish(draft["id"], idempotency_key="related")
    archived = knowledge.create_draft(
        "root",
        [
            {
                "action": "archive",
                "document_id": original["id"],
                "base_version_id": original["version_id"],
            }
        ],
    )
    knowledge.publish(archived["id"], idempotency_key="archive")
    with zipfile.ZipFile(io.BytesIO(knowledge.export_markdown())) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert len(manifest["documents"]) == 2
        reference = next(d for d in manifest["documents"] if d["id"] == original["id"])
        assert reference["reference_only"] and reference["archived"]
        body = archive.read(f"documents/{related['document_ids'][0]}.md").decode()
        assert "/knowledge/" not in body and "基础概念" in body
        assert f"../{reference['path']}" in body
        # 文章引用的文章片段还可能引用材料；整个证据链都必须可离线打开。
        assert f"sources/{source_evidence['id']}.md" in archive.namelist()
        snapshot = archive.read(f"sources/{knowledge_evidence['id']}.md").decode()
        assert "/api/evidence/" not in snapshot
