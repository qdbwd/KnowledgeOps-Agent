"""两篇虚构材料与只读工具：不访问磁盘知识库，也不访问网络。

工具描述和参数类型会暴露给模型；下面的 Python 实现由 Runtime 执行。
教学注释放在函数内部，避免把冗长的实现说明一起发送给模型。
"""

from typing import Literal

from langchain_core.tools import tool

# 材料刻意把关键条件放在 context 中，使片段不足以支持最终判断。
# 修改这里的材料可以观察真实模型的决策变化；脚本决策器只服务于原始夹具。
DOCUMENTS = {
    "A": {
        "title": "查询请求重试",
        "excerpt": "调用失败后可以重试。",
        "context": "本文讨论无业务写入副作用、可以安全重复执行的查询。临时失败后可以重试。",
    },
    "B": {
        "title": "扣款请求响应丢失",
        "excerpt": "调用失败后不能直接重试。",
        "context": (
            "本文讨论扣款可能已执行成功、但响应丢失的情况。"
            "在核对结果或具备可靠幂等保障之前，不能盲目重放扣款请求。"
        ),
    },
}


@tool
def list_documents() -> list[dict[str, str]]:
    """列出教学材料的 ID 和标题。需要正文时再调用 read_document。"""
    # 返回新建字典，避免调用方修改结果时顺带改写原始教学材料。
    return [{"id": key, "title": value["title"]} for key, value in DOCUMENTS.items()]


@tool
def read_document(doc_id: Literal["A", "B"], view: Literal["excerpt", "context"]) -> dict:
    """读取指定材料的片段或上下文；判断观点冲突前应核对 context 中的适用条件。

    Args:
        doc_id: list_documents 返回的材料 ID，只允许 A 或 B。
        view: excerpt 是简短观点，context 包含适用条件。
    """
    # Literal 参与生成标准工具 Schema。BaseTool.invoke 会先校验参数，
    # 因而不存在的 ID 不会进入这里；校验失败由 Runtime 变成工具错误反馈。
    return {"doc_id": doc_id, "view": view, "text": DOCUMENTS[doc_id][view]}


TOOLS = [list_documents, read_document]
