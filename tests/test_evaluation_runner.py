"""固定评估本身也要能抓住真实错误，不能只有始终为真的占位检查。"""

import importlib
import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from test_api_worker import services  # noqa: F401
from test_runtime import DraftProvider


def test_recovery_evaluation_replays_committed_tool_without_a_second_version(services):  # noqa: F811
    runner = importlib.import_module("evaluation.run")
    source = services.sources.import_text("固定材料", "重复消息需要幂等事务。")
    services.gateway.enabled = True
    services.gateway.client = DraftProvider(source["segments"][0]["id"])
    case = next(
        row
        for row in json.loads(Path("evaluation/cases.json").read_text())["cases"]
        if row["id"] == "K24"
    )
    result = runner.execute_case(
        services,
        case,
        mode="single",
        attempt=1,
        pdf_folder=Path("evaluation/materials"),
        checkpointer=InMemorySaver(),
    )
    assert result["phases"]["interrupted"]["status"] == "failed"
    assert result["phases"]["versions_before_resume"] == result["phases"]["versions_after_resume"]
    assert result["run"]["status"] == "completed"
    assert len(services.knowledge.list_documents()) == 1
    assert result["hard_checks"]["resume_no_duplicate"]
    assert (
        len(
            [
                event
                for event in result["events"]
                if event["kind"] == "tool_started" and event["data"]["name"] == "publish_changes"
            ]
        )
        == 2
    )


def test_invalid_reference_is_a_hard_failure_even_when_expected_words_match(services):  # noqa: F811
    checks = importlib.import_module("evaluation.checks")
    refs, valid = checks.collect_citations(services, ["答案80次 [假的](/api/evidence/not-real)"])
    assert not valid
    assert refs[0]["error"] == "unresolved reference"


@pytest.mark.parametrize("body", ["结论[citation]", "结论[citation:{id}]", "结论没有来源链接"])
def test_article_citation_failure_cannot_be_masked_by_the_final_answer(services, body):  # noqa: F811
    checks = importlib.import_module("evaluation.checks")
    source = services.sources.import_text("固定材料", "幂等避免重复写入。")
    evidence = services.sources.read_segment(source["segments"][0]["id"], root_id="root")
    article = body.replace("{id}", evidence["id"])
    result = {
        "run": {
            "id": "root",
            "status": "completed",
            "outcome": {"answer": evidence["citation"]},
            "budgets": {"model_requests": 10},
        },
        "drafts": [
            {
                "root_id": "root",
                "status": "draft",
                "operations": [{"action": "create", "body": article}],
            }
        ],
        "agent_documents": [],
        "documents": [],
        "fixture": {"before": []},
        "events": [],
        "children": [],
        "models": [],
        "phases": {},
    }
    _, hard, _ = checks.evaluate(services, {"scenario": "direct"}, result)
    assert not hard.get("article_citations_complete", True)


def test_published_body_is_checked_independently_of_a_valid_draft(services):  # noqa: F811
    checks = importlib.import_module("evaluation.checks")
    source = services.sources.import_text("固定材料", "幂等避免重复写入。")
    evidence = services.sources.read_segment(source["segments"][0]["id"], root_id="root")
    result = {
        "run": {
            "id": "root",
            "status": "completed",
            "outcome": {"answer": evidence["citation"]},
            "budgets": {"model_requests": 10},
        },
        "drafts": [
            {
                "root_id": "root",
                "status": "published",
                "operations": [{"action": "create", "body": evidence["citation"]}],
            }
        ],
        "agent_documents": [
            {"version_id": "new-version", "body": evidence["citation"], "archived": False}
        ],
        "documents": [
            {"version_id": "new-version", "archived": False, "body": "已发布结论[citation]"}
        ],
        "fixture": {"before": []},
        "events": [],
        "children": [],
        "models": [],
        "phases": {},
    }
    _, hard, _ = checks.evaluate(services, {"scenario": "direct"}, result)
    assert not hard.get("article_citations_complete", True)


def test_concurrent_user_article_does_not_require_an_agent_source_citation(services):  # noqa: F811
    """K19 用户并发编辑的自写正文不可被误判为 Agent 产物缺少引用。"""
    checks = importlib.import_module("evaluation.checks")
    source = services.sources.import_text("固定材料", "工具上限80次。")
    evidence = services.sources.read_segment(source["segments"][0]["id"], root_id="root")
    result = {
        "run": {
            "id": "root",
            "status": "completed",
            "outcome": {"answer": evidence["citation"]},
            "budgets": {"model_requests": 10},
        },
        "drafts": [
            {
                "root_id": "root",
                "status": "draft",
                "operations": [{"action": "update", "body": evidence["citation"]}],
            }
        ],
        "agent_documents": [
            {"version_id": "old-version", "archived": False, "body": evidence["citation"]}
        ],
        "documents": [
            {
                "version_id": "user-version",
                "archived": False,
                "body": "用户并发编辑：当前工具上限99次，不能被旧草稿覆盖。",
            }
        ],
        "fixture": {"before": [{"version_id": "old-version"}]},
        "events": [],
        "children": [],
        "models": [],
        "phases": {"checks": {"user_edit_not_overwritten": True}},
    }
    _, hard, _ = checks.evaluate(services, {"scenario": "edit_conflict"}, result)
    assert hard["article_citations_complete"]


