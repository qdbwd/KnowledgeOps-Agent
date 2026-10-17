"""根任务首次执行冻结可信配置；恢复保留版本，撤回在下一次调用前生效。

持久化的是普通JSON资源内容，不保存活的客户端。MCP连接由worker按聊天/
子Run复用，密钥来自环境而不写入清单。子任务共享root资源，工具权限仍由
装配阶段单独缩小。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from sqlalchemy import select

from knowledgeops_agent.core.resources import bundled_resource, package_resource, resource_path
from knowledgeops_agent.harness.context.assembly import RETRIEVAL_GUIDANCE, SYSTEM_TEMPLATE
from knowledgeops_agent.harness.context.capacity import ModelCapacity
from knowledgeops_agent.harness.models.gateway import ModelGateway
from knowledgeops_agent.harness.skills.packages import (
    SkillPackageError,
    disabled_skills,
    load_package_resources,
    package_catalog,
    scan_packages,
)
from knowledgeops_agent.knowledge import policy as knowledge_policy
from knowledgeops_agent.knowledge.policy import KnowledgePolicy, load_knowledge_policy
from knowledgeops_agent.persistence.models import (
    ResourceRevocation,
    Run,
    RunEvent,
    RunResourceSnapshot,
)
from knowledgeops_agent.sources.search_config import load_web_search_config

SKILLS_PATH = bundled_resource("skills")
FORMAT_VERSION = 2


class ResourceUnavailable(RuntimeError):
    """需要停止或返回明确不可用；不得改用未冻结的资源猜测恢复。"""


def content_hash(value: dict | list) -> str:
    """固定JSON规范用于完整性校验，不将哈希本身当权限或真实性证明。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def load_skills(tool_policy: dict | None = None) -> list[dict]:
    """冻结已启用技能的完整资源；发现时不运行脚本、不安装依赖。"""
    try:
        enabled = (tool_policy or {}).get("skill_enabled", {})
        return load_package_resources(
            resource_path("SKILLS_DIRECTORY", SKILLS_PATH),
            excluded={name for name, value in enabled.items() if not value},
        )
    except SkillPackageError as error:
        raise ResourceUnavailable(str(error)) from error


def skill_catalog() -> list[dict]:
    """管理页面只展示声明，读取目录不等于授予工具权限或创建模型请求。"""
    try:
        return package_catalog(resource_path("SKILLS_DIRECTORY", SKILLS_PATH))
    except SkillPackageError as error:
        raise ResourceUnavailable(str(error)) from error


def model_reference(gateway: ModelGateway) -> dict:
    """只白名单保存非秘密的模型身份及生成配置；不序列化SDK对象。"""
    client = gateway.client
    endpoint = str(getattr(client, "openai_api_base", "") or "")
    parsed = urlsplit(endpoint)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ResourceUnavailable("模型端点不能包含凭据或查询参数")
    return {
        "model": getattr(client, "model_name", gateway.capacity.model),
        "endpoint": endpoint or gateway.capacity.endpoint_host,
        "adapter": (
            "knowledgeops-openai-chat-v1"
            if type(client).__name__ == "ChatOpenAI"
            else "knowledgeops-deepseek-v2"
        ),
        "temperature": getattr(client, "temperature", None),
        "top_p": getattr(client, "top_p", None),
    }


