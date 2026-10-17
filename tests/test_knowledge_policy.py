"""领域规则必须真实加载且可追溯；这些合同不冒充模型遵循规则的行为评估。"""

import hashlib
import importlib

import pytest


def test_packaged_policy_delivers_identifiable_rules_to_prompt():
    policy_module = importlib.import_module("knowledgeops_agent.knowledge.policy")
    policy = policy_module.load_knowledge_policy()
    original = policy_module._POLICY_PATH.read_bytes()
    assert policy.content == original.decode("utf-8")
    assert policy.sha256 == hashlib.sha256(original).hexdigest()
    assert policy.metadata == {
        "version": policy.version,
        "sha256": policy.sha256,
        "resource": "knowledgeops_agent/prompts/knowledge_policy.md",
    }
    assert policy.content in policy.prompt_text
    assert policy.version in policy.prompt_text
    assert policy.sha256 in policy.prompt_text


def test_missing_policy_stops_instead_of_returning_empty_rules(tmp_path, monkeypatch):
    policy_module = importlib.import_module("knowledgeops_agent.knowledge.policy")
    monkeypatch.setattr(policy_module, "_POLICY_PATH", tmp_path / "absent.md")
    with pytest.raises(policy_module.KnowledgePolicyError, match="无法读取"):
        policy_module.load_knowledge_policy()


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (b"", "规则为空"),
        (b"\xff", "UTF-8"),
        ("没有版本的正文".encode(), "版本"),
        ("版本：\n正文".encode(), "版本格式"),
        ("版本：test-v1\n".encode(), "规则正文为空"),
        ("版本：test-v1\n版本：test-v2\n正文".encode(), "版本"),
    ],
)
def test_invalid_policy_cannot_silently_disable_domain_rules(content, error, tmp_path, monkeypatch):
    policy_module = importlib.import_module("knowledgeops_agent.knowledge.policy")
    resource = tmp_path / "policy.md"
    resource.write_bytes(content)
    monkeypatch.setattr(policy_module, "_POLICY_PATH", resource)
    with pytest.raises(policy_module.KnowledgePolicyError, match=error):
        policy_module.load_knowledge_policy()


def test_resource_update_changes_prompt_and_identity_without_stale_cache(tmp_path, monkeypatch):
    policy_module = importlib.import_module("knowledgeops_agent.knowledge.policy")
    resource = tmp_path / "policy.md"
    resource.write_text("版本：test-v1\n先核对适用范围。\n", encoding="utf-8")
    monkeypatch.setattr(policy_module, "_POLICY_PATH", resource)
    first = policy_module.load_knowledge_policy()
    resource.write_text("版本：test-v1\n先核对适用范围和来源。\n", encoding="utf-8")
    second = policy_module.load_knowledge_policy()
    assert first.version == second.version == "test-v1"
    assert first.sha256 != second.sha256
    assert first.prompt_text != second.prompt_text
    assert second.content == "版本：test-v1\n先核对适用范围和来源。\n"