def test_fixed_catalog_is_complete_and_comparison_is_preselected():
    catalog = json.loads(Path("evaluation/cases.json").read_text())
    assert len(catalog["cases"]) == 24
    assert len({row["id"] for row in catalog["cases"]}) == 24
    assert len(catalog["comparison_cases"]) == 6
    assert sum(row.get("holdout", False) for row in catalog["cases"]) >= 6


@pytest.mark.parametrize(
    ("wrapper", "expected"),
    [("`{citation}`", False), ("```markdown\n{citation}\n```", False), ("{citation}", True)],
)
def test_optimization_requires_clickable_source_instead_of_code_url(services, wrapper, expected):  # noqa: F811
    """重现 A03：同一个真实证据 ID，代码显示不能冒充可点击正文引用。"""
    runner = importlib.import_module("evaluation.optimization")
    source = services.sources.import_text("引用显示", "先读原文，再引用。")
    evidence = services.sources.read_segment(source["segments"][0]["id"], root_id="root")
    turn = _citation_turn(wrapper.format(citation=evidence["citation"]))
    result = runner.result_data(services, "A03", "引用显示", "auto", 1, [turn], {}, [], 0)
    assert result["hard_checks"]["citations_resolve"]
    assert result["checks"]["has_source_citation"] is expected
    assert result["hard_checks"].get("answer_citations_complete", True) is expected


def _citation_turn(answer, *, run_id="root", drafts=None):
    """评估器消费的已完成轨迹；不执行图、不触发供应商请求。"""
    return {
        "run": {
            "id": run_id,
            "status": "completed",
            "outcome": {"answer": answer},
            "budgets": {"model_requests": 10},
            "active_ms": 0,
        },
        "models": [],
        "drafts": drafts or [],
    }


@pytest.mark.parametrize("bad_artifact", ["later_answer", "draft"])
def test_other_artifact_cannot_hide_a_code_only_citation(services, bad_artifact):  # noqa: F811
    runner = importlib.import_module("evaluation.optimization")
    source = services.sources.import_text("引用显示", "读过资料不等于展示了可点击来源。")
    evidence = services.sources.read_segment(source["segments"][0]["id"], root_id="root")
    turns = [_citation_turn(evidence["citation"])]
    code = f"结论 `{evidence['citation']}`"
    if bad_artifact == "later_answer":
        turns.append(_citation_turn(code, run_id="later"))
    else:
        turns[0]["drafts"] = [{"id": "draft", "operations": [{"action": "create", "body": code}]}]
    result = runner.result_data(
        services,
        "I02" if bad_artifact == "later_answer" else "I03",
        "跨轮引用",
        "interactive",
        1,
        turns,
        {},
        [],
        0,
    )
    assert result["checks"]["has_source_citation"]
    assert not result["hard_checks"].get(
        "answer_citations_complete"
        if bad_artifact == "later_answer"
        else "article_citations_complete",
        True,
    )


def test_plain_control_reply_does_not_acquire_a_citation_obligation(services):  # noqa: F811
    """这项格式检查不替代语义审读，也不要求纯确认消息附上无关来源。"""
    runner = importlib.import_module("evaluation.optimization")
    source = services.sources.import_text("引用显示", "发布保存具体修订。")
    evidence = services.sources.read_segment(source["segments"][0]["id"], root_id="root")
    turns = [
        _citation_turn(evidence["citation"]),
        _citation_turn("已保存刚刚确认的修订。", run_id="confirm"),
    ]
    result = runner.result_data(services, "I03", "纯确认", "interactive", 1, turns, {}, [], 0)
    assert result["hard_checks"].get("answer_citations_complete", True)
    assert result["checks"]["has_source_citation"]


def test_business_answer_code_citation_cannot_borrow_valid_draft_link(services):  # noqa: F811
    checks = importlib.import_module("evaluation.checks")
    source = services.sources.import_text("引用显示", "草稿与回复分别验收。")
    evidence = services.sources.read_segment(source["segments"][0]["id"], root_id="root")
    result = {
        **_citation_turn(f"结论 `{evidence['citation']}`"),
        "drafts": [
            {
                "root_id": "root",
                "status": "draft",
                "operations": [{"action": "create", "body": evidence["citation"]}],
            }
        ],
        "agent_documents": [],
        "documents": [],
        "fixture": {"before": []},
        "events": [],
        "children": [],
        "phases": {},
    }
    _, hard, _ = checks.evaluate(services, {"scenario": "direct"}, result)
    assert hard["citations_resolve"]
    assert hard["article_citations_complete"]
    assert not hard.get("answer_citations_complete", True)


