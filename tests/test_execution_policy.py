"""命令授权的边界合同：不是语义分类器，也不把审批当作文件隔离。"""

import pytest

from knowledgeops_agent.harness.permissions.policy import evaluate_execution
from knowledgeops_agent.harness.permissions.rules import (
    command_argv,
    reusable_command,
    rule_matches,
)


@pytest.mark.parametrize(
    "command",
    [
        "git status && touch x",
        "git status; touch x",
        "git status\ntouch x",
        "echo $(touch x)",
        "echo `touch x`",
        "cat $HOME/x",
        "cat ~/x",
        "cat *.txt",
        "cat <(touch x)",
        "echo yes > out",
        "A=1 git status",
        "git status &",
        "git status | cat",
        "echo {a,b}",
        "git status # trailing comment",
    ],
)
def test_shell_composition_never_acquires_reusable_rule(command):
    assert reusable_command(command) is None


def test_static_quoting_and_argument_boundaries():
    assert command_argv("printf '%s' 'hello world'") == ["printf", "%s", "hello world"]
    assert command_argv("echo '$HOME'") == ["echo", "$HOME"]
    rule = {"argv": ["git", "status"], "match": "prefix", "cwd": "/data/ws"}
    assert rule_matches(rule, "git status --short", cwd="/data/ws")
    assert not rule_matches(rule, "git status-other", cwd="/data/ws")
    assert not rule_matches(rule, "git status && rm x", cwd="/data/ws")
    assert not rule_matches(rule, "git status", cwd="/data/other")
    assert not rule_matches({**rule, "match": "exact"}, "git status --short", cwd="/data/ws")


@pytest.mark.parametrize(
    "command",
    ["bash -c 'touch x'", "python -c 'print(1)'", "node x.js", "env bash x.sh", "./work/task.sh"],
)
def test_interpreters_and_mutable_workspace_scripts_only_offer_once(command):
    assert reusable_command(command) is None


def test_default_matrix_and_permission_upper_bounds():
    assert evaluate_execution("middleware:execute").decision == "ask"
    for tool in (
        "middleware:read_file",
        "builtin:search_web",
        "builtin:publish_changes",
    ):
        assert evaluate_execution(tool).decision == "allow"
    for tool in ("middleware:write_file", "middleware:edit_file", "middleware:delete"):
        assert evaluate_execution(tool).decision == "ask"
        assert evaluate_execution(tool, mode="auto_review").decision == "review"
        assert evaluate_execution(tool, mode="auto_review", available=False).decision == "deny"
    assert evaluate_execution("middleware:execute", mode="auto").decision == "ask"
    assert evaluate_execution("mcp:server/tool", mode="auto_review", mcp_permission="confirm").decision == "review"
    assert evaluate_execution("mcp:server/tool", mode="auto_review", mcp_permission="deny").decision == "deny"
    assert evaluate_execution("middleware:execute", mode="auto", available=False).decision == "deny"
    assert (
        evaluate_execution("mcp:server/tool", mode="auto", mcp_permission="confirm").decision
        == "ask"
    )
    assert (
        evaluate_execution("mcp:server/tool", mode="auto", mcp_permission="deny").decision == "deny"
    )
    assert evaluate_execution("unknown:tool", mode="auto").decision == "deny"


def test_explicit_deny_and_ask_override_auto_and_allow_rules():
    base = {"argv": ["git", "status"], "match": "prefix", "cwd": "/data/ws", "enabled": True}

    def result(effects):
        return evaluate_execution(
            "middleware:execute",
            mode="auto_review",
            command="git status --short",
            cwd="/data/ws",
            rules=[{**base, "effect": effect} for effect in effects],
        ).decision

    assert result(["allow", "deny"]) == "deny"
    assert result(["allow", "ask"]) == "ask"
    assert result(["allow"]) == "allow"
