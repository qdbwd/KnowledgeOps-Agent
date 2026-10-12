"""只用于本项目协议验证的MCP服务；没有外网/模型/业务数据库能力。"""

import argparse
import os

from fastmcp import FastMCP

server = FastMCP("KnowledgeOps protocol fixture")


def get_note(topic: str) -> dict:
    """返回合成材料和子进程环境隔离的观测结果。"""
    return {
        "topic": topic,
        "text": "保留原始来源后才能复核结论。",
        "inherited_key": bool(os.getenv("OPENAI_API_KEY")),
    }


if os.getenv("MCP_PROBE_SCHEMA") == "v2":

    def get_note_v2(topic: str, include_examples: bool = False) -> dict:
        """测试远端服务升级改变参数结构，旧运行不能继续沿用旧许可。"""
        return {**get_note(topic), "include_examples": include_examples}

    server.tool(name="get_note", annotations={"readOnlyHint": True})(get_note_v2)
else:
    server.tool(annotations={"readOnlyHint": True})(get_note)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--http-port", type=int)
    args = parser.parse_args()
    if args.http_port:
        server.run(transport="http", host="127.0.0.1", port=args.http_port, show_banner=False)
    else:
        server.run(transport="stdio", show_banner=False)
