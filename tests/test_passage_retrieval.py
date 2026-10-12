"""检索验收只使用隔离PG与固定材料，不调用模型；结果必须能回到原文。"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select
from test_knowledge_service import evidence_for
from test_knowledge_service import services as _services
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

services = _services
runtime_services = _runtime_services


def test_splitter_preserves_content_and_late_chinese_offsets():
    from knowledgeops_agent.retrieval import split_passages

    text = "\n# 第一节\r\n\n" + "重复说明。" * 600 + "\n\n# 第二节\n末尾原话：绝不自动扣款。\n"
    chunks = split_passages(text)
    assert len(chunks) > 2
    covered = set()
    for chunk in chunks:
        assert text[chunk.start : chunk.end] == chunk.text
        assert 0 < len(chunk.text) <= 1200
        covered.update(range(chunk.start, chunk.end))
    # SDK可能略过独立空白段，正文不能被遗漏；原文仍完整保存且每个偏移逐字一致。
    assert all(index in covered for index, char in enumerate(text) if not char.isspace())
    assert any("绝不自动扣款" in chunk.text for chunk in chunks)


@pytest.mark.parametrize("archived", [False, True])
def test_history_late_passage_rank_readback_and_no_duplicate_messages(runtime_services, archived):
    from knowledgeops_agent.task_continuity import history_read, history_search
    from knowledgeops_agent.task_history import capture_history

    _, context, _ = setup_run(runtime_services)
    text = "背景介绍。" * 1600 + "\n\n旧约定：订单超时，先查回执，禁止直接重复扣款。"
    messages = [HumanMessage(text, id="long-original")]
    messages += [AIMessage("订单" * 40, id=f"noise-{i}") for i in range(12)]
    state = {"messages": messages}
    if archived:
        state = {"task_history": capture_history(context, {}, messages), "messages": []}
    runtime = SimpleNamespace(context=context, state=state)
    results = history_search.func("订单 超时 回执 扣款", runtime)["results"]
    best = results[0]
    assert best["message_id"] == "long-original"
    assert best["start"] > 600
    assert "禁止直接重复扣款" in best["excerpt"]
    assert text[best["start"] : best["end"]] == best["excerpt"]
    assert len({row["id"] for row in results}) == len(results)
    page = history_read.func(best["id"], runtime, offset=best["start"])
    assert "禁止直接重复扣款" in page["text"]
    assert page["role"] == "human"


def test_knowledge_searches_unseen_body_chunks_and_replaces_index_atomically(services):
    from knowledgeops_agent.db import KnowledgeSearchChunk

    _, sources, knowledge, sessions = services
    _, evidence = evidence_for(sources)
    knowledge.grant_direct("root")
    body = (
        "# 运维说明\n\n"
        + "界面颜色说明。" * 1000
        + "\n\n## 支付\n订单超时先查询回执，禁止重复扣款。\n"
        + evidence["citation"]
    )
    draft = knowledge.create_draft(
        "root", [{"action": "create", "title": "操作手册", "body": body}]
    )
    receipt = knowledge.publish(draft["id"], idempotency_key="passage-create")
    document = knowledge.get_document(receipt["document_ids"][0])
    # 没有聊天展示或历史归档，也必须能直接搜索知识正文的后半部分。
    hit = knowledge.search("订单 超时 回执 扣款")[0]
    assert hit["start"] > 600
    assert "禁止重复扣款" in hit["snippet"]
    assert body[hit["start"] : hit["end"]] == hit["snippet"]
    read = knowledge.read_document(
        document["id"],
        root_id="root",
        version_id=hit["version_id"],
        start=hit["start"],
        length=hit["end"] - hit["start"],
    )
    assert read["text"] == hit["snippet"]
    with sessions() as session:
        assert len(list(session.scalars(select(KnowledgeSearchChunk)))) > 1
    updated = knowledge.create_draft(
        "root",
        [
            {
                "action": "update",
                "document_id": document["id"],
                "base_version_id": document["version_id"],
                "title": "操作手册",
                "body": "新版本只说明界面主题。" + evidence["citation"],
            }
        ],
    )
    knowledge.publish(updated["id"], idempotency_key="passage-update")
    assert knowledge.search("扣款") == []
    assert knowledge.get_document(document["id"], version_id=document["version_id"])["body"] == body
    with sessions() as session:
        assert not list(
            session.scalars(
                select(KnowledgeSearchChunk).where(
                    KnowledgeSearchChunk.version_id == document["version_id"]
                )
            )
        )


def test_chinese_natural_query_uses_or_recall_without_treating_operators_as_sql(services):
    _, sources, knowledge, _ = services
    _, evidence = evidence_for(sources)
    knowledge.grant_direct("root")
    draft = knowledge.create_draft(
        "root",
        [
            {
                "action": "create",
                "title": "部署",
                "body": "采用蓝绿部署，升级前先准备回滚。" + evidence["citation"],
            }
        ],
    )
    knowledge.publish(draft["id"], idempotency_key="or-query")
    assert knowledge.search("请问蓝绿部署怎么回滚 外部不存在的词")[0]["title"] == "部署"
    assert knowledge.search('" | ! & ()') == []
    assert knowledge.search("zzzxqv_unrelated") == []


def test_current_history_cache_is_idempotent_and_does_not_grant_old_state_access(runtime_services):
    """索引缓存不是权限目录：新消息已进数据库，旧State仍不能搜到/读到。"""
    from sqlalchemy import func

    from knowledgeops_agent.db import TaskHistoryBatch, TaskHistoryChunk
    from knowledgeops_agent.task_history import lookup_history

    _, context, _ = setup_run(runtime_services)
    state = {"messages": [HumanMessage("新安排：明日采用蓝绿部署。", id="new-only")]}
    first = lookup_history(context, state, query="蓝绿")
    assert len(first["results"]) == 1
    assert first["results"][0]["score"] > 0
    assert lookup_history(context, state, query="蓝绿") == first
    assert "task_history" not in state
    assert lookup_history(context, {"messages": []}, query="蓝绿")["results"] == []
    assert lookup_history(context, {}, source_id=first["results"][0]["id"])["results"] == []
    with context.jobs.sessions() as session:
        assert session.scalar(select(func.count()).select_from(TaskHistoryChunk)) == 1
        assert session.scalar(select(func.count()).select_from(TaskHistoryBatch)) == 0


def test_bm25_prefers_concise_passage_and_uncommitted_write_is_not_searchable(services):
    """实际pg_search长度归一化及事务回滚，不以自制词频评分冒充BM25。"""
    from sqlalchemy import text
    from test_lexical_search import article

    _, _, knowledge, sessions = services
    short = article(knowledge, title="简明", body="数据库 事务 隔离")
    article(knowledge, title="冗长", body="数据库 " + "界面 颜色  " * 100)
    hits = knowledge.search("数据库")
    assert hits[0]["document_id"] == short and hits[0]["score"] > hits[1]["score"]
    with sessions() as session:
        assert (
            session.execute(
                text("SELECT extversion FROM pg_extension WHERE extname='pg_search'")
            ).scalar_one()
            == "0.26.1"
        )
        session.execute(
            text(
                "UPDATE knowledge_search_chunks SET body_tokens='nevercommittedtoken' WHERE document_id=:id"
            ),
            {"id": short},
        )
        # context退出回滚，索引必须与正文派生表一起回滚。
    assert knowledge.search("nevercommittedtoken") == []
    assert knowledge.search("数据库")[0]["document_id"] == short
