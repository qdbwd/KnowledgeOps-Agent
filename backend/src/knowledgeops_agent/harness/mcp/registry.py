"""MCP管理入口：PG是唯一配置源，发现目录与用户审查许可分别记录。

协议复用官方适配。普通配置读取不连接外部服务，配置中只保存密钥引用；
发现失败只反馈异常种类，底层异常可能包含HTTP凭据，不直接回传。
"""

from __future__ import annotations

import asyncio
import builtins
import json
import os
from pathlib import PurePosixPath

from sqlalchemy import select

from knowledgeops_agent.harness.mcp.transport import MCPConnectionConfig, connected_tools
from knowledgeops_agent.harness.runtime.resources import content_hash
from knowledgeops_agent.persistence.models import MCPConnection, MCPConnectionRevision


def configuration(value: dict) -> dict:
    """严格字段+环境引用；stdio只能启动已安装入口，不执行临时shell或安装器。"""
    config = MCPConnectionConfig.model_validate(value)
    if config.transport == "stdio":
        command = PurePosixPath(config.command or "").name.lower()
        if command in {
            "sh",
            "bash",
            "zsh",
            "cmd",
            "cmd.exe",
            "powershell",
            "pwsh",
            "uvx",
            "npx",
            "pip",
            "pip3",
        }:
            raise ValueError("MCP需要预安装的服务入口，不能使用shell或自动安装器")
        if any(argument in {"-c", "-e", "--eval", "--command"} for argument in config.args):
            raise ValueError("MCP不接受内联脚本，请配置预安装的入口文件")
    result = config.model_dump(mode="json")
    # 缺省保持旧配置 JSON 身份，避免仅保存旧配置就误判为连接变更而撤回许可。
    if config.protocol_mode == "auto":
        result.pop("protocol_mode")
    return result


def redact(value, config: dict):
    """按此连接的引用值递归脱敏；不把实际值保存为另一份配置或版本。"""
    secrets = [
        os.environ.get(name, "")
        for name in [
            *config.get("environment_refs", {}).values(),
            *config.get("header_refs", {}).values(),
        ]
    ]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {key: redact(item, config) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, config) for item in value]
    return value


def descriptor(tool) -> dict:
    """冻结输入结构、说明与服务提示；hint只供用户审查，不转成自动许可。"""
    schema = tool.args_schema
    if not isinstance(schema, dict):
        schema = tool.get_input_schema().model_json_schema()
    if "runtime" in schema.get("properties", {}):
        raise ValueError("MCP工具参数runtime与框架注入字段冲突")
    data = {"name": tool.name, "description": tool.description, "schema": schema}
    annotations = (tool.metadata or {}).get("mcp", {}).get("tool", {}).get("annotations", {})
    data["annotations"] = annotations
    return {**data, "schema_hash": content_hash(data)}


def connection_view(row: MCPConnection) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "revision": row.revision,
        "authorization_epoch": row.authorization_epoch,
        "enabled": row.enabled,
        "deleted": row.deleted,
        "config": row.config,
        "tools": row.tools,
    }


def active_connections(session) -> list[dict]:
    return [
        connection_view(row)
        for row in session.scalars(
            select(MCPConnection)
            .where(MCPConnection.enabled.is_(True), MCPConnection.deleted.is_(False))
            .order_by(MCPConnection.created_at, MCPConnection.id)
        )
    ]


