"""详情清理后仍能判断重复接单，不以保留完整任务输入维持幂等。"""

import hashlib
import json


def normalized_inputs(value: dict) -> dict:
    result = {"auxiliary": False, **value}
    result.pop("runtime_settings", None)
    result["reasoning"] = result.get("reasoning", {}).get("requested", "default")
    return result


def request_fingerprint(conversation_id, task: str, inputs: dict, budgets: dict) -> str:
    payload = json.dumps([conversation_id, task, inputs, budgets], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def freeze_request(row) -> None:
    """在清除原输入前冻结，沿用原先重试比较的字段与理由选择语义。"""
    inputs = normalized_inputs(row.input_data) if row.kind == "agent" else row.input_data
    row.input_fingerprint = request_fingerprint(row.conversation_id, row.task, inputs, row.budgets)
    row.details_summary = {
        **row.details_summary, "requested_reasoning": inputs.get("reasoning", "default"),
    }
