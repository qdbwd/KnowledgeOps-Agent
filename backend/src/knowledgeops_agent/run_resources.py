"""根任务首次执行冻结可信配置；恢复保留版本，撤回在下一次调用前生效。

持久化的是普通JSON资源内容。模型/数据库/MCP客户端按运行重建，密钥来自
环境而不写入清单。子任务共享root资源，工具权限仍由装配阶段单独缩小。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from sqlalchemy import select

from knowledgeops_agent import knowledge_policy
from knowledgeops_agent.config_resources import bundled_resource, resource_path
from knowledgeops_agent.db import ResourceRevocation, Run, RunEvent, RunResourceSnapshot
from knowledgeops_agent.knowledge_policy import KnowledgePolicy, load_knowledge_policy
from knowledgeops_agent.model_gateway import ModelGateway
from knowledgeops_agent.prompt_assembly import RETRIEVAL_GUIDANCE, SYSTEM_TEMPLATE
from knowledgeops_agent.request_capacity import ModelCapacity
from knowledgeops_agent.web_search_config import load_web_search_config

SKILLS_PATH = bundled_resource("skills")
FORMAT_VERSION = 2


class ResourceUnavailable(RuntimeError):
    """需要停止或返回明确不可用；不得改用未冻结的资源猜测恢复。"""


def content_hash(value: dict | list) -> str:
    """固定JSON规范用于完整性校验，不将哈希本身当权限或真实性证明。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def load_skills() -> list[dict]:
    """读取部署指定的技能目录并冻结文本；不执行技能，也不自动扫描用户目录。"""
    result = []
    root = resource_path("SKILLS_DIRECTORY", SKILLS_PATH)
    if not root.is_dir():
        raise ResourceUnavailable("项目技能目录缺失")
    # 根 README、许可和维护说明不充当 Agent 技能；每个直接子目录是一份技能。
    paths = []
    for directory in sorted(root.iterdir()):
        if directory.is_symlink():
            raise ResourceUnavailable("技能目录不能使用符号链接")
        if directory.is_dir() and (directory / "SKILL.md").is_file():
            paths.extend(sorted(directory.rglob("*.md")))
    names = set()
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ResourceUnavailable("技能资源必须在声明的技能目录内")
        raw = path.read_bytes()
        content = raw.decode("utf-8")
        resource = "/skills/" + path.relative_to(root).as_posix()
        if path.name == "SKILL.md":
            # 格式与解析由上游进一步验证；这里只读取受控资源声明，不执行内容。
            import yaml

            parts = content.split("---", 2)
            try:
                meta = yaml.safe_load(parts[1]) if len(parts) == 3 and not parts[0].strip() else {}
            except yaml.YAMLError as error:
                raise ResourceUnavailable(f"技能声明无效：{resource}") from error
            if (
                not isinstance(meta, dict)
                or not isinstance(meta.get("name"), str)
                or not meta["name"].strip()
                or not isinstance(meta.get("description"), str)
                or not meta["description"].strip()
                or not isinstance(meta.get("metadata", {}), dict)
            ):
                raise ResourceUnavailable(f"技能声明无效：{resource}")
            if meta["name"] != path.parent.name or meta["name"] in names:
                raise ResourceUnavailable(f"技能名称必须唯一且与目录名一致：{resource}")
            names.add(meta["name"])
            # 上游通用 SKILL.md 可以没有版本；内容哈希仍保证恢复使用原文。
            version = str(meta.get("metadata", {}).get("version") or hashlib.sha256(raw).hexdigest()[:12])
        else:
            meta, version = {}, "resource-v1"
        result.append(
            {
                "resource": resource,
                "name": meta.get("name", path.stem),
                "version": version,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "content": content,
            }
        )
    if not result:
        raise ResourceUnavailable("项目技能目录为空")
    return result


