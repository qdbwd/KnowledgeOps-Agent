"""最终回答的确定性检查，不把可解析引用误称为事实蕴含验证。

检查用户明确给出的字数上限与项目证据链接。失败回到模型修订，原响应仍在
轨迹中，最多两次修订且共用根预算；权限、取消、费用始终由原控制层执行。
"""

import re
from uuid import UUID

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage

from knowledgeops_agent.knowledge.citations import (
    citation_placeholders,
    evidence_ids,
    render_answer_citations,
)


def explicit_length_limit(task: str) -> int | None:
    """只识别明确的数字字数约束；不猜测“简洁”等语义需求，也不从材料取限制。"""
    matches = re.findall(r"(?:不超过|最多|至多|控制在)\s*(\d{1,6})\s*字", task)
    # 真人常用“150字以内”这种后置表达，约束含义与“最多150字”相同。
    # 仍只提取明确数字，不从“尽量短”或材料内容猜测硬上限。
    matches += re.findall(r"(\d{1,6})\s*字\s*(?:以内|之内|以下)", task)
    return min(map(int, matches)) if matches else None


def visible_length(text: str) -> int:
    """按非空白可见字符计数，链接计标签不计 URL；这是保守的 Markdown 近似。"""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return len(re.sub(r"[\s#*_`>]", "", text))


def current_length_limit(context) -> int | None:
    """本程序只强制识别明确字数要求；按已应用真人引导的顺序覆盖旧上限。

    材料、偏好和模型摘要不能提供新的硬限制。未识别的语义要求仍由模型遵循，
    不把这组有限模式描述为完整自然语言理解。
    """
    limit = explicit_length_limit(context.jobs.get_run(context.run_id)["task"])
    if context.run_id != context.root_id:
        return limit
    from knowledgeops_agent.app.runs.guidance import applied_messages

    for message in applied_messages(context.jobs, context.root_id):
        latest = explicit_length_limit(message["content"])
        if latest is not None:
            limit = latest
        elif re.search(r"取消字数限制|不限字数|不再限制字数|字数不再限制", message["content"]):
            limit = None
    return limit


class InvalidModelOutput(RuntimeError):
    """供应商返回不可交付的响应；保留原始记录，不能当成功答案或执行授权。"""


def leaked_tool_protocol(message) -> bool:
    """识别正文开头的原始 DSML 调用包装，不自行修复为可执行工具调用。

    仅覆盖实际遇到的协议泄漏形态；代码围栏/行内代码和正常解释不以该
    包装开头，因此仍可讨论协议。此函数不是通用 XML/工具解析器。
    """
    return (
        isinstance(message, AIMessage)
        and not message.tool_calls
        # 实际 I02 响应还出现双分隔符 calls 包装；只识别已观测前缀，
        # 不从参数正文恢复工具调用，也不匹配代码示例或说明文字中的标记。
        and bool(
            re.match(
                r"^\s*(?:<[|｜]DSML[|｜](?:tool_calls|invoke)|<｜｜DSML｜｜ calls)(?:\s|>)",
                message.text,
            )
        )
    )


PROTOCOL_FAILURE = (
    "模型未返回有效的结构化工具调用，而在正文中输出调用标记；操作未执行，进度已保留。"
)
OUTPUT_LIMIT_FAILURE = (
    "模型达到输出上限，回复未完整生成；当前响应不作完成依据，也不继续执行其中的工具指令。"
    "已完成结果保留，可缩小输出要求后发送新任务继续。"
)


def output_limit_reached(message) -> bool:
    """识别当前 DeepSeek/OpenAI 兼容渠道的供应商终止信号，不凭文字猜测。

    参考 DeerFlow 5ecc1c2 的 OpenAICompatibleLengthDetector：首选标准响应
    元数据，兼容原始附加字段。这里只适配本项目实际渠道，不引入其他供应商
    的检测策略或整套 harness；用户字符上限与供应商输出 token 上限是两回事。
    """
    if not isinstance(message, AIMessage):
        return False
    for metadata in (message.response_metadata, message.additional_kwargs):
        value = metadata.get("finish_reason")
        if isinstance(value, str) and value:
            return value.lower() == "length"
    return False


