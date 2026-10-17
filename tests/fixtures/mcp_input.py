"""现代 MCP 两轮补问夹具；磁盘计数证明恢复未重发首轮，无模型或外部业务。"""

import sys
from pathlib import Path

from fastmcp import Context, FastMCP
from mcp.server.request_state import RequestStateSecurity
from mcp.types import (
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitRequestURLParams,
    InputRequiredResult,
)

# 服务端也须保留可核验的续接签名密钥；客户端保存 token 无法补救服务丢失密钥。
security = RequestStateSecurity(keys=[Path(sys.argv[1]).read_bytes()]) if len(sys.argv) > 1 else None
server = FastMCP("durable-input-probe", request_state_security=security)


@server.tool
async def live_collect(ctx: Context, log_path: str, url_mode: bool = False) -> dict:
    """旧式反向补问，用于验证客户端会明确报告限制而非假造拒绝。"""
    with Path(log_path).open("a") as output:
        output.write("legacy_first\n")
    response = await ctx.elicit("需要一条说明", response_type=str)
    return {"response": str(response)}


@server.tool
async def collect(ctx: Context, log_path: str, url_mode: bool = False) -> dict:
    """先记录首轮，再索取数量与补充信息；须回传不透明状态才能继续。"""
    path = Path(log_path)
    if ctx.request_state is None:
        with path.open("a") as output:
            output.write("first\n")
        return InputRequiredResult(
            request_state="opaque:round-one/+==",
            input_requests={"quantity": ElicitRequest(params=ElicitRequestFormParams(
                message="需要核对多少条？",
                requested_schema={"type": "object", "properties": {
                    "count": {"type": "integer", "minimum": 1, "maximum": 5, "title": "条数"},
                }, "required": ["count"]},
            ))},
        )
    assert ctx.input_responses is not None
    if ctx.request_state == "opaque:round-one/+==":
        answer = ctx.input_responses["quantity"]
        with path.open("a") as output:
            output.write(f"quantity:{answer.action}\n")
        if answer.action != "accept":
            return {"declined": True}
        assert answer.content and isinstance(answer.content["count"], int)
        return InputRequiredResult(
            request_state="opaque:round-two/+==",
            input_requests={"detail": ElicitRequest(params=ElicitRequestURLParams(
                message="请到服务页面完成确认，然后返回继续。", url="https://example.com/verify",
            ) if url_mode else ElicitRequestFormParams(
                message="选择说明深度。",
                requested_schema={"type": "object", "properties": {
                    "depth": {"type": "string", "enum": ["简要", "详细"], "title": "深度"},
                }, "required": ["depth"]},
            ))},
        )
    assert ctx.request_state == "opaque:round-two/+=="
    answer = ctx.input_responses["detail"]
    with path.open("a") as output:
        output.write(f"detail:{answer.action}\n")
    confirmed = answer.action == "accept" and (not url_mode or path.with_suffix(".verified").exists())
    return {"confirmed_by_server": confirmed, "content": answer.content}


if __name__ == "__main__":
    server.run(transport="stdio", show_banner=False)
