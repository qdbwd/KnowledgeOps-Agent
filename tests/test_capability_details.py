"""管理详情来自真实定义；查看不能创建运行、执行脚本或打开外部连接。"""

import json
import os

import pytest
from langchain_core.utils.function_calling import convert_to_openai_function
from sqlalchemy import func, select
from test_api_worker import client as _client
from test_api_worker import services as _services

from knowledgeops_agent.harness.runtime.agent import LOCAL_TOOLS
from knowledgeops_agent.persistence.models import Run, ToolPolicy

client = _client
services = _services


def test_tool_detail_uses_real_definition_without_executing(client, services, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("管理详情不能执行工具")

    monkeypatch.setattr(type(LOCAL_TOOLS["read_source"]), "invoke", forbidden)
    expected = convert_to_openai_function(LOCAL_TOOLS["read_source"])
    result = client.get("/api/tools/detail", params={"tool_id": "builtin:read_source"})
    assert result.status_code == 200, result.text
    assert result.json()["definition"] == expected
    assert result.json()["display_name"] == "读取材料正文"
    assert "runtime" not in result.json()["definition"]["parameters"]["properties"]
    with services.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 0
        assert session.scalar(select(func.count()).select_from(ToolPolicy)) == 0
    assert services.gateway.ledger.records() == []


@pytest.mark.parametrize("name", ["read_file", "execute", "task", "tool_search"])
def test_middleware_details_have_actual_schema(client, name):
    result = client.get("/api/tools/detail", params={"tool_id": f"middleware:{name}"})
    assert result.status_code == 200, result.text
    detail = result.json()
    assert detail["definition"]["name"] == name
    assert detail["definition"]["parameters"]["properties"]
    assert "runtime" not in detail["definition"]["parameters"]["properties"]


def test_unknown_tool_is_not_imported_from_user_input(client):
    assert (
        client.get("/api/tools/detail", params={"tool_id": "builtin:os.system"}).status_code == 404
    )


def make_package(root, name="example"):
    package = root / name
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 使用示例\ncompatibility: 需要 Python\n---\n\n# 完整说明\n保留正文。",
        encoding="utf-8",
    )
    (package / "script.py").write_text("raise RuntimeError('不能执行')", encoding="utf-8")
    (package / "pixel.bin").write_bytes(b"\xff\x00")
    return package


def test_disabled_skill_details_read_full_text_and_resources_without_execution(
    client, tmp_path, monkeypatch
):
    make_package(tmp_path)
    (tmp_path / "availability.json").write_text(
        json.dumps({"version": 1, "disabled": {"example": "缺少外部服务"}}), encoding="utf-8"
    )
    monkeypatch.setenv("SKILLS_DIRECTORY", str(tmp_path))
    result = client.get("/api/skills/example")
    assert result.status_code == 200
    detail = result.json()
    assert "完整说明" in detail["content"]
    assert detail["compatibility"] == "需要 Python"
    assert detail["enabled"] is False
    assert {item["path"] for item in detail["resources"]} == {"SKILL.md", "script.py", "pixel.bin"}
    assert all("content" not in item for item in detail["resources"])
    text = client.get("/api/skills/example/resource", params={"path": "script.py"})
    assert text.status_code == 200
    assert text.json()["content"].startswith("raise RuntimeError")
    binary = client.get("/api/skills/example/resource", params={"path": "pixel.bin"})
    assert binary.json()["binary"] is True
    assert binary.json()["content"] is None
    assert (
        client.get("/api/skills/example/resource", params={"path": "../outside.txt"}).status_code
        == 404
    )
    assert client.get("/api/skills/unknown").status_code == 404


def test_skill_linked_resource_fails_closed(client, tmp_path, monkeypatch):
    package = make_package(tmp_path / "skills")
    outside = tmp_path / "secret.txt"
    outside.write_text("hidden")
    (package / "linked.txt").symlink_to(outside)
    monkeypatch.setenv("SKILLS_DIRECTORY", str(tmp_path / "skills"))
    response = client.get("/api/skills/example/resource", params={"path": "linked.txt"})
    assert response.status_code == 503
    assert "hidden" not in response.text


def test_skill_special_file_is_rejected_without_opening(client, tmp_path, monkeypatch):
    package = make_package(tmp_path)
    os.mkfifo(package / "blocking-pipe")
    monkeypatch.setenv("SKILLS_DIRECTORY", str(tmp_path))
    response = client.get("/api/skills/example")
    assert response.status_code == 503
