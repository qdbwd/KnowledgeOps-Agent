"""本地有状态 MCP 夹具：只用会话计数、进程身份与受控延迟验证连续调用。"""

import argparse
import asyncio
import os

from fastmcp import Context, FastMCP

server = FastMCP("stateful-session-probe")


@server.tool
async def counter(ctx: Context, delay: float = 0) -> dict:
    """增加当前 MCP 会话计数；不同客户端会话不得读到别人的计数。"""
    count = (await ctx.get_state("count") or 0) + 1
    await ctx.set_state("count", count)
    if delay:
        await asyncio.sleep(delay)
    return {"count": count, "pid": os.getpid(), "session": ctx.session_id}


@server.tool
async def exit_process() -> None:
    """仅供 stdio 断连验证；模拟远端执行后在回包前退出。"""
    os._exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--http-port", type=int)
    args = parser.parse_args()
    if args.http_port:
        server.run(
            transport="http",
            host="127.0.0.1",
            port=args.http_port,
            stateless_http=False,
            show_banner=False,
        )
    else:
        server.run(transport="stdio", show_banner=False)