class ResourceService:
    """使用root行锁确保重复领取只冻结一次；只在首次冻结读取完整文件内容。"""

    def __init__(self, sessions):
        self.sessions = sessions

    def ensure(self, context) -> dict:
        with self.sessions.begin() as session:
            root = session.scalar(select(Run).where(Run.id == context.root_id).with_for_update())
            if root is None:
                raise KeyError("root not found")
            if root.format_version != FORMAT_VERSION:
                raise ResourceUnavailable(
                    "旧格式运行缺少可信资源快照；请保留成果并作为新任务继续。"
                )
            snapshot = session.get(RunResourceSnapshot, root.id)
            if snapshot is None:
                if context.run_id != context.root_id:
                    raise ResourceUnavailable("子任务不能替根任务构造缺失的资源清单")
                try:
                    policy = load_knowledge_policy()
                except knowledge_policy.KnowledgePolicyError as error:
                    raise ResourceUnavailable(str(error)) from error
                from knowledgeops_agent.harness.context.management import SUMMARY_PROMPT
                from knowledgeops_agent.harness.mcp.registry import active_connections
                from knowledgeops_agent.harness.models.auxiliary import ISOLATED_INSTRUCTION
                from knowledgeops_agent.harness.runtime.agent import SYSTEM_PROMPT
                from knowledgeops_agent.harness.subagents.registry import active_definitions
                from knowledgeops_agent.harness.tools.policy import read_policy
                from knowledgeops_agent.knowledge.intent import INTENT_PROMPT
                from knowledgeops_agent.learning.profiles import LearningProfileService
                from knowledgeops_agent.learning.tasks import task_view
                from knowledgeops_agent.memory.retrieval import freeze as freeze_memory
                from knowledgeops_agent.persistence.models import LearningTask

                learning_task = (
                    session.get(LearningTask, root.learning_task_id)
                    if root.learning_task_id
                    else None
                )
                topic = (
                    learning_task.topic
                    if learning_task
                    else root.input_data.get("memory_topic", "")
                )
                memory = freeze_memory(session, root.task[:500], topic=topic)
                learning = None
                if learning_task:
                    learning = {
                        "task": task_view(session, learning_task),
                        "profiles": LearningProfileService(self.sessions).freeze(
                            task_id=learning_task.id, query=root.task[:500], memory=memory
                        ),
                    }
                tool_policy = read_policy(session)
                skill_catalog_rows, skill_resources = scan_packages(
                    resource_path("SKILLS_DIRECTORY", SKILLS_PATH),
                    excluded={name for name, enabled in tool_policy.get("skill_enabled", {}).items() if not enabled},
                )
                manifest: dict = {
                    "format_version": FORMAT_VERSION,
                    "policy": asdict(policy),
                    "skills": skill_resources,
                    "prompts": {
                        "assembly_template": SYSTEM_TEMPLATE,
                        "retrieval": RETRIEVAL_GUIDANCE,
                        "system": SYSTEM_PROMPT,
                        "intent": INTENT_PROMPT,
                        "summary": SUMMARY_PROMPT,
                        "auxiliary": ISOLATED_INSTRUCTION,
                        "learning": package_resource("prompts/learning-modes.md").read_text(
                            encoding="utf-8"
                        ),
                    },
                    "model": {
                        "provider": model_reference(context.gateway),
                        "capacity": context.gateway.capacity.model_dump(mode="json"),
                    },
                    # 旧快照继续读取原preferences；新快照保存有界背景及只读版本目录。
                    "preferences": memory["selected"],
                    "memory": memory,
                    "mcp": active_connections(session),
                    # 仅新根任务开放角色只读继承，旧冻结清单不得因升级扩大能力。
                    "child_mcp": 1,
                    "subagents": active_definitions(session),
                    "tool_policy": tool_policy,
                    "web_search": load_web_search_config().model_dump(mode="json"),
                }
                diagnostics = [
                    row for row in skill_catalog_rows
                    if row["availability_status"] in {"invalid", "config_error"}
                ]
                if diagnostics:
                    manifest["skill_diagnostics"] = diagnostics
                if learning:
                    manifest["learning"] = learning
                    # 同一条记忆已经进入配置自动部分时，不重复注入另一份背景。
                    used_ids = set(learning["profiles"]["memory_ids"])
                    manifest["preferences"] = [
                        p for p in memory["selected"] if p["id"] not in used_ids
                    ]
                if root.input_data.get("reasoning"):
                    manifest["model"]["reasoning"] = root.input_data["reasoning"]
                if getattr(context, "workspace_root", None) is not None:
                    manifest["workspace"] = {
                        "root": str(context.workspace_root.resolve()),
                        "execute_timeout": context.workspace_execute_timeout,
                        "output_limit_bytes": context.workspace_output_limit_bytes,
                        "format": "local-workspace-v1",
                    }
                snapshot = RunResourceSnapshot(
                    root_id=root.id, content=manifest, content_hash=content_hash(manifest)
                )
                session.add(snapshot)
                context.jobs._event(
                    session,
                    root,
                    "resources_frozen",
                    {
                        "content_hash": snapshot.content_hash,
                        "format_version": FORMAT_VERSION,
                        "policy": policy.metadata,
                        "skills": [
                            {k: v for k, v in item.items() if k != "content"}
                            for item in manifest["skills"]
                        ],
                    },
                )
            if content_hash(snapshot.content) != snapshot.content_hash:
                raise ResourceUnavailable("运行资源快照哈希不匹配，不能猜测恢复")
            # 与ORM对象断开引用，调用方不会意外修改已保存的清单。
            manifest = json.loads(json.dumps(snapshot.content))
        self.validate(context, manifest)
        return manifest

    def validate(self, context, manifest: dict) -> None:
        """核心资源不可用时停止；普通正文编辑不改变已冻结内容。"""
        if manifest.get("format_version") != FORMAT_VERSION:
            raise ResourceUnavailable("资源格式不兼容，请以新任务继续")
        if context.run_id == context.root_id and manifest.get("workspace"):
            root = getattr(context, "workspace_root", None)
            if root is None or str(root.resolve()) != manifest["workspace"]["root"]:
                raise ResourceUnavailable("本运行绑定的文件工作区已变化，请以新任务继续")
        if not knowledge_policy._POLICY_PATH.is_file():
            raise ResourceUnavailable("KnowledgeOps 核心领域规则已不可用")
        from knowledgeops_agent.harness.subagents.runtime import selected_definition

        selected_definition(context, manifest)
        with self.sessions() as session:
            run = session.get(Run, context.root_id)
            delegated_unit = bool(run and run.learning_unit_id)
            from knowledgeops_agent.persistence.models import LearningTask

            task = session.get(LearningTask, run.learning_task_id) if delegated_unit else None
            automatic_publication = bool(task and task.auto_publish)
        if delegated_unit:
            from knowledgeops_agent.harness.subagents.registry import SubagentRegistry
            from knowledgeops_agent.harness.tools.policy import ToolPolicyService

            policy_service = ToolPolicyService(self.sessions)
            allowed = policy_service.allowed_ids(manifest["tool_policy"], is_root=True)
            child_allowed = policy_service.allowed_ids(manifest["tool_policy"], is_root=False)
            if automatic_publication and "builtin:publish_changes" not in allowed:
                raise ResourceUnavailable("委托自动保存需要的发布工具已停用，请用户处理后继续")
            if (
                not {
                    "builtin:learning_feedback",
                    "builtin:prepare_changes",
                    "builtin:revise_changeset",
                }
                <= allowed
                or "builtin:read_learning_draft" not in child_allowed
            ):
                raise ResourceUnavailable("委托整理需要的草稿/协作工具已停用，请用户处理后继续")
            for role_id in ("learning-reader", "learning-reviewer"):
                definition = next((d for d in manifest["subagents"] if d["id"] == role_id), None)
                if definition is None:
                    raise ResourceUnavailable("委托整理的读者或审读角色未启用，请用户处理后继续")
                if "read_learning_draft" not in definition.get(
                    "resolved_tools", definition["tools"]
                ):
                    raise ResourceUnavailable(
                        "读者或审读角色缺少固定草稿读取工具，请用户调整后继续"
                    )
                try:
                    SubagentRegistry(self.sessions).validate_binding(definition)
                except (ValueError, PermissionError) as exc:
                    raise ResourceUnavailable(str(exc)) from None
        policy = manifest["policy"]
        if self.revoked("policy", policy["resource"], policy["sha256"]):
            raise ResourceUnavailable("当前任务使用的核心规则版本已撤回")
        # 已读内容可能仍在历史/摘要里；仅让下一次read_file失败不足以撤回影响。
        # 保留历史但停止继续调用模型，由用户以新任务继续，不能假装已从模型记忆擦除。
        with self.sessions() as session:
            used = {
                event.data.get("path")
                for event in session.scalars(
                    select(RunEvent)
                    .join(Run, Run.id == RunEvent.run_id)
                    .where(
                        Run.root_id == context.root_id,
                        RunEvent.kind.in_(["skill_read", "skill_materialized"]),
                    )
                )
            }
        for path in used:
            if path and not self.skill_available(manifest, path):
                raise ResourceUnavailable(
                    "本任务已读取的技能已撤回或不可用；请以新任务继续，历史保留。"
                )
        if manifest["model"]["provider"] != model_reference(context.gateway):
            raise ResourceUnavailable("恢复时模型通道或配置已变化，请以新任务继续，不能静默换用")
        reasoning = manifest["model"].get("reasoning")
        if reasoning:
            provider = manifest["model"]["provider"]
            endpoint_host = urlsplit(provider["endpoint"]).hostname or provider["endpoint"]
            expected_adapter = (
                "knowledgeops-deepseek-v2"
                if reasoning["provider"] == "deepseek"
                else "knowledgeops-openai-chat-v1"
            )
            if (reasoning["model"], reasoning["endpoint_host"], expected_adapter) != (
                provider["model"],
                endpoint_host,
                provider["adapter"],
            ):
                raise ResourceUnavailable("发送时的思考配置与当前模型通道不一致，请新建任务")

    def revoked(self, kind: str, resource: str, sha256: str) -> bool:
        with self.sessions() as session:
            return session.get(ResourceRevocation, (kind, resource, sha256)) is not None

    def read(self, root_id: str) -> dict | None:
        """运行审查读取已有清单，GET不会替旧运行创建缺失的历史。"""
        with self.sessions() as session:
            snapshot = session.get(RunResourceSnapshot, root_id)
            if snapshot is None:
                return None
            if content_hash(snapshot.content) != snapshot.content_hash:
                raise ResourceUnavailable("运行资源快照哈希不匹配")
            return {"content_hash": snapshot.content_hash, "content": snapshot.content}

    def skill_available(self, manifest: dict, path: str) -> bool:
        """按冻结清单找文件，删掉或撤回的可选技能不能继续读取，不回退到宿主路径。"""
        item = next((row for row in manifest["skills"] if row["resource"] == path), None)
        if item is None:
            return False
        relative = PurePosixPath(path).relative_to("/skills")
        from knowledgeops_agent.harness.tools.policy import read_policy

        with self.sessions() as session:
            current = read_policy(session)
        name = relative.parts[0]
        frozen = manifest.get("tool_policy", {})
        if not current.get("skill_enabled", {}).get(name, True) or frozen.get(
            "skill_epochs", {}
        ).get(name, 0) != current.get("skill_epochs", {}).get(name, 0):
            return False
        root = resource_path("SKILLS_DIRECTORY", SKILLS_PATH)
        try:
            if relative.parts[0] in disabled_skills(root):
                return False
        except (SkillPackageError, OSError):
            # 已读过的技能会由 validate 明确停止；普通任务不因全局配置坏而崩溃。
            return False
        local = root.joinpath(*relative.parts)
        entry_path = f"/skills/{relative.parts[0]}/SKILL.md"
        entry = next((row for row in manifest["skills"] if row["resource"] == entry_path), None)
        return bool(
            entry
            and local.is_file()
            and not local.is_symlink()
            and local.resolve().is_relative_to(root.resolve())
            and (root / relative.parts[0] / "SKILL.md").is_file()
            and not self.revoked("skill", entry_path, entry["sha256"])
            and not self.revoked("skill", path, item["sha256"])
        )


def prepare_resources(context) -> dict:
    """在意图解释之前装配，并给本运行一个独立网关视图，避免污染其他run配置。"""
    service = ResourceService(context.jobs.sessions)
    manifest = service.ensure(context)
    context.resources = manifest
    context.knowledge_policy_metadata = KnowledgePolicy(**manifest["policy"]).metadata
    current = context.gateway
    context.gateway = ModelGateway(
        current.ledger,
        enabled=current.enabled,
        client=current.client,
        sensitive_values=current.sensitive_values,
        capacity=ModelCapacity.from_snapshot(manifest["model"]["capacity"]),
    )
    context.gateway.attempt_guard = current.attempt_guard
    return manifest