def skill_catalog() -> list[dict]:
    """管理页面只展示声明，读取目录不等于授予工具权限或创建模型请求。"""
    import yaml

    return [
        {
            "name": item["name"],
            "description": yaml.safe_load(item["content"].split("---", 2)[1])["description"],
            "version": item["version"],
            "resource": item["resource"],
        }
        for item in load_skills()
        if item["resource"].endswith("/SKILL.md")
    ]


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
        "adapter": "knowledgeops-deepseek-v2",
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
                from knowledgeops_agent.auxiliary_model import ISOLATED_INSTRUCTION
                from knowledgeops_agent.context_management import SUMMARY_PROMPT
                from knowledgeops_agent.db import LearningTask
                from knowledgeops_agent.learning_profiles import LearningProfileService
                from knowledgeops_agent.learning_tasks import task_view
                from knowledgeops_agent.mcp_registry import active_connections
                from knowledgeops_agent.memory_retrieval import freeze as freeze_memory
                from knowledgeops_agent.publication_intent import INTENT_PROMPT
                from knowledgeops_agent.runtime import SYSTEM_PROMPT
                from knowledgeops_agent.subagent_registry import active_definitions
                from knowledgeops_agent.tool_policy import read_policy

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
                manifest: dict = {
                    "format_version": FORMAT_VERSION,
                    "policy": asdict(policy),
                    "skills": load_skills(),
                    "prompts": {
                        "assembly_template": SYSTEM_TEMPLATE,
                        "retrieval": RETRIEVAL_GUIDANCE,
                        "system": SYSTEM_PROMPT,
                        "intent": INTENT_PROMPT,
                        "summary": SUMMARY_PROMPT,
                        "auxiliary": ISOLATED_INSTRUCTION,
                        "learning": Path(__file__)
                        .with_name("prompts")
                        .joinpath("learning-modes.md")
                        .read_text(encoding="utf-8"),
                    },
                    "model": {
                        "provider": model_reference(context.gateway),
                        "capacity": context.gateway.capacity.model_dump(mode="json"),
                    },
                    # 旧快照继续读取原preferences；新快照保存有界背景及只读版本目录。
                    "preferences": memory["selected"],
                    "memory": memory,
                    "mcp": active_connections(session),
                    "subagents": active_definitions(session),
                    "tool_policy": read_policy(session),
                    "web_search": load_web_search_config().model_dump(mode="json"),
                }
                if learning:
                    manifest["learning"] = learning
                    # 同一条记忆已经进入配置自动部分时，不重复注入另一份背景。
                    used_ids = set(learning["profiles"]["memory_ids"])
                    manifest["preferences"] = [
                        p for p in memory["selected"] if p["id"] not in used_ids
                    ]
                if getattr(context, "workspace_root", None) is not None:
                    manifest["workspace"] = {
                        "root": str(context.workspace_root.resolve()),
                        "execute_timeout": context.workspace_execute_timeout,
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
        from knowledgeops_agent.subagent_runtime import selected_definition

        selected_definition(context, manifest)
        with self.sessions() as session:
            run = session.get(Run, context.root_id)
            delegated_unit = bool(run and run.learning_unit_id)
            from knowledgeops_agent.db import LearningTask

            task = session.get(LearningTask, run.learning_task_id) if delegated_unit else None
            automatic_publication = bool(task and task.auto_publish)
        if delegated_unit:
            from knowledgeops_agent.subagent_registry import SubagentRegistry
            from knowledgeops_agent.tool_policy import ToolPolicyService

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
                    .where(Run.root_id == context.root_id, RunEvent.kind == "skill_read")
                )
            }
        for path in used:
            if path and not self.skill_available(manifest, path):
                raise ResourceUnavailable(
                    "本任务已读取的技能已撤回或不可用；请以新任务继续，历史保留。"
                )
        if manifest["model"]["provider"] != model_reference(context.gateway):
            raise ResourceUnavailable("恢复时模型通道或配置已变化，请以新任务继续，不能静默换用")

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
        local = SKILLS_PATH.joinpath(*relative.parts)
        return local.is_file() and not self.revoked("skill", path, item["sha256"])


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
