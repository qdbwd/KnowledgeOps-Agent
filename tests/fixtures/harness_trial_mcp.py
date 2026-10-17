"""真实模型小范围联调的本地只读材料服务；不包含项目或用户资料。"""

from typing import Literal

from fastmcp import FastMCP

server = FastMCP("harness-trial-materials")


@server.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def read_topic(topic: Literal["预算", "恢复"]) -> dict:
    """读取验证材料。topic为预算或恢复；返回固定事实和材料标记，不执行任何修改。"""
    return {
        "topic": topic,
        "content": (
            "验证材料A：根任务预算为37次，主任务已用9次，两个子任务合计已用6次，剩余22次。子任务不另领预算。材料标记：松杉七。"
            if topic == "预算" else
            "验证材料B：关闭网页不取消后台任务；服务重启须真人明确恢复。已保存的模型响应和成功工具回执可重放，不重复请求或副作用。材料标记：石桥九。"
        ),
    }


if __name__ == "__main__":
    server.run(transport="stdio", show_banner=False)
