"""命令行入口：显示可观察消息，可选保存本地 JSON 轨迹。

默认离线；只有 --mode online 才读取 .env 并创建真实模型客户端。
两种模式均不自动开启远程追踪，也不操作两套知识库。
"""

import argparse
import json
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage
from langsmith import tracing_context

from agent_loop.models import DEFAULT_QUESTION, ScriptedModel, create_online_model
from agent_loop.runtime import build_agent, initial_state


def main(argv: list[str] | None = None) -> int:
    """解析配置、运行图并输出终态；返回 0=已回答、1=未完成、2=配置错误。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["scripted", "online"], default="scripted")
    parser.add_argument("--scenario", choices=["success", "recover", "loop"], default="success")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--max-model-calls", type=int, default=6)
    parser.add_argument("--max-tool-calls", type=int, default=8)
    parser.add_argument("--trace", type=Path, help="可选本地 JSON 文件；不会覆盖已有文件")
    args = parser.parse_args(argv)
    try:
        if args.mode == "scripted" and args.question != DEFAULT_QUESTION:
            raise ValueError("脚本只演示固定任务；自定义问题请使用 online 模式")
        if args.mode == "online" and args.scenario != "success":
            raise ValueError("--scenario 是离线脚本选项，online 模式不使用预写情节")
        if args.trace and args.trace.exists():
            raise ValueError("轨迹文件已存在，请选择新路径")
        state = initial_state(args.question)
        model = ScriptedModel(args.scenario) if args.mode == "scripted" else create_online_model()
        graph = build_agent(
            model, max_model_calls=args.max_model_calls, max_tool_calls=args.max_tool_calls
        )
    except ValueError as exc:
        parser.error(str(exc))

    print(f"mode={args.mode} | scenario={args.scenario}")
    if args.mode == "scripted":
        print("离线脚本演示：验证 Runtime，不代表真实模型自主决策能力。")
    seen = 0
    # 禁用继承的 LangSmith 自动追踪，保证离线演示不因环境变量向外发送记录。
    # 递归上限是图的保险丝；业务预算先于它生效，不把图异常当正常终止。
    with tracing_context(enabled=False):
        for state in graph.stream(
            state,
            stream_mode="values",
            config={"recursion_limit": 2 * args.max_model_calls + 6},
        ):
            for message in state["messages"][seen:]:
                if isinstance(message, AIMessage) and message.tool_calls:
                    print("MODEL -> " + json.dumps(message.tool_calls, ensure_ascii=False))
                elif isinstance(message, ToolMessage):
                    print(f"TOOL [{message.tool_call_id}] {message.status}: {message.content}")
                elif message.content:
                    print(f"{message.type.upper()}: {message.text}")
            seen = len(state["messages"])

    print(
        f"status={state['status']} | model_calls={state['model_calls']} "
        f"| tool_calls={state['tool_calls']} | detail={state['detail']}"
    )
    if args.trace:
        # 只有用户显式指定 --trace 才写文件。x 模式还能防止检查后出现同名
        # 文件时被意外覆盖；轨迹保存外显消息，不保存客户端配置或隐藏推理。
        artifact = {**state, "mode": args.mode, "scenario": args.scenario}
        artifact["messages"] = [m.model_dump(mode="json") for m in state["messages"]]
        try:
            args.trace.parent.mkdir(parents=True, exist_ok=True)
            with args.trace.open("x", encoding="utf-8") as file:
                json.dump(artifact, file, ensure_ascii=False, indent=2)
        except OSError as exc:
            print(f"轨迹保存失败：{type(exc).__name__}")
            return 1
    return 0 if state["status"] == "answered" else 1
