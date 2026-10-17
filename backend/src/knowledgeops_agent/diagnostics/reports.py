"""固定评估的持久报告服务，不在 HTTP 请求里启动收费评估。

执行器逐案追加真实证据。确定性检查、模型裁判和人工审读分开：高语义分
不能抵消硬性失败；尚未审读只能显示待审。失败重跑使用新键，原记录保留。
"""

from __future__ import annotations

import json
from statistics import mean

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from knowledgeops_agent.persistence.models import EvaluationReport, EvaluationResult, utcnow


def result_summary(row: EvaluationResult) -> dict:
    """仅形成小型索引；完整请求/证据在单例详情或导出时加载。"""
    data, review = row.data, row.review
    hard = data.get("hard_checks", {})
    checks = data.get("checks", {})
    hard_ok = bool(hard) and all(value is True for value in hard.values())
    findings = (review or {}).get("semantic_findings", {})
    checks_ok = bool(checks) and all(
        value is True or key in findings for key, value in checks.items()
    )
    return {
        "key": row.key,
        "case_id": data["case_id"],
        "title": data.get("title", ""),
        "mode": data["mode"],
        "attempt": data["attempt"],
        "passed": hard_ok and checks_ok and bool(review and review["accepted"]),
        "hard_passed": hard_ok,
        "checks_passed": checks_ok,
        "raw_checks_passed": bool(checks) and all(value is True for value in checks.values()),
        "review": review,
        "metrics": data.get("metrics", {}),
        "judge": data.get("judge"),
    }


class ReportService:
    def __init__(self, sessions):
        self.sessions = sessions

    def create(self, configuration: dict) -> dict:
        """固定目录哈希、预算、模式和运行边界后才能开始收费执行。"""
        if (
            configuration.get("kind")
            not in {
                "business",
                "comparison",
                "regression",
                "interactive",
                "auxiliary",
                "delegation",
            }
            or not 1 <= configuration.get("expected_count", 0) <= 1000
        ):
            raise ValueError("valid report kind and expected count are required")
        with self.sessions.begin() as session:
            report = EvaluationReport(configuration=jsonable_encoder(configuration))
            session.add(report)
            session.flush()
            report_id = report.id
        return self.get(report_id)

    def append(self, report_id: str, key: str, data: dict) -> None:
        """追加一次执行，不提供修改原始结果的接口；同键恢复必须先查询。"""
        if not 1 <= len(key) <= 120 or any(
            field not in data for field in ("case_id", "mode", "attempt", "checks", "hard_checks")
        ):
            raise ValueError("invalid evaluation result")
        with self.sessions.begin() as session:
            report = session.scalar(
                select(EvaluationReport).where(EvaluationReport.id == report_id).with_for_update()
            )
            if report is None:
                raise KeyError("evaluation report not found")
            if session.get(EvaluationResult, (report_id, key)):
                raise ValueError("evaluation result already exists")
            if report.status != "running":
                raise ValueError("report is closed; create a new report for new evidence")
            session.add(EvaluationResult(report_id=report_id, key=key, data=jsonable_encoder(data)))

    def review(
        self,
        report_id: str,
        key: str,
        *,
        accepted: bool,
        notes: str,
        semantic_findings: dict[str, str] | None = None,
    ) -> None:
        """供维护者在读完正文/引用后记录判断；更正审读保留先前意见。"""
        if not notes.strip():
            raise ValueError("review notes are required")
        findings = semantic_findings or {}
        if any(
            key not in {"expected_terms", "states_allowed_conclusion"} or not value.strip()
            for key, value in findings.items()
        ):
            raise ValueError("only lexical checks may have documented semantic findings")
        with self.sessions.begin() as session:
            row = session.get(EvaluationResult, (report_id, key), with_for_update=True)
            if row is None:
                raise KeyError("evaluation result not found")
            if any(key not in row.data["checks"] for key in findings):
                raise ValueError("only lexical checks present in the result may be reviewed")
            if row.review and all(
                row.review.get(k, {}) == v
                for k, v in {
                    "accepted": accepted,
                    "notes": notes,
                    "semantic_findings": findings,
                }.items()
            ):
                return
            history = (row.review or {}).get("history", [])
            if row.review:
                history = [*history, {k: v for k, v in row.review.items() if k != "history"}]
            row.review = {
                "accepted": accepted,
                "notes": notes,
                "semantic_findings": findings,
                "reviewed_at": utcnow().isoformat(),
                "history": history,
            }

    def get(self, report_id: str) -> dict:
        with self.sessions() as session:
            report = session.get(EvaluationReport, report_id)
            if report is None:
                raise KeyError("evaluation report not found")
            rows = list(
                session.scalars(
                    select(EvaluationResult)
                    .where(EvaluationResult.report_id == report_id)
                    .order_by(EvaluationResult.key)
                )
            )
            results = [result_summary(row) for row in rows]
            groups = {}
            for mode in sorted({row["mode"] for row in results}):
                group = [row for row in results if row["mode"] == mode]
                if group:
                    groups[mode] = {
                        "count": len(group),
                        "passed": sum(row["passed"] for row in group),
                        **{
                            f"mean_{name}": mean(row["metrics"].get(name, 0) for row in group)
                            for name in ("model_requests", "total_tokens", "active_ms")
                        },
                    }
            return {
                "id": report.id,
                "configuration": report.configuration,
                "status": report.status,
                "created_at": report.created_at.isoformat(),
                "results": results,
                "summary": {
                    "count": len(results),
                    "passed": sum(row["passed"] for row in results),
                    "hard_failures": sum(not row["hard_passed"] for row in results),
                    "pending_review": sum(row["review"] is None for row in results),
                    "model_requests": sum(
                        row["metrics"].get("model_requests", 0) for row in results
                    ),
                    "by_mode": groups,
                },
            }

    def list(self) -> list[dict]:
        with self.sessions() as session:
            ids = list(
                session.scalars(
                    select(EvaluationReport.id)
                    .order_by(EvaluationReport.created_at.desc())
                    .limit(100)
                )
            )
        return [
            {k: v for k, v in self.get(report_id).items() if k != "results"} for report_id in ids
        ]

    def result(self, report_id: str, key: str) -> dict:
        with self.sessions() as session:
            row = session.get(EvaluationResult, (report_id, key))
            if row is None:
                raise KeyError("evaluation result not found")
            return {**row.data, "key": key, "review": row.review}

    def export(self, report_id: str) -> dict:
        report = self.get(report_id)
        report["results"] = [self.result(report_id, row["key"]) for row in report["results"]]
        return report

    def finish(self, report_id: str) -> dict:
        report = self.get(report_id)
        if (
            report["summary"]["count"] != report["configuration"]["expected_count"]
            or report["summary"]["pending_review"]
        ):
            raise ValueError("report is incomplete: expected results and reviews are required")
        with self.sessions.begin() as session:
            row = session.get(EvaluationReport, report_id, with_for_update=True)
            row.status = "completed"
        return self.get(report_id)


def export_json(service: ReportService, report_id: str) -> str:
    return json.dumps(service.export(report_id), ensure_ascii=False, indent=2)
