"""真实容器故障验收分两步；prepare 后由宿主 kill/start 本项目 worker，再 finish。

使用 40 页合成扫描 PDF 和隔离 verification schema，不调用模型，不操作正常材料。
"""

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from pypdf import PdfWriter

from knowledgeops_agent.task_queue import TASK_NAME, create_queue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["prepare", "finish"])
    args = parser.parse_args()
    path = Path(".local/worker-restart-verification.json")
    with httpx.Client(base_url="http://web", headers={"Host": "localhost"}, timeout=30) as client:
        settings = client.get("/api/settings").raise_for_status().json()
        assert settings["data_scope"] == "verification"
        if args.stage == "prepare":
            sys.path.insert(0, str(Path("tests").resolve()))
            from test_pdf_sources import scan_pdf, text_pdf

            writer = PdfWriter()
            writer.append(io.BytesIO(text_pdf()))
            scan = scan_pdf("chi_sim")
            for _ in range(39):
                writer.append(io.BytesIO(scan))
            writer.add_metadata({"/Title": "restart verification " + uuid4().hex})
            data = io.BytesIO()
            writer.write(data)
            result = (
                client.post(
                    "/api/sources/upload",
                    files={"file": ("restart-scan.pdf", data.getvalue(), "application/pdf")},
                )
                .raise_for_status()
                .json()
            )
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                source = client.get(f"/api/sources/{result['source_id']}").raise_for_status().json()
                if source["segments"]:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("worker did not parse first page")
            run = client.get(f"/api/runs/{result['job_id']}").raise_for_status().json()
            assert run["status"] == "running", "task completed before fault injection"
            report = {
                "kind": "real-worker-container-restart",
                "run_id": run["id"],
                "source_id": result["source_id"],
                "before_usage": settings["usage"],
                "before_source": source,
                "before_run": run,
            }
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(
                {
                    "ready_to_kill_worker": True,
                    "run_id": run["id"],
                    "completed_pages": len(source["segments"]),
                },
                flush=True,
            )
            return
        report = json.loads(path.read_text(encoding="utf-8"))
        queue = create_queue(os.environ["CELERY_BROKER_URL"])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if queue.control.ping(timeout=0.5):
                break
        else:
            raise RuntimeError("restarted worker not ready")
        run_id = report["run_id"]
        interrupted = client.get(f"/api/runs/{run_id}").raise_for_status().json()
        assert interrupted["status"] == "interrupted"
        queue.send_task(TASK_NAME, args=[run_id, report["before_run"]["generation"]])
        time.sleep(3)
        after_stale = client.get(f"/api/runs/{run_id}").raise_for_status().json()
        assert after_stale["status"] == "interrupted"
        assert client.get("/api/settings").json()["usage"] == report["before_usage"]
        resumed = client.post(f"/api/runs/{run_id}/resume", json={}).raise_for_status().json()
        assert resumed["generation"] == report["before_run"]["generation"] + 1
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            final = client.get(f"/api/runs/{run_id}").raise_for_status().json()
            if final["status"] in {
                "completed",
                "failed",
                "interrupted",
                "budget_exhausted",
                "cancelled",
            }:
                break
            time.sleep(0.5)
        source = client.get(f"/api/sources/{report['source_id']}").raise_for_status().json()
        after_usage = client.get("/api/settings").raise_for_status().json()["usage"]
        checks = {
            "completed_after_explicit_resume": final["status"] == "completed",
            "all_40_pages": source["status"] == "ready"
            and {s["page"] for s in source["segments"]} == set(range(1, 41)),
            "preserved_segment_ids": {s["id"] for s in report["before_source"]["segments"]}
            <= {s["id"] for s in source["segments"]},
            "unchanged_source_version": source["version_id"]
            == report["before_source"]["version_id"],
            "zero_model_requests": after_usage == report["before_usage"],
            "old_delivery_did_not_resume": after_stale["status"] == "interrupted",
        }
        report.update(
            interrupted=interrupted,
            after_stale=after_stale,
            resumed=resumed,
            final=final,
            source=source,
            after_usage=after_usage,
            checks=checks,
            records=client.get(f"/api/runs/{run_id}/records").raise_for_status().json(),
        )
        destination = Path("artifacts/verification") / ("worker-restart-" + run_id + ".json")
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        queue.close()
        print({"report": str(destination), "checks": checks}, flush=True)
        assert all(checks.values()), "restart verification failed; inspect preserved report"


if __name__ == "__main__":
    main()
