"""收费联调：合成的长会话 + 两份独立资料，验证真实研究委派与历史摘要。

只写当前已建立的 verification schema，调用账本继续使用原 accounting 库。
合成历史明确标记为测试输入，不伪造已有模型回答或真实业务记录。
"""

import json
import os
import re
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.db import ChatMessage, Run, utcnow
from knowledgeops_agent.jobs import Budgets
from knowledgeops_agent.task_queue import create_queue


def main():
    manifest = json.loads(Path(".local/web-verification.json").read_text())
    schema = manifest["schema"]
    assert re.fullmatch(r"verify_[0-9a-f]{32}", schema)
    engine = create_engine(
        os.environ["TEST_DATABASE_URL"], connect_args={"options": f"-csearch_path={schema}"}
    )
    assert engine.url.database == "knowledgeops_test"
    folder = Path("artifacts/verification") / ("delegation_" + uuid4().hex)
    folder.mkdir(parents=True)
    report = {
        "kind": "real-delegation-long-history",
        "synthetic_history": True,
        "manual_review": "pending",
    }
    queue = create_queue(os.environ["CELERY_BROKER_URL"])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if queue.control.ping(timeout=0.5):
            break
    else:
        raise RuntimeError("worker not ready")
    queue.close()
    with httpx.Client(base_url="http://web", headers={"Host": "localhost"}, timeout=30) as client:
        settings = client.get("/api/settings").raise_for_status().json()
        assert settings["data_scope"] == "verification" and settings["model_calls_enabled"]
        report["before_usage"] = settings["usage"]
        try:
            sources = [
                client.post("/api/sources/text", json={"title": title, "content": content})
                .raise_for_status()
                .json()
                for title, content in [
                    (
                        "委派验证材料 A：缓存",
                        "这是自包含的工程验证材料，并非外部机构制度。网页快照缓存时长为60分钟，下载的解码内容上限为2 MiB。超过上限应明确失败，不能截断后说已读全文。",
                    ),
                    (
                        "委派验证材料 B：恢复",
                        "这是自包含的工程验证材料，并非外部机构制度。浏览器关闭不取消后台任务。服务重启后须用户显式恢复。正文版本、索引和幂等回执在同一事务提交，恢复重放不得重复创建知识版本。",
                    ),
                ]
            ]
            chat = (
                client.post("/api/conversations", json={"title": "验证：长历史与研究委派"})
                .raise_for_status()
                .json()
            )
            sessions = sessionmaker(engine)
            with sessions.begin() as session:
                now = utcnow() - timedelta(hours=1)
                seed_id = str(uuid4())
                session.add(
                    Run(
                        id=seed_id,
                        root_id=seed_id,
                        conversation_id=chat["id"],
                        checkpoint_id=seed_id,
                        task="合成上下文测试输入",
                        input_data={},
                        kind="fixture",
                        budgets=Budgets().model_dump(),
                        status="completed",
                        created_at=now,
                    )
                )
                session.flush()
                for index in range(24):
                    content = (
                        "合成历史要求：最终回答末尾须保留验证标记『松杉七』。只输出有来源的事实，不能自行保存知识。"
                        if index == 0
                        else f"合成讨论背景 {index}：运行、材料、证据与知识版本是不同对象。"
                    )
                    session.add(
                        ChatMessage(
                            conversation_id=chat["id"],
                            run_id=seed_id,
                            role="user" if index % 2 == 0 else "assistant",
                            content=content,
                            created_at=now + timedelta(seconds=index),
                        )
                    )
            task = f"请在同轮使用两个标准task调用，subagent_type均选内置researcher，将两份资料各交给一个只读研究子任务并行核对，每人只读分配的一份。材料A ID={sources[0]['id']}；材料B ID={sources[1]['id']}。然后用不超过350字综合说明缓存/下载上限及浏览器关闭与服务重启的差别、如何防止重复写入。必须使用两份实际读过的材料引用，保持早期约定的验证标记。只回答，不保存，也不联网。"
            run = (
                client.post(
                    f"/api/conversations/{chat['id']}/messages",
                    json={
                        "content": task,
                        "request_key": str(uuid4()),
                        "publication_mode": "preview",
                        "knowledge_only": True,
                        "delegation": True,
                        "source_ids": [s["id"] for s in sources],
                        "budgets": {
                            "model_requests": 18,
                            "tool_calls": 40,
                            "searches": 0,
                            "active_seconds": 300,
                        },
                    },
                )
                .raise_for_status()
                .json()
            )
            deadline = time.monotonic() + 330
            while time.monotonic() < deadline:
                run = client.get(f"/api/runs/{run['id']}").raise_for_status().json()
                if run["status"] in {
                    "completed",
                    "failed",
                    "cancelled",
                    "interrupted",
                    "budget_exhausted",
                }:
                    break
                time.sleep(1)
            else:
                client.post(f"/api/runs/{run['id']}/cancel").raise_for_status()
                raise TimeoutError("verification time limit")
            runs = client.get(f"/api/runs?root_id={run['id']}").raise_for_status().json()
            records = [
                client.get(f"/api/runs/{row['id']}/records").raise_for_status().json()
                for row in runs
            ]
            answer = run["outcome"].get("answer", "")
            citations = [
                client.get(f"/api/evidence/{ref}").raise_for_status().json()
                for ref in set(re.findall(r"/api/evidence/([a-f0-9-]+)", answer))
            ]
            root_records = next(record for record in records if record["run"]["id"] == run["id"])
            children = [row for row in runs if row["parent_id"]]
            checks = {
                "completed": run["status"] == "completed",
                "two_completed_children": len(children) == 2
                and all(c["status"] == "completed" for c in children),
                "summary_charged": any(
                    m["purpose"] == "context_summary" for m in root_records["models"]
                ),
                "early_requirement_retained": "松杉七" in answer,
                "both_sources_cited": {s["version_id"] for s in sources}
                <= {c.get("source_version_id") for c in citations},
                "no_knowledge_write": not run["outcome"].get("draft_ids")
                and not run["outcome"].get("published_ids"),
            }
            report.update(
                run=run, children=children, records=records, citations=citations, checks=checks
            )
            print({"run_id": run["id"], "checks": checks}, flush=True)
        finally:
            report["after_usage"] = client.get("/api/settings").raise_for_status().json()["usage"]
            (folder / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            engine.dispose()
            print(
                {"report": str(folder / "report.json"), "usage": report["after_usage"]}, flush=True
            )


if __name__ == "__main__":
    main()