class MCPRegistry:
    def __init__(self, sessions):
        self.sessions = sessions

    @staticmethod
    def _save(session, row):
        session.flush()
        session.add(
            MCPConnectionRevision(
                connection_id=row.id, revision=row.revision, content=connection_view(row)
            )
        )

    def list(self) -> builtins.list[dict]:
        with self.sessions() as session:
            return [
                connection_view(row)
                for row in session.scalars(
                    select(MCPConnection)
                    .where(MCPConnection.deleted.is_(False))
                    .order_by(MCPConnection.created_at)
                )
            ]

    def snapshot(self) -> builtins.list[dict]:
        with self.sessions() as session:
            return active_connections(session)

    def get(self, identifier: str) -> dict:
        with self.sessions() as session:
            row = session.get(MCPConnection, identifier)
            if row is None or row.deleted:
                raise KeyError("MCP连接不存在")
            return connection_view(row)

    def create(self, name: str, config: dict) -> dict:
        if not name.strip() or len(name) > 200:
            raise ValueError("连接名称需为1至200字")
        config = configuration(config)
        with self.sessions.begin() as session:
            row = MCPConnection(name=name.strip(), config=config)
            session.add(row)
            self._save(session, row)
            return connection_view(row)

    def update(
        self,
        identifier: str,
        *,
        expected_revision: int,
        name: str | None = None,
        config: dict | None = None,
        enabled: bool | None = None,
        permissions: dict[str, str] | None = None,
        deleted: bool = False,
        discovered: builtins.list[dict] | None = None,
    ) -> dict:
        """所有变更带修订号；权限减小使旧绑定永久失效，重新启用不撤销这道屏障。"""
        with self.sessions.begin() as session:
            row = session.scalar(
                select(MCPConnection).where(MCPConnection.id == identifier).with_for_update()
            )
            if row is None or row.deleted:
                raise KeyError("MCP连接不存在")
            if row.revision != expected_revision:
                raise ValueError("MCP配置修订已变化，请刷新")
            previous = {item["name"]: item for item in row.tools}
            revoke = deleted or (row.enabled and enabled is False)
            if name is not None:
                if not name.strip() or len(name) > 200:
                    raise ValueError("连接名称需为1至200字")
                row.name = name.strip()
            if config is not None:
                updated = configuration(config)
                if updated != row.config:
                    row.config, row.tools, row.enabled = updated, [], False
                    previous, revoke = {}, True
            catalog = (
                row.tools
                if discovered is None
                else [
                    {
                        **item,
                        "permission": previous[item["name"]]["permission"]
                        if item["name"] in previous
                        and previous[item["name"]]["schema_hash"] == item["schema_hash"]
                        else "deny",
                    }
                    for item in discovered
                ]
            )
            by_name = {item["name"]: dict(item) for item in catalog}
            for tool_name, permission in (permissions or {}).items():
                if tool_name not in by_name or permission not in {"deny", "read", "confirm"}:
                    raise ValueError("工具未发现或许可类型无效")
                by_name[tool_name]["permission"] = permission
            for tool_name, old in previous.items():
                current = by_name.get(tool_name, {})
                if old["permission"] != "deny" and (
                    current.get("permission") == "deny"
                    or not current
                    or current.get("schema_hash") != old["schema_hash"]
                    or (old["permission"] == "read" and current.get("permission") == "confirm")
                ):
                    revoke = True
            row.tools = list(by_name.values())
            if enabled is not None:
                row.enabled = enabled
            if deleted:
                row.deleted, row.enabled = True, False
            if revoke:
                row.authorization_epoch += 1
            row.revision += 1
            self._save(session, row)
            return connection_view(row)

    def discover(self, identifier: str, *, expected_revision: int) -> dict:
        """发现通过真实连接，但不调用业务工具；慢网络不持有配置行锁。"""
        row = self.get(identifier)
        if row["revision"] != expected_revision:
            raise ValueError("MCP配置修订已变化，请刷新")

        async def collect():
            async with connected_tools(
                MCPConnectionConfig.model_validate(row["config"]), secrets=os.environ
            ) as tools:
                return [descriptor(tool) for tool in tools]

        try:
            catalog = asyncio.run(
                asyncio.wait_for(collect(), timeout=row["config"]["timeout_seconds"])
            )
        except Exception as error:
            raise ValueError(
                f"MCP连接或发现失败（{type(error).__name__}）；请检查服务入口与环境引用"
            ) from None
        if len(catalog) > 200 or len(json.dumps(catalog)) > 2_000_000:
            raise ValueError("MCP工具目录过大；请在服务端缩小暴露范围")
        if redact(catalog, row["config"]) != catalog:
            raise ValueError("MCP工具目录含凭据，已拒绝保存")
        return self.update(identifier, expected_revision=expected_revision, discovered=catalog)

    def is_binding_active(self, binding: dict) -> bool:
        """会话维护读取 PG 权威状态；API 改配置后 worker 不必等下一次工具调用才回收。"""
        with self.sessions() as session:
            current = session.get(MCPConnection, binding["id"])
            version = session.get(MCPConnectionRevision, (binding["id"], binding["revision"]))
            return bool(
                version
                and version.content == binding
                and current
                and not current.deleted
                and current.enabled
                and current.authorization_epoch == binding["authorization_epoch"]
                and current.config == binding["config"]
            )

    def check(self, binding: dict, name: str) -> dict:
        """冻结内容完整且许可仍有效才能使用；不根据远端hint或同名新服务猜测权限。"""
        with self.sessions() as session:
            current = session.get(MCPConnection, binding["id"])
            version = session.get(MCPConnectionRevision, (binding["id"], binding["revision"]))
            if not version or version.content != binding:
                raise PermissionError("MCP冻结配置不兼容")
            if (
                not current
                or current.deleted
                or not current.enabled
                or current.authorization_epoch != binding["authorization_epoch"]
            ):
                raise PermissionError("MCP连接或许可已撤回；新任务须使用重新审查后的配置")
            frozen = next((item for item in binding["tools"] if item["name"] == name), None)
            current_tool = next((item for item in current.tools if item["name"] == name), None)
            if (
                not frozen
                or frozen["permission"] == "deny"
                or not current_tool
                or current_tool["permission"] == "deny"
            ):
                raise PermissionError("MCP工具尚未许可")
            return frozen
