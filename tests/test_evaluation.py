"""评估证据与裁决分离：模型高分不能覆盖硬失败，重跑不覆写原始记录。"""

import importlib

import pytest


def test_report_keeps_failed_attempt_and_does_not_accept_a_judge_score(business_sessions):
    module = importlib.import_module("knowledgeops_agent.evaluation")
    service = module.ReportService(business_sessions)
    report = service.create({"kind": "business", "expected_count": 1, "catalog_hash": "fixture"})
    result = {
        "case_id": "K01",
        "mode": "single",
        "attempt": 1,
        "checks": {"completed": True},
        "hard_checks": {"no_unauthorized_write": False},
        "judge": {"score": 100},
        "metrics": {"model_requests": 3},
        "run": {"id": "fixture-run", "outcome": {"answer": "原始错误结果"}},
    }
    service.append(report["id"], "K01-single-1", result)
    with pytest.raises(ValueError, match="exists"):
        service.append(report["id"], "K01-single-1", {**result, "hard_checks": {}})
    service.review(report["id"], "K01-single-1", accepted=True, notes="已读正文")
    view = service.get(report["id"])
    assert view["summary"]["passed"] == 0
    assert view["summary"]["hard_failures"] == 1
    assert (
        service.result(report["id"], "K01-single-1")["run"]["outcome"]["answer"] == "原始错误结果"
    )
    assert "data" not in view["results"][0]


def test_report_completion_requires_expected_results_and_reviews(business_sessions):
    module = importlib.import_module("knowledgeops_agent.evaluation")
    service = module.ReportService(business_sessions)
    report = service.create({"kind": "comparison", "expected_count": 1})
    with pytest.raises(ValueError, match="incomplete"):
        service.finish(report["id"])
    service.append(
        report["id"],
        "K04-single-1",
        {
            "case_id": "K04",
            "mode": "single",
            "attempt": 1,
            "checks": {"fact": True},
            "hard_checks": {"citation": True},
            "metrics": {"model_requests": 2},
        },
    )
    assert service.get(report["id"])["summary"]["pending_review"] == 1
    with pytest.raises(ValueError, match="incomplete"):
        service.finish(report["id"])
    service.review(report["id"], "K04-single-1", accepted=True, notes="逐项核对事实与引用")
    assert service.finish(report["id"])["status"] == "completed"
    assert service.get(report["id"])["summary"]["passed"] == 1
    assert service.export(report["id"])["results"][0]["review"]["notes"] == "逐项核对事实与引用"


@pytest.mark.parametrize("kind", ["interactive", "auxiliary", "delegation"])
def test_optimization_reports_keep_distinct_modes(business_sessions, kind):
    service = importlib.import_module("knowledgeops_agent.evaluation").ReportService(
        business_sessions
    )
    report = service.create({"kind": kind, "expected_count": 1})
    service.append(
        report["id"],
        "fixed-once-1",
        {
            "case_id": "A01",
            "mode": "fixed_once",
            "attempt": 1,
            "checks": {"count": True},
            "hard_checks": {"quota": True},
            "metrics": {"model_requests": 4},
        },
    )
    assert service.get(report["id"])["summary"]["by_mode"]["fixed_once"]["mean_model_requests"] == 4


def test_semantic_review_can_explain_synonyms_but_never_override_business_assertions(
    business_sessions,
):
    service = importlib.import_module("knowledgeops_agent.evaluation").ReportService(
        business_sessions
    )
    report = service.create({"kind": "business", "expected_count": 1})
    data = {
        "case_id": "K10",
        "mode": "multi",
        "attempt": 1,
        "checks": {"completed": True, "expected_terms": False},
        "hard_checks": {"citations_resolve": True},
    }
    service.append(report["id"], "K10-multi-1", data)
    service.review(
        report["id"],
        "K10-multi-1",
        accepted=True,
        notes="已比对原文",
        semantic_findings={"expected_terms": "Restart正确译为重启，不必逐字出现恢复。"},
    )
    assert service.get(report["id"])["summary"]["passed"] == 1
    assert service.result(report["id"], "K10-multi-1")["checks"]["expected_terms"] is False
    for field in ("completed", "citations_resolve", "made_up_check"):
        with pytest.raises(ValueError, match="only lexical"):
            service.review(
                report["id"],
                "K10-multi-1",
                accepted=True,
                notes="无效覆盖",
                semantic_findings={field: "不能覆盖"},
            )
