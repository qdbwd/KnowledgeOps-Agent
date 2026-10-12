"""验收证据必须覆盖递归技能/适配代码、前端、迁移，且排除密钥和运行数据。"""

from pathlib import Path


def test_evaluation_fingerprint_is_recursive_and_excludes_secrets_and_generated_data(
    tmp_path, monkeypatch
):
    from evaluation.run import fingerprint

    included = [
        "backend/src/knowledgeops_agent/skills/guide/SKILL.md",
        "skills/guide/SKILL.md",
        "config/knowledge-policy.md",
        "config/web-search.yaml",
        "backend/src/knowledgeops_agent/vendor/adapter.py",
        "backend/src/knowledgeops_agent/profiles/deepseek.json",
        "frontend/src/App.tsx",
        "frontend/package-lock.json",
        "migrations/versions/0013_mcp_controls.py",
        "pyproject.toml",
        "uv.lock",
        "compose.yaml",
        "compose.dev.yaml",
        "Makefile",
    ]
    excluded = [
        ".env",
        "artifacts/private.json",
        "frontend/node_modules/library/index.js",
        "backend/src/knowledgeops_agent/__pycache__/data.pyc",
    ]
    for name in included + excluded:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("initial", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    first = fingerprint()
    assert set(included).issubset(first)
    assert not set(excluded).intersection(first)
    Path(included[0]).write_text("changed skill", encoding="utf-8")
    assert fingerprint()[included[0]] != first[included[0]]
