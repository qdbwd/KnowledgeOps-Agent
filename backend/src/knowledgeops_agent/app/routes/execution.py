"""执行权限的用户 API；不能通过工具目录调用，也不保存到用户记忆。"""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from knowledgeops_agent.harness.models.reasoning import ReasoningService
from knowledgeops_agent.harness.permissions.settings import ExecutionSettings, RuntimeSettingsUpdate


class RuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    conversation_id: str | None = Field(default=None, max_length=36)
    argv: list[str] = Field(min_length=1, max_length=128)
    match: Literal["exact", "prefix"] = "exact"
    cwd: str = Field(min_length=1, max_length=4096)
    effect: Literal["allow", "ask", "deny"] = "allow"


class RuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    expected_revision: int = Field(ge=1)
    argv: list[str] | None = Field(default=None, min_length=1, max_length=128)
    match: Literal["exact", "prefix"] | None = None
    cwd: str | None = Field(default=None, min_length=1, max_length=4096)
    effect: Literal["allow", "ask", "deny"] | None = None
    enabled: bool | None = None


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    expected_fingerprint: str = Field(min_length=64, max_length=64)
    approve: bool
    scope: Literal["once", "session", "persistent"] = "once"
    match: Literal["exact", "prefix"] = "exact"


class ReceiptDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    expected_revision: int = Field(ge=1)
    decision: Literal["skip", "retry"]


class FileDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    expected_revision: int = Field(ge=1)
    decision: Literal["skip", "verify"]


execution_router = APIRouter(prefix="/api")


def _settings(request: Request) -> ExecutionSettings:
    services = request.app.state.services
    return ExecutionSettings(services.sessions, jobs=services.jobs)


def _workspace(request: Request) -> str:
    root = request.app.state.services.settings.workspace_root
    if root is None:
        raise ValueError("尚未配置命令工作区")
    return str(Path(root).resolve())


@execution_router.get("/conversations/{conversation_id}/runtime-settings")
def runtime_settings(conversation_id: str, request: Request):
    return _settings(request).get(conversation_id)


@execution_router.patch("/conversations/{conversation_id}/runtime-settings")
def update_runtime_settings(conversation_id: str, body: RuntimeSettingsUpdate, request: Request):
    binding = None
    if body.reasoning_effort is not None:
        services = request.app.state.services
        if services.reasoning is None:
            services.reasoning = ReasoningService(services.settings)
        binding = services.reasoning.freeze(body.reasoning_effort)
    return _settings(request).update(
        conversation_id, reasoning_binding=binding, **body.model_dump(exclude_none=True)
    )


@execution_router.get("/execution-rules")
def execution_rules(request: Request, conversation_id: str | None = None):
    return _settings(request).rules(_workspace(request), conversation_id)


@execution_router.get("/execution-rules/context")
def execution_rule_context(request: Request):
    return {"workspace": _workspace(request), "cwd": _workspace(request)}


@execution_router.post("/execution-rules", status_code=201)
def create_execution_rule(body: RuleInput, request: Request):
    return _settings(request).create_rule(workspace=_workspace(request), **body.model_dump())


@execution_router.patch("/execution-rules/{rule_id}")
def update_execution_rule(rule_id: str, body: RuleUpdate, request: Request):
    return _settings(request).update_rule(
        rule_id, workspace=_workspace(request), **body.model_dump(exclude_none=True)
    )


@execution_router.delete("/execution-rules/{rule_id}", status_code=204)
def delete_execution_rule(rule_id: str, request: Request, expected_revision: int = Query(ge=1)):
    _settings(request).delete_rule(
        rule_id, workspace=_workspace(request), expected_revision=expected_revision
    )
    return Response(status_code=204)


def _context(request: Request, run_id: str):
    from knowledgeops_agent.harness.runtime.agent import RunContext
    from knowledgeops_agent.harness.runtime.resources import ResourceService

    services = request.app.state.services
    run = services.jobs.get_run(run_id)
    snapshot = ResourceService(services.sessions).read(run["root_id"])
    return RunContext(
        run_id=run["root_id"],
        root_id=run["root_id"],
        worker_id="",
        jobs=services.jobs,
        knowledge=services.knowledge,
        sources=services.sources,
        gateway=services.gateway,
        workspace_root=services.settings.workspace_root,
        resources=snapshot["content"] if snapshot else None,
    )


@execution_router.get("/runs/{run_id}/approvals")
def run_approvals(run_id: str, request: Request):
    from knowledgeops_agent.harness.mcp.calls import MCPCalls
    from knowledgeops_agent.harness.permissions.approvals import ExecutionApprovals
    from knowledgeops_agent.harness.permissions.receipts import CommandJournal
    from knowledgeops_agent.harness.sandbox.reconciliation import FileReconciliation

    context = _context(request, run_id)
    return context.gateway._redact(
        {
            "actions": ExecutionApprovals(context).list(),
            "commands": CommandJournal(context).list(),
            "mcp": MCPCalls(context).list(),
            "files": FileReconciliation(context).list(),
        }
    )


@execution_router.post("/runs/{run_id}/approvals/{action_id}/decision")
def decide_action(run_id: str, action_id: str, body: ApprovalDecision, request: Request):
    from knowledgeops_agent.harness.permissions.approvals import ExecutionApprovals

    context = _context(request, run_id)
    return context.gateway._redact(
        ExecutionApprovals(context).decide(action_id, **body.model_dump())
    )


@execution_router.post("/runs/{run_id}/commands/{receipt_id}/resolve")
def resolve_command(run_id: str, receipt_id: str, body: ReceiptDecision, request: Request):
    from knowledgeops_agent.harness.permissions.approvals import queue_after_decisions
    from knowledgeops_agent.harness.permissions.receipts import CommandJournal

    context = _context(request, run_id)
    result = CommandJournal(context).resolve(receipt_id, **body.model_dump())
    queue_after_decisions(context.jobs, context.root_id)
    return context.gateway._redact(result)


@execution_router.post("/runs/{run_id}/files/{receipt_id}/resolve")
def resolve_file(run_id: str, receipt_id: str, body: FileDecision, request: Request):
    from knowledgeops_agent.harness.permissions.approvals import queue_after_decisions
    from knowledgeops_agent.harness.sandbox.reconciliation import FileReconciliation

    context = _context(request, run_id)
    result = FileReconciliation(context).resolve(receipt_id, **body.model_dump())
    queue_after_decisions(context.jobs, context.root_id)
    return result
