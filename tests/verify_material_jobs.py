"""显式运行的真实 HTTP/Redis/worker 验证；用项目原创样本，不请求模型。"""

import io
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
from pypdf import PdfWriter
from test_pdf_sources import scan_pdf, text_pdf

from knowledgeops_agent.config import get_settings
from knowledgeops_agent.task_queue import create_queue


def main():
    folder = Path("artifacts/verification") / ("materials_" + uuid4().hex)
    folder.mkdir(parents=True)
    queue = create_queue(get_settings().celery_broker_url)
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if queue.control.ping(timeout=1):
                break
        else:
            raise RuntimeError("worker is not ready")
    finally:
        queue.close()
    report = {"kind": "real-material-jobs-no-model", "cases": [], "passed": False}
    with httpx.Client(base_url="http://web", timeout=30) as client:
        before = client.get("/api/settings").raise_for_status().json()
        assert before["data_scope"] == "verification"
        assert before["model_calls_enabled"] is False
        writer = PdfWriter()
        writer.append(io.BytesIO(text_pdf()))
        writer.append(io.BytesIO(scan_pdf("chi_sim")))
        merged = io.BytesIO()
        writer.write(merged)
        cases = {
            "text": text_pdf(),
            "english_scan": scan_pdf("eng"),
            "chinese_scan": scan_pdf("chi_sim"),
            "mixed": merged.getvalue(),
            "corrupt": b"%PDF-corrupt data",
        }

        def wait(run_id):
            end = time.monotonic() + 150
            while time.monotonic() < end:
                run = client.get(f"/api/runs/{run_id}").raise_for_status().json()
                if run["status"] in {
                    "completed",
                    "failed",
                    "interrupted",
                    "cancelled",
                    "budget_exhausted",
                }:
                    return run
                time.sleep(0.5)
            client.post(f"/api/runs/{run_id}/cancel").raise_for_status()
            raise TimeoutError("material task did not finish")

        try:
            for name, data in cases.items():
                (folder / f"{name}.pdf").write_bytes(data)
                accepted = (
                    client.post(
                        "/api/sources/upload",
                        files={"file": (f"{name}.pdf", data, "application/pdf")},
                    )
                    .raise_for_status()
                    .json()
                )
                run = wait(accepted["job_id"])
                source = (
                    client.get(f"/api/sources/{accepted['source_id']}").raise_for_status().json()
                )
                records = client.get(f"/api/runs/{run['id']}/records").raise_for_status().json()
                texts = [
                    client.get(f"/api/source-segments/{part['id']}")
                    .raise_for_status()
                    .json()["text"]
                    for part in source["segments"]
                ]
                passed = (
                    run["status"] == ("failed" if name == "corrupt" else "completed")
                    and source["status"] == ("failed" if name == "corrupt" else "ready")
                    and not records["models"]
                )
                if name != "corrupt":
                    passed = passed and "80" in "".join(texts)
                if name in {"chinese_scan", "mixed"}:
                    passed = passed and "恢复" in "".join(texts)
                report["cases"].append(
                    {
                        "name": name,
                        "run": run,
                        "source": source,
                        "texts": texts,
                        "records": records,
                        "passed": passed,
                    }
                )
                print({"case": name, "status": run["status"], "passed": passed}, flush=True)
            submitted = (
                client.post(
                    "/api/sources/url",
                    json={
                        "url": "https://docs.python.org/zh-cn/3/library/contextlib.html",
                        "request_key": str(uuid4()),
                    },
                )
                .raise_for_status()
                .json()
            )
            run = wait(submitted["id"])
            report["cases"].append(
                {"name": "public_web", "run": run, "passed": run["status"] == "completed"}
            )
            for case in report["cases"]:
                records = (
                    client.get(f"/api/runs/{case['run']['id']}/records").raise_for_status().json()
                )
                assert records["models"] == []
            report["passed"] = all(row["passed"] for row in report["cases"])
        finally:
            (folder / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    print({"passed": report["passed"], "report": str(folder / "report.json")}, flush=True)
    if not report["passed"]:
        raise RuntimeError("material verification failed; review saved evidence")


if __name__ == "__main__":
    main()
