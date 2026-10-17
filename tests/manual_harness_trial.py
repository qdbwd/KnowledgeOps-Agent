"""显式、小范围收费联调；文件名不会被常规pytest收集，另需授权开关。

只验证本轮真实供应商、异步工具理解与MCP查找/读取/汇总。工程并发、故障及
权限已由确定性用例覆盖；此例不能代表任意知识整理的语义质量。
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from test_api_worker import services as _services

from knowledgeops_agent.app.runs.children import ChildRuns
from knowledgeops_agent.app.runs.service import Budgets
from knowledgeops_agent.app.runs.worker import execute_run
from knowledgeops_agent.core.config import Settings, create_chat_client
from knowledgeops_agent.harness.context.capacity import load_model_capacity
from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.harness.models.gateway import ModelGateway
from knowledgeops_agent.harness.subagents.async_tools import ASYNC_TOOLS
from knowledgeops_agent.harness.subagents.registry import SubagentRegistry
from knowledgeops_agent.harness.tools.catalog import tool_options
from knowledgeops_agent.harness.tools.policy import ToolPolicyService
from knowledgeops_agent.persistence.models import MCPCall, Run

services = _services


@pytest.mark.skipif(os.environ.get("KNOWLEDGEOPS_REAL_HARNESS_TRIAL") != "1", reason="requires explicit paid-trial authorization")
def test_real_model_async_mcp_discovery(services, tmp_path):
    settings = Settings()
    assert settings.model_calls_enabled and services.engine.url.database == "knowledgeops_test"
    services.settings.workspace_root = tmp_path / "workspace"
    services.gateway = ModelGateway(
        services.gateway.ledger, enabled=True, client=create_chat_client(settings),
        sensitive_values=[settings.openai_api_key.get_secret_value()],
        capacity=load_model_capacity(settings.agent_model, settings.openai_base_url),
    )
    registry = MCPRegistry(services.sessions)
    server = registry.create("验证材料读取", {"transport": "stdio", "command": sys.executable,
                            "args": [str(Path("tests/fixtures/harness_trial_mcp.py").resolve())]})
    server = registry.discover(server["id"], expected_revision=server["revision"])
    registry.update(server["id"], expected_revision=server["revision"], enabled=True, permissions={"read_topic": "read"})
    SubagentRegistry(services.sessions).create(
        name="trial-reader", display_name="验证材料读者", description="独立读取指定主题的验证材料并报告事实。",
        system_prompt="按委派任务从可用服务读取一个指定主题。材料通过工具目录查找后读取，报告原事实和材料标记，不扩写、不搜索互联网。",
        tools=[], tool_groups=["mcp"], skills=[], enabled=True,
    )
    enabled = {f"builtin:{tool.name}" for tool in ASYNC_TOOLS} | {"middleware:tool_search", "builtin:read_tool_result"}
    policy = ToolPolicyService(services.sessions)
    policy.update(expected_revision=policy.get()["revision"], enabled={row["id"]: row["id"] in enabled for row in tool_options()})
    chat = services.jobs.create_conversation("真实模型小范围Harness联调")
    root = services.jobs.enqueue(chat["id"],
        "这是自包含工程联调，不是知识入库。请用两个trial-reader异步子任务分别读取本地验证材料服务的预算与恢复主题，"
        "完整交代主题给它们；你可以先简短说明核对范围，再等待它们，不反复查询状态。"
        "最终用四句话说明预算剩余多少、子任务预算关系、关闭网页与服务重启的区别、两份材料标记。"
        "事实必须来自实际服务结果，不要自行填充，不保存文章。",
        request_key="real-trial", delegation=True,
        budgets=Budgets(model_requests=12, tool_calls=24, searches=0, active_seconds=300).model_dump())
    report_path = Path("artifacts/verification") / f"reliability-real-model-{uuid4().hex[:10]}.json"
    saver = InMemorySaver()
    futures = {}
    deadline = time.monotonic() + 300
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            while time.monotonic() < deadline:
                for key, future in list(futures.items()):
                    if future.done():
                        future.result()
                        del futures[key]
                with services.sessions() as session:
                    ready = [(row.id, row.generation) for row in session.scalars(select(Run).where(
                        Run.root_id == root["id"], Run.status == "queued",
                    ))]
                for identifier, generation in ready:
                    key = (identifier, generation)
                    if key not in futures:
                        futures[key] = pool.submit(execute_run, identifier, generation, services=services,
                                                   checkpointer=saver, worker_id=f"real-trial:{identifier}:{generation}")
                current = services.jobs.get_run(root["id"])
                if not futures and current["status"] != "queued":
                    break
                time.sleep(.05)
            else:
                services.jobs.cancel(root["id"])
        current = services.jobs.get_run(root["id"])
        children = ChildRuns(services.jobs).list(root["id"])
        assert current["status"] == "completed", current["stop_reason"]
        assert len(children) == 2 and all(child["status"] == "completed" for child in children)
        answer = current["outcome"].get("answer", "")
        assert all(marker in answer for marker in ("22", "松杉七", "石桥九")), answer
        with services.sessions() as session:
            calls = list(session.scalars(select(MCPCall).join(Run, Run.id == MCPCall.run_id).where(Run.root_id == root["id"])))
            assert len(calls) == 2 and all(call.status == "succeeded" and call.run_id != root["id"] for call in calls)
    finally:
        current = services.jobs.get_run(root["id"])
        children = ChildRuns(services.jobs).list(root["id"])
        records = services.gateway.ledger.records(root_id=root["id"])
        report = {"kind": "real-model-async-mcp-trial", "model": settings.agent_model,
                  "request_limit": 12, "requests": len(records), "run": current, "children": children,
                  "calls": [{"run_id": r["run_id"], "purpose": r["purpose"], "status": r["status"],
                             "response": r.get("response"), "usage": r.get("usage")} for r in records],
                  "semantic_review": "pending human-readable answer inspection",
                  "scope": "synthetic materials; real model and local MCP; isolated PG; no ordinary user data"}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(services.gateway._redact(report), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        services.jobs.close_mcp_sessions()
        print(f"Real trial report: {report_path}")
