"""复用 DeepSeek 官方请求模板和 tokenizer，离线估计完整模型输入。

固定词表随包分发并校验，运行时不下载或请求供应商。官方模板比字符比例更
贴近实际输入，但服务器模板仍可能变化，因此账单以 API usage 为准。
"""

import hashlib
import json
from functools import lru_cache

from deepseek_recipe import (
    ChatCompletionRequest,
    ConversionOptions,
    DeepseekV41Encoding,
    Tokenizer,
)

from knowledgeops_agent.core.resources import package_resource

TOKENIZER_SHA256 = "81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99"


@lru_cache(maxsize=1)
def _encoding():
    """每个进程只加载一次固定词表；缺失/损坏时明确失败，不能回退成字节计数。"""
    path = package_resource("vendor/deepseek/tokenizer-v41.json")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != TOKENIZER_SHA256:
        raise ValueError("DeepSeek tokenizer checksum mismatch")
    tokenizer = Tokenizer.from_str(content.decode("utf-8"))
    return tokenizer, DeepseekV41Encoding().with_tokenizer(tokenizer)


def count_request_tokens(payload: dict) -> tuple[int, str]:
    """完整 SDK payload 走官方模板；离线替身的框架消息用同词表显式估算。

    官方转换器负责 system/tool schema/思考协议的模型格式转换。本项目不
    自行再造聊天模板。后者仅用于没有供应商序列化器的测试客户端，方法名不同，
    不能把测试 JSON 的计数当成供应商实际输入量。
    """
    tokenizer, encoding = _encoding()
    messages = payload.get("messages", [])
    if all("role" in message for message in messages):
        request = {"model": "deepseek-flash", **payload}
        # OpenAI SDK 在 HTTP 层合并 extra_body；计数在网络发送前完成同一合并。
        request.update(request.pop("extra_body", {}) or {})
        converted = ChatCompletionRequest(request).convert(ConversionOptions())
        return len(encoding.encode(converted.conversation)), "deepseek_v41_tokenizer_v1"
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return len(tokenizer.encode(serialized)), "deepseek_v41_json_estimate_v1"


def count_text_tokens(text: str) -> int:
    """用同一固定词表计量可选背景；无需网络，不替代完整请求容量检查。"""
    tokenizer, _ = _encoding()
    return len(tokenizer.encode(text))
