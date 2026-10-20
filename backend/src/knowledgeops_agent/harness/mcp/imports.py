"""把常见MCP连接文件转成非秘密配置与显式准备请求，不启动包管理器。

按npm npx的参数边界与uv工具文档解析常见形式；不把未知选项拼成shell。
准备阶段将请求解析为固定安装，运行适配层只接受最终入口。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from uuid import uuid4

from packaging.requirements import InvalidRequirement, Requirement

from knowledgeops_agent.harness.mcp.registry import configuration

REFERENCE = re.compile(r"\$\{(?:env:)?([A-Z_][A-Z0-9_]*)\}")
NPM_PACKAGE = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(?:@[A-Za-z0-9~^<>=|.* +_-]+)?")
ENTRY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class ImportedConnection:
    name: str
    config: dict
    installation: dict | None
    secret_values: dict[str, str] = field(default_factory=dict, repr=False)

    def safe_view(self) -> dict:
        return {"name": self.name, "config": self.config, "installation": self.installation,
                "preparation_required": self.installation is not None,
                "provided_secret_refs": sorted(self.secret_values)}


def _strings(value, *, limit=128) -> list[str]:
    if not isinstance(value, list) or len(value) > limit or any(
        not isinstance(item, str) or len(item) > 4096 or "\x00" in item for item in value
    ):
        raise ValueError("MCP参数必须为有界字符串数组")
    return list(value)


def _package(value: str, manager: str) -> str:
    if len(value) > 512:
        raise ValueError("MCP包描述过长")
    if manager == "npm":
        if not NPM_PACKAGE.fullmatch(value):
            raise ValueError("当前仅支持npm仓库包名及版本，不支持URL、本地路径或Git安装")
    else:
        try:
            requirement = Requirement(value)
            if requirement.url or requirement.marker:
                raise ValueError("当前仅支持Python包名、extras及版本约束")
        except InvalidRequirement:
            raise ValueError("Python包描述无效；请使用包名、extras及版本约束") from None
    return value


def _entry(value: str) -> str:
    if not ENTRY.fullmatch(value):
        raise ValueError("MCP入口必须是包提供的命令名，不能是路径或shell表达式")
    return value


def _launch(command: str, args: list[str]) -> tuple[dict | None, list[str]]:
    runner = PurePosixPath(command).name.lower()
    if runner not in {"npx", "npx.cmd", "uvx", "uvx.exe"}:
        return None, args
    manager = "npm" if runner.startswith("npx") else "uv"
    packages, source, python = [], None, None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if not arg.startswith("-"):
            break
        if manager == "npm" and arg in {"-y", "--yes", "--yes=true"}:
            index += 1
            continue
        key, separator, value = arg.partition("=")
        allowed = {"--package", "-p"} if manager == "npm" else {"--from", "--with", "--python"}
        if key not in allowed:
            raise ValueError("不支持此启动器选项；只支持显式包、版本、依赖和已有Python选择")
        if not separator:
            index += 1
            if index >= len(args):
                raise ValueError("MCP启动器选项缺少值")
            value = args[index]
        if key == "--python":
            if python is not None or not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+){0,2}|/[A-Za-z0-9_./-]+)", value):
                raise ValueError("Python选择必须是镜像中已有的版本或绝对路径")
            python = value
        elif key == "--from":
            if source is not None:
                raise ValueError("--from只能指定一次")
            source = _package(value, manager)
        else:
            packages.append(_package(value, manager))
        index += 1
    if index >= len(args) or len(packages) > 16:
        raise ValueError("MCP启动配置缺少入口或包含过多依赖")
    positional, arguments = args[index], args[index + 1:]
    if manager == "npm":
        entry = _entry(positional) if packages else None
        if not packages:
            packages = [_package(positional, manager)]
    else:
        entry = positional
        if "@" in positional:
            if source is not None:
                raise ValueError("使用--from时请将版本写在包描述中")
            entry, version = positional.rsplit("@", 1)
            if version != "latest" and not re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]*", version):
                raise ValueError("uvx的@语法只支持确切版本或latest")
            source = entry if version == "latest" else f"{entry}=={version}"
        entry = _entry(entry)
        packages = [_package(source or entry, manager), *packages]
    return {"manager": manager, "packages": packages, "entrypoint": entry, "python": python}, arguments


def _references(values: dict, *, kind: str, prefix: str) -> tuple[dict, dict, dict]:
    if not isinstance(values, dict) or len(values) > 64:
        raise ValueError("环境变量或请求头必须为有界对象")
    refs, secrets, prefixes = {}, {}, {}
    for index, (name, value) in enumerate(values.items()):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name):
            raise ValueError("环境变量或请求头名称无效")
        if kind == "env" and not ENV_NAME.fullmatch(name):
            raise ValueError("环境变量名称无效")
        if not isinstance(value, str) or not value or len(value) > 8192 or any(c in value for c in "\r\n\x00"):
            raise ValueError("环境变量与请求头须为非空单行字符串")
        header_prefix = next((item for item in ("Bearer ", "Basic ") if kind == "headers" and value.startswith(item)), "")
        reference = REFERENCE.fullmatch(value[len(header_prefix):])
        if reference:
            refs[name] = reference[1]
            if header_prefix:
                prefixes[name] = header_prefix
        elif "${" in value:
            raise ValueError("仅支持完整环境引用，以及Bearer/Basic请求头引用")
        else:
            reference_name = f"{prefix}_{kind.upper()}_{index}"
            refs[name], secrets[reference_name] = reference_name, value
    return refs, secrets, prefixes


def _one(name, raw: dict) -> ImportedConnection:
    if not isinstance(name, str) or not name.strip() or len(name) > 200 or not isinstance(raw, dict):
        raise ValueError("每条MCP连接需要名称与配置对象")
    config = dict(raw)
    if config.get('installation_id'):
        raise ValueError('托管安装不能直接从其他设备复制入口；请导入包描述并重新准备')
    # 常见客户端的disabled只是保存意图；导入一律不授予启用或工具许可。
    config.pop("disabled", None)
    kind = config.pop("type", None) or config.get("transport") or ("http" if "url" in config else "stdio")
    if kind == "streamable-http":
        kind = "http"
    config["transport"] = kind
    command = config.get("command", "")
    if not isinstance(command, str) and command is not None:
        raise ValueError("MCP启动命令必须为文本")
    args = _strings(config.get("args", []))
    if any(re.match(r"--?(?:api[-_]key|token|password|secret|authorization)(?:=|$)", item, re.I) for item in args):
        raise ValueError("请通过env或headers配置密钥，不要把凭据放进启动参数")
    installation, config["args"] = _launch(command or "", args)
    if installation:
        config["command"] = "prepared-mcp-entrypoint"
    prefix = "MCP_" + uuid4().hex.upper()
    secret_values = {}
    for input_key, output_key in (("env", "environment_refs"), ("headers", "header_refs")):
        if input_key in config:
            refs, supplied, prefixes = _references(config.pop(input_key), kind=input_key, prefix=prefix)
            prior = config.get(output_key, {})
            if not isinstance(prior, dict) or set(prior) & set(refs):
                raise ValueError("同一个环境变量或请求头不能同时定义引用和内容")
            config[output_key] = {**prior, **refs}
            if prefixes:
                config["header_prefixes"] = {**config.get("header_prefixes", {}), **prefixes}
            secret_values.update(supplied)
    try:
        config = configuration(config)
    except ValueError:
        # 不将Pydantic上下文或原始JSON回送UI，错误参数可能含凭据。
        raise ValueError("MCP配置不受支持；请核对协议、容器内路径、字段和环境引用") from None
    if installation:
        config["command"] = None
    return ImportedConnection(name.strip(), config, installation, secret_values)


def parse_import(value: dict) -> list[ImportedConnection]:
    """接受mcpServers、旧{name,config}和单连接；返回值中的秘密单独交环境文件服务。"""
    if not isinstance(value, dict) or len(json.dumps(value, ensure_ascii=False)) > 128 * 1024:
        raise ValueError("MCP配置需要有界JSON对象")
    if "mcpServers" in value:
        rows = value["mcpServers"]
        if set(value) != {"mcpServers"} or not isinstance(rows, dict) or not 1 <= len(rows) <= 32:
            raise ValueError("mcpServers需要1至32个连接定义")
        return [_one(name, raw) for name, raw in rows.items()]
    if "config" in value:
        if set(value) != {"name", "config"}:
            raise ValueError("旧连接导入只接受name和config")
        return [_one(value["name"], value["config"])]
    return [_one(value.get("name", "MCP服务"), {key: item for key, item in value.items() if key != "name"})]
