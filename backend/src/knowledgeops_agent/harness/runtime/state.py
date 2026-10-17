"""图状态与运行依赖：仅 State 进入检查点，RunContext 由应用显式注入。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, NotRequired

from langchain.agents import AgentState

from knowledgeops_agent.app.runs.service import RunService
from knowledgeops_agent.harness.models.gateway import ModelGateway
from knowledgeops_agent.knowledge.service import KnowledgeService
from knowledgeops_agent.sources.service import SourceService
from knowledgeops_agent.vendor.deerflow.deferred_tools import merge_promotions
from knowledgeops_agent.vendor.deerflow.task_continuity_state import (
    TaskNotesChannel,
    merge_task_notes,
)


class KnowledgeOpsState(AgentState):
    """检查点保存消息和显式计划；业务草稿/证据独立存储，状态只引用它们。"""

    plan: NotRequired[list[dict[str, str]]]
    working_summary: NotRequired[str]
    context_compaction: NotRequired[dict]
    context_background_cache: NotRequired[dict]
    promoted_tools: Annotated[dict, merge_promotions]
    final_response_feedback: NotRequired[list[str]]
    answer_repair_count: NotRequired[int]
    guidance_seen_sequence: NotRequired[int]
    decision_authority_epoch: NotRequired[int]
    model_step_key: NotRequired[str]
    child_notifications_seen: NotRequired[list[str]]
    child_instruction_seen: NotRequired[int]
    task_notes: Annotated[dict, TaskNotesChannel(dict, merge_task_notes)]
    task_history: NotRequired[dict]


@dataclass
class RunContext:
    """应用为每次运行显式注入依赖；不是框架自动从任意变量收集的资料。"""

    run_id: str
    root_id: str
    worker_id: str
    jobs: RunService
    knowledge: KnowledgeService
    sources: SourceService
    gateway: ModelGateway
    checkpointer: Any = None
    knowledge_policy_metadata: dict[str, str] | None = None
    resources: dict | None = None
    # 每次构图的实际工具视图，包括SDK中间件声明；不是授权或持久业务状态。
    tool_catalog: list[dict] | None = None
    workspace_root: Path | None = None
    workspace_execute_timeout: int = 60
    workspace_output_limit_bytes: int = 10485760

    def __post_init__(self):
        from knowledgeops_agent.learning.budget import reserve_model

        self.gateway.attempt_guard = lambda reservation, root, run: reserve_model(
            self.jobs, reservation, root, run
        )
