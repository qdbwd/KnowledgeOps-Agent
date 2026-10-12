"""固定查询验证词法索引的元数据、数字/标识符及无关结果抑制。"""

from test_knowledge_service import services as _services

services = _services


def article(knowledge, *, title, body, category="", tags=None, root="topic"):
    knowledge.grant_direct(root)
    draft = knowledge.create_draft(
        root,
        [
            {
                "action": "create",
                "title": title,
                "body": body,
                "category": category,
                "tags": tags or [],
            }
        ],
    )
    receipt = knowledge.publish(draft["id"], idempotency_key="search:" + draft["id"])
    return receipt["document_ids"][0]


def test_category_only_match_and_query_stopwords(services):
    _, _, knowledge, _ = services
    expected = article(knowledge, title="版本事务", body="正文不含分类词。", category="PostgreSQL")
    article(knowledge, title="其他", body="我想知道怎样使用以及这些功能的细节。")
    result = knowledge.search("请问 PostgreSQL")
    assert [item["document_id"] for item in result] == [expected]


def test_multiple_meaningful_terms_numbers_and_code_identifiers_are_preserved(services):
    _, _, knowledge, _ = services
    expected = article(knowledge, title="请求重试", body="max_retries=0，每个项目累计1200次。")
    article(knowledge, title="旧配置", body="max_retries=2，每个项目600次。")
    assert [item["document_id"] for item in knowledge.search("max_retries 1200")] == [expected]


def test_rebuild_derived_index_preserves_content_versions(services):
    from sqlalchemy import delete

    db, _, knowledge, sessions = services
    expected = article(knowledge, title="固定正文", body="原文保持不变", category="PostgreSQL")
    original = knowledge.get_document(expected)
    with sessions.begin() as session:
        session.execute(delete(db.SearchEntry))
    assert knowledge.search("PostgreSQL") == []
    assert knowledge.rebuild_index() == 1
    assert knowledge.get_document(expected) == original
    assert [item["document_id"] for item in knowledge.search("PostgreSQL")] == [expected]