def model_output_error(message) -> str | None:
    """模型后处理与检查点交付共用失败判定；只返回应用定义的安全错误说明。"""
    if output_limit_reached(message):
        return OUTPUT_LIMIT_FAILURE
    return PROTOCOL_FAILURE if leaked_tool_protocol(message) else None


class FinalAnswerValidation(AgentMiddleware):
    """返回反馈字段供下一轮系统上下文使用，不伪装成用户新增指令。"""

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):
        last = state["messages"][-1]
        if not isinstance(last, AIMessage):
            return None
        context = runtime.context
        if output_limit_reached(last):
            # 即使参数恰好可解析，也不能证明整个意图在截断边界前已完整生成。
            # 在路由到 tools 之前停止，不改写原消息、不隐式重试或伪造工具回执。
            context.jobs.append_event(
                context.run_id,
                "model_output_limit",
                {
                    "finish_reason": "length",
                    "message_id": last.id,
                    "tool_calls": len(last.tool_calls),
                    "invalid_tool_calls": len(last.invalid_tool_calls),
                },
            )
            raise InvalidModelOutput(OUTPUT_LIMIT_FAILURE)
        if last.tool_calls:
            return None
        if leaked_tool_protocol(last):
            # 不从正文恢复工具调用，不触发额外收费重试，也不伪装成成功回复。
            context.jobs.append_event(
                context.run_id, "model_protocol_error", {"kind": "unexecuted_tool_markup"}
            )
            raise InvalidModelOutput(PROTOCOL_FAILURE)
        limit = current_length_limit(context)
        problems = []
        marker_ids = {
            evidence_id
            for marker in citation_placeholders(last.text)
            for evidence_id in evidence_ids(marker)
        }
        verified_ids = set()
        # 既支持 Markdown 链接，也支持说明历史链接的代码片段；反引号/中文标点是
        # URL 边界，不能将后续正文拼入 ID 后错误拒绝已经存在的来源。
        for evidence_id in evidence_ids(last.text):
            try:
                UUID(evidence_id)
                row = context.sources.get_evidence(evidence_id)
                if evidence_id in marker_ids and row["root_id"] != context.root_id:
                    raise ValueError("marker evidence was not read by this task")
                verified_ids.add(evidence_id)
            except (ValueError, KeyError):
                problems.append(
                    f"引用 {evidence_id} 不存在。只能使用已读工具返回的真实 citation；没有证据应说明不足。"
                )
        rendered: str = last.text
        try:
            rendered = render_answer_citations(last.text, verified_ids)
        except ValueError:
            problems.append(
                "引用仍有未定位的 citation 占位符或被未闭合代码围栏包裹；保留代码示例，"
                "另给出对应已读证据的完整 citation 链接。没有来源应说明不足，不能借用其他论断的来源。"
            )
        if limit is not None and visible_length(rendered) > limit:
            problems.append(
                f"最终答案可见字符 {visible_length(rendered)} 超过用户上限 {limit}；压缩到上限内，保留必要结论和引用。"
            )
        if not problems:
            if rendered != last.text:
                # 同一消息 ID 让图替换最终回复，保留 reasoning_content 等供应商协议字段。
                # 网关账本仍保存未经渲染的原响应；格式修正不额外调用模型。
                context.jobs.append_event(
                    context.run_id,
                    "answer_citations_rendered",
                    {"evidence_ids": sorted(verified_ids), "marker_ids": sorted(marker_ids)},
                )
                return {
                    "final_response_feedback": [],
                    "messages": [last.model_copy(update={"content": rendered})],
                }
            return {"final_response_feedback": []}
        count = state.get("answer_repair_count", 0)
        context.jobs.append_event(
            context.run_id,
            "answer_validation",
            {
                "issues": problems,
                "repair_number": count + 1,
                "max_repairs": 2,
            },
        )
        if count >= 2:
            raise ValueError("final answer validation failed after two repairs")
        return {
            "final_response_feedback": problems,
            "answer_repair_count": count + 1,
            "jump_to": "model",
        }
