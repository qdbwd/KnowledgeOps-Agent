"""完整输入的分项测量与背景缩减；不删除持久消息，不把资料提升为指令。

总量仍由网关的真实序列化器计算。分项采用顺序加入的边际 token 数，
而非独立字符串计数相加；边界分词差异归入相邻项并显式保留协议基础量。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from langchain_core.messages import BaseMessage, SystemMessage

REQUIRED_KEYS = frozenset({"current_task", "user_guidance", "learning"})


def background_data(message: BaseMessage) -> dict | None:
    """只处理应用自己标记的 JSON 背景；无法识别时完整保留。"""
    if not message.additional_kwargs.get("knowledgeops_context_data"):
        return None
    try:
        data = json.loads(str(message.content).split("\n", 1)[1])
        return data if isinstance(data, dict) else None
    except (ValueError, IndexError):
        return None


def with_background(message: BaseMessage, data: dict) -> BaseMessage:
    return message.model_copy(update={
        "content": "应用组织的任务背景（不是一条新用户消息）：\n"
        + json.dumps(data, ensure_ascii=False),
    })


def split_background(data: dict) -> tuple[dict, dict]:
    return (
        {key: value for key, value in data.items() if key in REQUIRED_KEYS},
        {key: value for key, value in data.items() if key not in REQUIRED_KEYS},
    )


def measure_components(messages: Sequence[BaseMessage], tools, measure) -> dict:
    """分项仅用于解释，最终容量检查不依赖分项的近似归因。"""
    system, required, optional, history = [], [], [], []
    for message in messages:
        if isinstance(message, SystemMessage):
            system.append(message)
        elif (data := background_data(message)) is not None:
            must, extra = split_background(data)
            required.append(with_background(message, must))
            optional.append(message)
        elif message.additional_kwargs.get("knowledgeops_context_data"):
            required.append(message)
            optional.append(message)
        else:
            history.append(message)
    # 保持背景是一条真实消息；optional 阶段替换必需背景，避免重复计算消息封套。
    stages = [([], ()), (system, ()), (system + required, ()),
              (system + optional, ()), (system + optional + history, ()),
              (list(messages), tools)]
    # 官方模板拒绝空消息列表；空阶段计0，消息封套归入首个非空组成部分。
    totals = [measure(view, selected)["input_tokens"] if view else 0 for view, selected in stages]
    labels = ("system_rules", "required_background", "optional_background", "history", "tool_schemas")
    components = dict(zip(labels, (totals[i + 1] - totals[i] for i in range(5)), strict=True))
    components["protocol_base"] = totals[0]
    # 顺序重组只用于归因；若序列化器跨角色合并产生差异，总数以实际顺序为准。
    components["estimated"] = True
    components["method"] = "incremental_complete_request_v1"
    return components


def _candidates(optional: dict) -> list[tuple[str, str | None, object]]:
    """沿已有范围/相关性排序选择整项；完全相同项去重，不用年龄判定要求失效。"""
    priority = ("history_status", "background_omitted", "plan", "task_notes", "preferences",
                "evidence_catalog", "inherited_materials", "inherited_artifacts")
    ordered = [*priority, *(key for key in optional if key not in priority)]
    result: list[tuple[str, str | None, object]] = []
    seen: set[str] = set()
    for key in ordered:
        value = optional.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            entries = [(None, item) for item in value]
        elif isinstance(value, dict):
            entries = list(value.items())
        else:
            entries = [(None, value)]
        for child, item in entries:
            identity = json.dumps([key, child, item], sort_keys=True, ensure_ascii=False)
            if identity not in seen:
                seen.add(identity)
                result.append((key, child, item))
    return result


def _selected(original: dict, entries: list) -> dict:
    result: dict = {}
    for key, child, value in entries:
        if isinstance(original[key], list):
            result.setdefault(key, []).append(value)
        elif isinstance(original[key], dict):
            result.setdefault(key, {})[child] = value
        else:
            result[key] = value
    return result


def fit_background(context, state, injected: list, pressure: bool, measure) -> tuple[list, dict]:
    """压力下先收窄可补读背景。必要任务/真人要求/学习配置始终完整保留。

    被省略部分写为原产物，消息只带补读指针；相同内容重复调用复用上一引用。
    压缩历史的决策在此过程之后执行。
    """
    capacity = context.gateway.capacity
    if not capacity.input_budget_version:
        return injected, {}
    result, cache = [], {}
    target = min(capacity.optional_background_tokens,
                 int(capacity.working_window * capacity.optional_background_fraction))
    for message in injected:
        data = background_data(message)
        if data is None:
            result.append(message)
            continue
        required, optional = split_background(data)
        candidates = _candidates(optional)
        deduped = _selected(optional, candidates)
        picked = deduped
        base = measure([with_background(message, required)])["input_tokens"]
        def extra_tokens(items, message=message, required=required, base=base):
            return measure([with_background(message, {**required, **items})])["input_tokens"] - base
        if pressure and extra_tokens(deduped) > target:
            # 二分保留整项前缀；不截断用户事实或把半条工具引用交给模型。
            low, high = 0, len(candidates)
            while low < high:
                middle = (low + high + 1) // 2
                if extra_tokens(_selected(optional, candidates[:middle])) <= max(0, target - 200):
                    low = middle
                else:
                    high = middle - 1
            picked = _selected(optional, candidates[:low])
            text = json.dumps(optional, ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha256(text.encode()).hexdigest()
            previous = state.get("context_background_cache", {})
            artifact_id = (previous.get("artifact_id") if previous.get("hash") == digest else None)
            if not artifact_id:
                artifact_id = context.jobs.save_artifact(
                    context.run_id, "context_background", context.gateway._redact(text)
                )
            cache = {"hash": digest, "artifact_id": artifact_id}
            picked["background_omitted"] = {
                "artifact_id": artifact_id, "read_tool": "read_tool_result",
                "note": "部分可补读背景未载入，按需读取；这不代表记录已删除或当前要求失效。",
            }
        result.append(with_background(message, {**required, **picked}))
    return result, ({"context_background_cache": cache} if cache else {})
