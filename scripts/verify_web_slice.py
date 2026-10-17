"""通过真实 Web/API/Redis/worker 验证首个收费业务闭环，生成可追溯演示数据。

运行前必须显式启用模型；全程受现行持久账本累计总额约束。使用独立创作的
演示材料，不访问个人知识库。产物和运行 ID 保存到 artifacts，重复运行会新建
演示会话，只有明确要再次验证时才执行。
"""

import json
import re
import time
from pathlib import Path
from uuid import uuid4

import httpx

from knowledgeops_agent.app.runs.queue import create_queue
from knowledgeops_agent.core.config import get_settings


def main():
    # Compose 的 Started 只表示进程启动；等消费者就绪后再创建本轮任务，
    # 避免新任务落在启动恢复扫描之前而被正确标记为 interrupted。
    queue = create_queue(get_settings().celery_broker_url)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if queue.control.ping(timeout=1):
                break
        else:
            raise RuntimeError("worker must be ready before online verification")
    finally:
        queue.close()
    output = Path("artifacts/verification") / ("web-slice-online-" + uuid4().hex + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url="http://web", headers={"Host": "localhost"}, timeout=30) as client:
        settings = client.get("/api/settings").raise_for_status().json()
        if not settings["model_calls_enabled"]:
            raise RuntimeError("explicit MODEL_CALLS_ENABLED=true is required")
        if settings["data_scope"] != "verification":
            raise RuntimeError("use the isolated verification Compose override before this script")
        report = {"kind": "real-model-web-slice", "before_usage": settings["usage"], "runs": []}

        def save():
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        def wait(run_id):
            deadline = time.monotonic() + 300
            previous = None
            while time.monotonic() < deadline:
                run = client.get(f"/api/runs/{run_id}").raise_for_status().json()
                signature = (run["status"], run["tool_calls"], run["model_usage"]["root_used"])
                if signature != previous:
                    print(
                        {
                            "run_id": run_id,
                            "status": signature[0],
                            "tools": signature[1],
                            "models": signature[2],
                        },
                        flush=True,
                    )
                    previous = signature
                if run["status"] in {
                    "completed",
                    "failed",
                    "cancelled",
                    "budget_exhausted",
                    "interrupted",
                }:
                    report["runs"].append(run)
                    records = client.get(f"/api/runs/{run_id}/records").raise_for_status().json()
                    (output.parent / f"web-slice-{run_id}.json").write_text(
                        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    report["after_usage"] = (
                        client.get("/api/settings").raise_for_status().json()["usage"]
                    )
                    save()
                    if run["status"] != "completed":
                        raise RuntimeError(f"run stopped: {run['status']}: {run['stop_reason']}")
                    return run
                time.sleep(1)
            client.post(f"/api/runs/{run_id}/cancel").raise_for_status()
            raise TimeoutError("verification deadline reached; cancellation requested")

        source = (
            client.post(
                "/api/sources/text",
                json={
                    "title": "演示材料：研究助手的预算与恢复规则",
                    "content": "本材料是 KnowledgeOps 工程验证使用的自包含演示规则，不是外部机构制度。\n"
                    "每个根任务默认最多 40 次模型请求、80 次工具执行尝试、20 次搜索和 1200 秒活跃时间。"
                    "主 Agent 和研究子 Agent 共用根任务预算，失败请求和重试也计入。"
                    "例如主 Agent 使用 12 次请求，研究子 Agent 使用 8 次，则该根任务还剩 20 次模型请求。\n"
                    "关闭浏览器不取消后台任务。服务重启后任务标记为中断，等待用户明确恢复，不自动调用模型。"
                    "恢复使用原检查点和已有消费记录；已提交的知识版本不会因为重复投递而再次创建。"
                    "预算耗尽不等于业务成功，应该展示部分结果和停止原因，由用户决定是否增加额度继续。",
                },
            )
            .raise_for_status()
            .json()
        )
        conversation = (
            client.post("/api/conversations", json={"title": "真实闭环演示：共享预算与恢复"})
            .raise_for_status()
            .json()
        )
        report.update({"source_id": source["id"], "conversation_id": conversation["id"]})
        save()
        run = (
            client.post(
                f"/api/conversations/{conversation['id']}/messages",
                json={
                    "content": "请读取已关联的演示材料，整理一篇说明研究助手共享预算和中断恢复的知识文章，先给我看草稿。保留关键数字、计算例子、边界和原文引用，不需要联网。",
                    "request_key": str(uuid4()),
                    "publication_mode": "preview",
                    "knowledge_only": True,
                    "delegation": False,
                    "source_ids": [source["id"]],
                    "budgets": {
                        "model_requests": 10,
                        "tool_calls": 25,
                        "searches": 0,
                        "active_seconds": 240,
                    },
                },
            )
            .raise_for_status()
            .json()
        )
        report["submitted_run_id"] = run["id"]
        save()
        completed = wait(run["id"])
        drafts = (
            client.get("/api/changesets", params={"root_id": run["id"]}).raise_for_status().json()
        )
        assert len(drafts) == 1 and drafts[0]["status"] == "draft", (
            "preview must remain unpublished"
        )
        draft = drafts[0]
        body = draft["operations"][0]["body"]
        assert all(value in body for value in ("40", "80", "20", "1200")), (
            "draft lost source numbers"
        )
        citations = re.findall(r"/api/evidence/([0-9a-f-]+)", body)
        assert citations, "draft must cite an actual read"
        for evidence_id in citations:
            client.get(f"/api/evidence/{evidence_id}").raise_for_status()
        published = (
            client.post(
                f"/api/changesets/{draft['id']}/publish",
                json={
                    "expected_revision": draft["revision"],
                    "confirm_impact": draft["requires_impact_confirmation"],
                },
            )
            .raise_for_status()
            .json()
        )
        report["publication"] = published
        save()
        question = (
            client.post(
                f"/api/conversations/{conversation['id']}/messages",
                json={
                    "content": "请重新检索并读取刚保存的知识文章：主 Agent 已请求 12 次、子 Agent 已请求 8 次，还剩几次？关闭浏览器和服务重启分别会发生什么？引用知识库中的固定版本。",
                    "request_key": str(uuid4()),
                    "publication_mode": "preview",
                    "knowledge_only": True,
                    "delegation": False,
                    "budgets": {
                        "model_requests": 6,
                        "tool_calls": 12,
                        "searches": 0,
                        "active_seconds": 180,
                    },
                },
            )
            .raise_for_status()
            .json()
        )
        report["qa_run_id"] = question["id"]
        save()
        answer = wait(question["id"])
        assert "20" in answer["outcome"]["answer"]
        refs = re.findall(r"/api/evidence/([0-9a-f-]+)", answer["outcome"]["answer"])
        evidence = [client.get(f"/api/evidence/{ref}").raise_for_status().json() for ref in refs]
        assert any(
            item.get("knowledge_version_id") in published["version_ids"] for item in evidence
        )
        report["passed"] = True
        report["draft_outcome"] = completed["outcome"]
        save()
        print({"passed": True, "report": str(output), "usage": report["after_usage"]}, flush=True)


if __name__ == "__main__":
    main()
