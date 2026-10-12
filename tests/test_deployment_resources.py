"""部署资源可替换但仍冻结版本，错误配置不能变成默认值或扩大权限。"""

import json

import pytest

from knowledgeops_agent.config import Settings
from knowledgeops_agent.request_capacity import load_model_capacity
from knowledgeops_agent.run_resources import ResourceUnavailable, load_skills, skill_catalog
from knowledgeops_agent.web_search_config import load_web_search_config


def test_custom_skill_directory_uses_content_revision_and_real_read_backend(tmp_path, monkeypatch):
    from knowledgeops_agent.skill_resources import build_skill_middleware

    root = tmp_path / "skills"
    (root / "example").mkdir(parents=True)
    (root / "README.md").write_text("部署者说明，不注入", encoding="utf-8")
    text = "---\nname: example\ndescription: 核对实例\n---\n读取全部材料，再归纳。"
    (root / "example" / "SKILL.md").write_text(text, encoding="utf-8")
    monkeypatch.setenv("SKILLS_DIRECTORY", str(root))
    rows = load_skills()
    assert len(rows) == 1 and rows[0]["version"] == rows[0]["sha256"][:12]
    assert skill_catalog()[0]["description"] == "核对实例"
    backend, _ = build_skill_middleware({row["resource"]: row["content"] for row in rows})
    (root / "example" / "SKILL.md").write_text(text + "\n新要求", encoding="utf-8")
    assert backend.download_files([rows[0]["resource"]])[0].content.decode() == text
    assert load_skills()[0]["sha256"] != rows[0]["sha256"]
    monkeypatch.setenv("SKILLS_DIRECTORY", str(tmp_path / "missing"))
    with pytest.raises(ResourceUnavailable, match="缺失"):
        load_skills()


def test_environment_selects_validated_search_and_model_profile(tmp_path, monkeypatch):
    from knowledgeops_agent.request_capacity import ModelCapacity

    search = tmp_path / "search.yaml"
    search.write_text("version: 1\nmax_results: 2\nregion: us-en\n", encoding="utf-8")
    monkeypatch.setenv("WEB_SEARCH_CONFIG_PATH", str(search))
    assert load_web_search_config().max_results == 2
    profile = tmp_path / "model.json"
    data = ModelCapacity(model="compatible-model", endpoint_host="example.test").model_dump()
    profile.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("MODEL_CAPACITY_PATH", str(profile))
    assert load_model_capacity("compatible-model", "https://example.test/v1").model == "compatible-model"
    with pytest.raises(ValueError, match="does not match"):
        load_model_capacity("different-model", "https://example.test/v1")
    with pytest.raises(ValueError, match="API_ALLOWED_HOSTS"):
        Settings(_env_file=None, model_calls_enabled=False, api_allowed_hosts="*")


@pytest.mark.parametrize("declaration", [
    "name: [broken", "name: example\ndescription: 42",
    "name: example\ndescription: valid\nmetadata: unexpected-text",
])
def test_invalid_imported_skill_has_actionable_configuration_error(tmp_path, monkeypatch, declaration):
    directory = tmp_path / "example"
    directory.mkdir()
    (directory / "SKILL.md").write_text(f"---\n{declaration}\n---\nBody", encoding="utf-8")
    monkeypatch.setenv("SKILLS_DIRECTORY", str(tmp_path))
    with pytest.raises(ResourceUnavailable, match="技能声明无效"):
        skill_catalog()


def test_builtin_and_imported_skills_have_valid_tool_and_path_adaptations(monkeypatch):
    monkeypatch.delenv("SKILLS_DIRECTORY", raising=False)
    rows = load_skills()
    assert {row["name"] for row in rows} == {
        "guided-learning", "knowledge-maintenance", "evidence-check",
        "code-documentation", "academic-paper-review",
    }
    for row in rows:
        assert "/mnt/user-data/" not in row["content"]
        assert "`web_fetch`" not in row["content"]
