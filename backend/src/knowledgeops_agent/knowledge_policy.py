"""加载应用自身的可信领域规则，不依赖聊天历史或模型自行发现文件。

调用方必须在构建主/子 Agent 时把 prompt_text 加入系统提示，并可记录 metadata。
资源由项目维护，模型与用户材料不能改写加载路径；读取失败不提供空规则后备。
版本表示维护者标识，SHA-256 标识实际字节，二者用于追溯，不是行为验收或签名。
"""

import hashlib
import re
from dataclasses import dataclass

from knowledgeops_agent.config_resources import bundled_resource, resource_path

_POLICY_PATH = bundled_resource("config/knowledge-policy.md")
_RESOURCE_NAME = "knowledgeops_agent/prompts/knowledge_policy.md"


class KnowledgePolicyError(RuntimeError):
    """可信规则缺失或损坏；调用方应终止装配并报告配置错误。"""


@dataclass(frozen=True)
class KnowledgePolicy:
    """一次装配使用的规则快照；正文、版本与哈希一起流向模型和运行记录。"""

    version: str
    sha256: str
    content: str
    resource: str = _RESOURCE_NAME

    @property
    def metadata(self) -> dict[str, str]:
        """返回可持久化的来源标识，不把全文重复写入每条观测事件。"""
        return {"version": self.version, "sha256": self.sha256, "resource": self.resource}

    @property
    def prompt_text(self) -> str:
        """系统提示片段保留完整正文，并明确标识本次实际装载的规则版本。"""
        return f"[KnowledgeOps 应用领域规则；版本={self.version}；SHA-256={self.sha256}]\n{self.content}"


def load_knowledge_policy() -> KnowledgePolicy:
    """每次读取固定可信资源，验证基本格式并计算实际字节哈希。

    不缓存旧正文，避免新运行继续使用已被项目更新的规则。只检查可装载合同；
    正文质量仍需人工审读，Agent 是否遵循规则还需独立行为评估。
    """
    try:
        raw = resource_path("KNOWLEDGE_POLICY_PATH", _POLICY_PATH).read_bytes()
    except OSError as exc:
        raise KnowledgePolicyError(f"无法读取 KnowledgeOps 应用领域规则：{_RESOURCE_NAME}") from exc
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KnowledgePolicyError("KnowledgeOps 应用领域规则必须使用 UTF-8 编码") from exc
    if not content.strip():
        raise KnowledgePolicyError("KnowledgeOps 应用领域规则为空")
    version_lines = [line for line in content.splitlines() if line.startswith("版本：")]
    if len(version_lines) != 1:
        raise KnowledgePolicyError("KnowledgeOps 应用领域规则必须声明唯一版本")
    version = version_lines[0].removeprefix("版本：").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        raise KnowledgePolicyError("KnowledgeOps 应用领域规则版本格式无效")
    if not content.replace(version_lines[0], "", 1).strip():
        raise KnowledgePolicyError("KnowledgeOps 应用领域规则正文为空")
    return KnowledgePolicy(version=version, sha256=hashlib.sha256(raw).hexdigest(), content=content)