def test_first_delivered_draft_revision_is_not_hidden_by_the_corrected_revision(services):  # noqa: F811
    runner = importlib.import_module("evaluation.optimization")
    source = services.sources.import_text("引用显示", "各次已交付草稿须分别审查。")
    evidence = services.sources.read_segment(source["segments"][0]["id"], root_id="root")
    turns = [
        _citation_turn(
            "请看草稿",
            drafts=[
                {
                    "id": "same",
                    "revision": 1,
                    "operations": [{"action": "create", "body": f"`{evidence['citation']}`"}],
                }
            ],
        ),
        _citation_turn(
            "已修订",
            run_id="later",
            drafts=[
                {
                    "id": "same",
                    "revision": 2,
                    "operations": [{"action": "create", "body": evidence["citation"]}],
                }
            ],
        ),
    ]
    result = runner.result_data(services, "I03", "草稿修订", "interactive", 1, turns, {}, [], 0)
    assert not result["hard_checks"].get("article_citations_complete", True)


def test_code_example_next_to_a_real_link_is_not_rewritten_or_rejected(services):  # noqa: F811
    runner = importlib.import_module("evaluation.optimization")
    source = services.sources.import_text("引用显示", "示例保持代码语义，正文单独引用。")
    evidence = services.sources.read_segment(source["segments"][0]["id"], root_id="root")
    content = f"正文{evidence['citation']}\n示例：`{evidence['citation']}`"
    result = runner.result_data(
        services, "A03", "代码示例", "auto", 1, [_citation_turn(content)], {}, [], 0
    )
    assert result["hard_checks"].get("answer_citations_complete", True)
    assert result["run"]["outcome"]["answer"] == content


def test_two_source_coverage_does_not_count_the_code_only_second_link(services):  # noqa: F811
    checks = importlib.import_module("evaluation.checks")
    sources = [
        services.sources.import_text(title, f"{title}的独立固定事实") for title in ["甲", "乙"]
    ]
    assert len({row["version_id"] for row in sources}) == 2
    refs = [
        services.sources.read_segment(row["segments"][0]["id"], root_id="root") for row in sources
    ]
    result = {
        **_citation_turn(f"甲{refs[0]['citation']} 乙`{refs[1]['citation']}`"),
        "agent_documents": [],
        "documents": [],
        "fixture": {
            "before": [],
            "sources": [{"version_id": row["version_id"]} for row in sources],
        },
        "events": [],
        "children": [],
        "phases": {},
    }
    findings, hard, _ = checks.evaluate(services, {"scenario": "conflict"}, result)
    assert hard["citations_resolve"]
    assert findings["has_actual_citation"]
    assert not findings["both_materials_read_and_cited"]


@pytest.mark.parametrize("published_clickable", [True, False])
def test_optimization_checks_agent_fixed_version_not_the_latest_user_version(
    services,  # noqa: F811
    published_clickable,
):
    """后来的用户修改、种子内容不得替代或污染当时的Agent发布正文验收。"""
    from knowledgeops_agent.db import KnowledgeDocument, KnowledgeVersion

    runner = importlib.import_module("evaluation.optimization")
    source = services.sources.import_text("固定历史", "发布产生不可变历史版本。")
    evidence = services.sources.read_segment(source["segments"][0]["id"], root_id="root")
    with services.sessions.begin() as session:
        doc = KnowledgeDocument()
        session.add(doc)
        session.flush()
        versions = [
            KnowledgeVersion(
                document_id=doc.id,
                number=1,
                title="种子",
                body="种子无引用",
                action="create",
                root_id="seed",
            ),
            KnowledgeVersion(
                document_id=doc.id,
                number=2,
                title="Agent发布",
                body=evidence["citation"] if published_clickable else f"`{evidence['citation']}`",
                action="update",
                root_id="root",
            ),
            KnowledgeVersion(
                document_id=doc.id,
                number=3,
                title="用户修改",
                body="用户后来单独写的无引用正文",
                action="update",
                root_id="user-edit",
            ),
        ]
        session.add_all(versions)
        session.flush()
        doc.current_version_id = versions[2].id
        expected_version = versions[1].id
    result = runner.result_data(
        services, "I03", "历史发布", "interactive", 1, [_citation_turn("已发布")], {}, [], 0
    )
    assert [d["version_id"] for d in result["published_documents"]] == [expected_version]
    assert result["hard_checks"]["article_citations_complete"] is published_clickable


def test_delegation_interrupted_empty_reply_does_not_replace_resumed_answer(services):  # noqa: F811
    runner = importlib.import_module("evaluation.optimization")
    source = services.sources.import_text("恢复", "子任务完成后恢复复用已有结果。")
    evidence = services.sources.read_segment(source["segments"][0]["id"], root_id="root")
    partial = _citation_turn("")
    partial["run"]["status"] = "failed"
    partial["run"]["outcome"]["kind"] = "partial"
    completed = _citation_turn(evidence["citation"])
    result = runner.result_data(
        services, "D01", "恢复交付", "multi", 1, [partial, completed], {}, [], 0
    )
    assert result["hard_checks"]["answer_citations_complete"]
    assert result["checks"]["completed"]
