"""保存维护者逐例审读意见；不自动把模型裁判分数转成通过。

输入 JSON 为 {case_key: {accepted: bool, notes: str}}。只写对应意见并导出
完整报告，原始结果不可修改；--finish 要求全部案例与意见齐全。
"""

import argparse
import json
from pathlib import Path

from knowledgeops_agent.app.services import create_services
from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.diagnostics.reports import ReportService, export_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_id")
    parser.add_argument("--notes", type=Path)
    parser.add_argument("--finish", action="store_true")
    args = parser.parse_args()
    services = create_services(Settings(model_calls_enabled=False))
    try:
        reports = ReportService(services.sessions)
        if args.notes:
            for key, review in json.loads(args.notes.read_text()).items():
                if not isinstance(review.get("accepted"), bool):
                    raise ValueError("accepted must be boolean")
                reports.review(
                    args.report_id,
                    key,
                    accepted=review["accepted"],
                    notes=review["notes"],
                    semantic_findings=review.get("semantic_findings"),
                )
        if args.finish:
            reports.finish(args.report_id)
        folder = Path("artifacts/evaluations") / args.report_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "report.json").write_text(export_json(reports, args.report_id), encoding="utf-8")
        print(json.dumps(reports.get(args.report_id)["summary"], ensure_ascii=False))
    finally:
        services.close()


if __name__ == "__main__":
    main()
