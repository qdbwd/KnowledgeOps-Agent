"""显式在线协议验证：三次请求，全部经网关计数；不导入真实业务材料。

运行前必须开启 MODEL_CALLS_ENABLED。脚本验证工具参数/执行/结果回传、
流式 JSON 输出与账本证据，不把接通成功当作业务验收通过。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from knowledgeops_agent.core.config import create_gateway, get_settings


@tool
def lookup_canary_source(source_id: str) -> str:
    """读取协议验证材料；唯一有效 source_id 为 canary-source。"""
    if source_id != "canary-source":
        raise ValueError("unknown canary source")
    return json.dumps(
        {
            "source_id": source_id,
            "page": 1,
            "text": "KnowledgeOps 使用版本检查防止并发修改互相覆盖。",
        },
        ensure_ascii=False,
    )


def main() -> None:
    """保留成功或失败证据；每次重跑仍占用首次验证与项目总额度。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    gateway = create_gateway(settings)
    root_id = f"canary-{uuid4()}"
    report: dict = {
        "root_id": root_id,
        "time": datetime.now(UTC).isoformat(),
        "model": settings.agent_model,
        "evidence_type": "real-model",
        "checks": {},
    }
    failed = False
    try:
        messages = [
            SystemMessage(
                "这是一个协议接通测试。首先调用 lookup_canary_source 读取 canary-source。"
                "得到工具结果后，用一句中文说明它的保护机制，并包含来源标识 canary-source。"
            ),
            HumanMessage("请读取指定测试材料并回答。"),
        ]
        first = gateway.invoke(
            messages,
            root_id=root_id,
            run_id=root_id,
            purpose="canary",
            root_limit=10,
            tools=[lookup_canary_source],
        )
        if len(first.tool_calls) != 1:
            raise ValueError("expected exactly one tool request")
        call = first.tool_calls[0]
        if call["name"] != "lookup_canary_source" or call["args"] != {"source_id": "canary-source"}:
            raise ValueError("provider returned unexpected structured tool arguments")
        report["checks"]["tool_schema_and_selection"] = True
        result = lookup_canary_source.invoke(call["args"])
        second = gateway.invoke(
            [*messages, first, ToolMessage(result, tool_call_id=call["id"])],
            root_id=root_id,
            run_id=root_id,
            purpose="canary",
            root_limit=10,
            tools=[lookup_canary_source],
        )
        if (
            second.tool_calls
            or "版本" not in str(second.content)
            or "canary-source" not in str(second.content)
        ):
            raise ValueError("tool result was not reflected in the final response")
        report["checks"]["tool_result_roundtrip"] = True
        third = gateway.invoke(
            [
                SystemMessage("只输出一个 JSON 对象，字段 ok 为 true，mechanism 为版本检查。"),
                HumanMessage("返回用于协议测试的 JSON 对象。"),
            ],
            root_id=root_id,
            run_id=root_id,
            purpose="canary",
            root_limit=10,
            stream=True,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(str(third.content))
        if parsed != {"ok": True, "mechanism": "版本检查"}:
            raise ValueError("structured JSON response did not match its contract")
        report["checks"]["streaming_json"] = True
        report["status"] = "passed"
    except Exception as exc:
        failed = True
        # 网关异常已脱敏；其他检查只包含脚本定义的固定错误文本。
        report["status"] = "failed"
        report["error"] = gateway._redact(f"{type(exc).__name__}: {exc}")
    finally:
        report["requests"] = gateway.ledger.records(root_id=root_id)
        report["requests_this_canary"] = len(report["requests"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(
            json.dumps(
                {key: value for key, value in report.items() if key != "requests"},
                ensure_ascii=False,
            )
        )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
