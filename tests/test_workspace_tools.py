"""在真实 Linux 文件系统验证 SDK 文件工具适配，不请求模型供应商。"""

import os
import time

import pytest

from knowledgeops_agent.harness.sandbox.workspace import (
    WorkspaceBackend,
    build_workspace_middleware,
)
from knowledgeops_agent.harness.skills.resources import FrozenSkillBackend


def backend(tmp_path, **kwargs):
    skills = FrozenSkillBackend({"/skills/demo/SKILL.md": "# 冻结方法\n只读内容"})
    return WorkspaceBackend(tmp_path, skills=skills, **kwargs)


def test_real_files_edit_search_and_frozen_skills(tmp_path):
    store = backend(tmp_path)
    assert store.write("/outputs/笔记.md", "# 标题\n可靠的文件工具").error is None
    assert "可靠" in str(store.read("/outputs/笔记.md"))
    assert store.edit("/outputs/笔记.md", "可靠", "可验证").error is None
    assert "可验证" in (tmp_path / "outputs/笔记.md").read_text()
    assert "笔记.md" in str(store.glob("**/*.md", "/"))
    assert "笔记.md" in str(store.grep("可验证", "/"))
    assert "冻结方法" in str(store.read("/skills/demo/SKILL.md"))
    assert store.write("/skills/demo/SKILL.md", "改写").error


def test_file_boundary_and_managed_knowledge_are_enforced(tmp_path):
    store = backend(tmp_path)
    with pytest.raises(ValueError, match="traversal"):
        store.write("/../escape.txt", "x")
    assert store.write("/knowledge/current/test.md", "旁路发布").error
    assert store.write("/knowledge/documents/可编辑.md", "# 知识工作文件").error is None
    assert "80KiB" in store.write("/outputs/too-large.md", "文" * 28000).error
    assert not (tmp_path / "outputs/too-large.md").exists()
    outside = tmp_path.parent / (tmp_path.name + "-outside.txt")
    outside.write_text("outside")
    (tmp_path / "escape").symlink_to(outside)
    with pytest.raises(ValueError):
        store.read("/escape")
    with pytest.raises(ValueError):
        store.edit("/escape", "outside", "changed")
    assert outside.read_text() == "outside"


def test_shell_environment_timeout_and_windows_visible_result(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-value")
    store = backend(tmp_path, max_timeout=2)
    result = store.execute(
        "printf '%s' \"${OPENAI_API_KEY-unset}\"; printf '完成' > outputs/result.txt"
    )
    assert result.exit_code == 0
    assert result.output == "unset"
    assert (tmp_path / "outputs/result.txt").read_text() == "完成"
    start = time.monotonic()
    result = store.execute("sleep 20", timeout=1)
    assert result.exit_code != 0
    assert time.monotonic() - start < 5
    assert store.execute("true", timeout=3).exit_code != 0


def test_sdk_middleware_has_real_schemas_and_no_duplicate_reader(tmp_path):
    middleware = build_workspace_middleware(
        backend(tmp_path), {"read_file", "ls", "write_file", "edit_file", "glob", "grep", "execute"}
    )
    names = [tool.name for tool in middleware.tools]
    assert sorted(names) == sorted(
        {"read_file", "ls", "write_file", "edit_file", "glob", "grep", "execute"}
    )
    assert "file_path" in next(tool for tool in middleware.tools if tool.name == "write_file").args
    assert "command" in next(tool for tool in middleware.tools if tool.name == "execute").args
    assert (
        "not an isolated security sandbox"
        in next(tool for tool in middleware.tools if tool.name == "execute").description
    )


def test_guard_prevents_execution(tmp_path):
    def stopped():
        raise PermissionError("任务已取消")

    store = backend(tmp_path, guard=stopped)
    with pytest.raises(PermissionError):
        store.execute("touch should-not-exist")
    assert not os.path.exists(tmp_path / "should-not-exist")
