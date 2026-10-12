"""验证当前 Agent 的业务状态、真实网络研究和扫描 PDF 引用；显式收费验证。

自动断言只验证链路/引用事实；回答全文保留供正文审阅，不把自动通过写成
语义质量已经全面通过。所有请求经过应用网关累计计入既有 600 次账本。
"""

import argparse
import json
import re
import time
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materials-report", type=Path, required=True)
    args = parser.parse_args()
    folder = Path("artifacts/verification") / ("agent_reading_" + uuid4().hex)
    folder.mkdir(parents=True)
    prior = json.loads(
        Path("artifacts/verification/web-slice-online.json").read_text(encoding="utf-8")
    )
    materials = json.loads(args.materials_report.read_text(encoding="utf-8"))
    scan = next(case for case in materials["cases"] if case["name"] == "chinese_scan")["source"]
    report = {"kind": "real-agent-reading", "cases": [], "manual_review": "pending"}
    tasks = [
        {
            "name": "current_state_qa",
            "conversation_id": prior["conversation_id"],
            "knowledge_only": True,
            "source_ids": [],
            "limit": 6,
            "content": "请重新核对知识库和实际发布状态：主 Agent 请求12次、子 Agent请求8次后还剩几次？关闭浏览器和服务重启有何差别？请引用已保存知识的固定版本，并说明那篇草稿现在是否已经发布。",
        },
        {
            "name": "public_research",
            "knowledge_only": False,
            "source_ids": [],
            "limit": 10,
            "content": "请研究 Python 虚拟环境能否直接复制到另一台机器使用。先检索知识库，不足时自主搜索并实际阅读 Python 官方文档，再解释原因和正确迁移方式，引用你实际读到的资料。只回答，不保存文章。",
        },
        {
            "name": "scanned_pdf",
            "knowledge_only": True,
            "source_ids": [scan["id"]],
            "limit": 6,
            "content": "请读取本次关联的扫描 PDF，说明材料中的工具预算和服务重启规则，提供可打开并定位页码的来源引用。只依据该材料作答，不保存文章。",
        },
    ]
    with httpx.Client(base_url="http://web", headers={"Host": "localhost"}, timeout=30) as client:
        settings = client.get("/api/settings").raise_for_status().json()
        assert settings["data_scope"] == "verification" and settings["model_calls_enabled"]
        report["before_usage"] = settings["usage"]
        try:
            for task in tasks:
                conversation_id = (
                    task.get("conversation_id")
                    or client.post("/api/conversations", json={"title": "验证：" + task["name"]})
                    .raise_for_status()
                    .json()["id"]
                )
                submitted = (
                    client.post(
                        f"/api/conversations/{conversation_id}/messages",
                        json={
                            "content": task["content"],
                            "request_key": str(uuid4()),
                            "publication_mode": "preview",
                            "knowledge_only": task["knowledge_only"],
                            "delegation": False,
                            "source_ids": task["source_ids"],
                            "budgets": {
                                "model_requests": task["limit"],
                                "tool_calls": 25,
                                "searches": 5,
                                "active_seconds": 300,
                            },
                        },
                    )
                    .raise_for_status()
                    .json()
                )
                deadline = time.monotonic() + 330
                run = submitted
                while time.monotonic() < deadline:
                    run = client.get(f"/api/runs/{run['id']}").raise_for_status().json()
                    if run["status"] in {
                        "completed",
                        "failed",
                        "interrupted",
                        "cancelled",
                        "budget_exhausted",
                    }:
                        break
                    time.sleep(1)
                else:
                    client.post(f"/api/runs/{run['id']}/cancel").raise_for_status()
                    raise TimeoutError("verification time limit; cancellation requested")
                records = client.get(f"/api/runs/{run['id']}/records").raise_for_status().json()
                refs = re.findall(r"/api/evidence/([0-9a-f-]+)", run["outcome"].get("answer", ""))
                citations = []
                for ref in dict.fromkeys(refs):
                    response = client.get(f"/api/evidence/{ref}")
                    citations.append(
                        response.json()
                        if response.is_success
                        else {"error": "unresolved", "id": ref}
                    )
                case = {
                    "name": task["name"],
                    "run": run,
                    "citations": citations,
                    "records": records,
                }
                case["automatic_checks_passed"] = (
                    run["status"] == "completed"
                    and bool(citations)
                    and all("error" not in cite for cite in citations)
                )
                if task["name"] == "scanned_pdf":
                    case["automatic_checks_passed"] &= any(
                        cite.get("source_version_id") == scan["version_id"]
                        and cite.get("page") == 1
                        for cite in citations
                    )
                if task["name"] == "public_research":
                    tools = {
                        event["data"].get("name")
                        for event in records["events"]
                        if event["kind"] == "tool_started"
                    }
                    case["automatic_checks_passed"] &= {"search_web", "read_web"} <= tools and any(
                        (cite.get("origin") or "").startswith("https://docs.python.org/")
                        for cite in citations
                    )
                report["cases"].append(case)
                print(
                    {
                        "case": task["name"],
                        "run_id": run["id"],
                        "status": run["status"],
                        "checks": case["automatic_checks_passed"],
                    },
                    flush=True,
                )
        finally:
            report["after_usage"] = client.get("/api/settings").raise_for_status().json()["usage"]
            (folder / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    print({"report": str(folder / "report.json"), "usage": report["after_usage"]}, flush=True)


if __name__ == "__main__":
    main()
