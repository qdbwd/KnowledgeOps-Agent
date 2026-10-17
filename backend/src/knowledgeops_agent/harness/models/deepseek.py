"""DeepSeek 协议适配，业务层继续使用 LangChain 标准消息。

锁定的 langchain-deepseek 已接收 reasoning_content，但发送路径仍需补齐。
复用DeerFlow固定版本的assistant匹配辅助函数，避免假定所有消息严格zip。
空字符串仅用于显式思考模式工具轮的协议补位，不表示恢复了缺失的推理。
出处和MIT许可证见vendor/deerflow；测试同时覆盖真实SDK和序列化变形。
"""

from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek

from knowledgeops_agent.vendor.deerflow.assistant_payload_replay import (
    restore_assistant_payloads,
    restore_reasoning_content,
)


class DeepSeekClient(ChatDeepSeek):
    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        """在SDK最终payload上恢复真实字段；逐请求设置优先于客户端默认值。

        参考DeerFlow PatchedChatDeepSeek的空content处理。没有沿用其
        多配置中任一enabled即启用的判断，以免本次disabled被旧默认覆盖。
        本项目应用回执在上游已转换为控制信息，不引入DeerFlow专用回退标记。
        """
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        messages = self._convert_input(input_).to_messages()
        extra = payload.get("extra_body") or {}
        thinking = extra.get("thinking", payload.get("thinking", {}))
        enabled = isinstance(thinking, dict) and thinking.get("type") == "enabled"

        def restore(outgoing: dict, original: AIMessage) -> None:
            restore_reasoning_content(outgoing, original)
            if outgoing.get("tool_calls"):
                if outgoing.get("content") is None:
                    outgoing["content"] = ""
                if enabled and "reasoning_content" not in outgoing:
                    outgoing["reasoning_content"] = ""

        restore_assistant_payloads(payload.get("messages", []), messages, restore)
        return payload
