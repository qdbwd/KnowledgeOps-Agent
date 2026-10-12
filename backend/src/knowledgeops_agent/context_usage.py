"""只读展示最近主模型输入的上下文占用，不把费用累计或历史字节数称为token。"""

from sqlalchemy import select

from knowledgeops_agent.db import Conversation, Run, RunEvent
from knowledgeops_agent.model_gateway import requests


def context_usage(sessions, gateway, conversation_id: str) -> dict:
    """从现有运行与账本取数，无模型请求、重分词或新一套状态存储。

    UI以应用工作窗口作分母，模型物理容量另列；本项目取完整请求的输入量，
    优先供应商usage，否则用本地tokenizer估计。运行完成且轮末压缩成功后，
    改用压缩后工作视图的本地估计并明确标注。两者都不声称已计入未来输入。
    """
    with sessions() as session:
        if session.get(Conversation, conversation_id) is None:
            raise KeyError("conversation not found")
        run_ids = list(
            session.scalars(
                select(Run.id).where(
                    Run.conversation_id == conversation_id, Run.parent_id.is_(None)
                )
            )
        )
    row = None
    if run_ids:
        # 业务库与账本是不同数据库；只传所属主run ID，不读取完整请求正文。
        with gateway.ledger.engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        requests.c.id,
                        requests.c.run_id,
                        requests.c.created_at,
                        requests.c.usage,
                        requests.c.request["capacity"].label("capacity"),
                    )
                    .where(
                        requests.c.scope == gateway.ledger.scope,
                        requests.c.run_id.in_(run_ids),
                        requests.c.purpose == "agent",
                    )
                    .order_by(requests.c.ordinal.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
    measured = dict(row["capacity"] or {}) if row else {}
    basis = "last_model_input"
    measured_at = row["created_at"] if row else None
    if row:
        # 轮末压缩在最后一次主模型之后发生。只显示已经完成运行的匹配事件，
        # 防止图尚未提交/失败时让界面声称压缩已生效；后续模型请求自然取代它。
        with sessions() as session:
            event = session.scalar(
                select(RunEvent)
                .join(Run, Run.id == RunEvent.run_id)
                .where(
                    RunEvent.run_id == row["run_id"],
                    Run.status == "completed",
                    RunEvent.kind == "context_compacted",
                    RunEvent.data["trigger"].astext == "after_turn",
                    RunEvent.data["based_on_request_id"].astext == row["id"],
                )
                .order_by(RunEvent.sequence.desc())
                .limit(1)
            )
            if event and event.data.get("after"):
                measured = event.data["after"]
                basis, measured_at = "after_turn_compaction_estimate", event.created_at
    profile = gateway.capacity
    window = measured.get("model_window", profile.context_window)
    working = measured.get("working_window", profile.working_window)
    method = measured.get("method")
    fraction = measured.get(
        "summary_fraction",
        0.85 if method == "utf8_bytes_plus_framing_v1" else profile.summary_fraction,
    )
    input_tokens: int | None = None
    estimated = True
    reason: str | None = "no_model_input"
    if row:
        actual = (row["usage"] or {}).get("input_tokens") if basis == "last_model_input" else None
        if isinstance(actual, int) and not isinstance(actual, bool) and actual >= 0:
            input_tokens, method, estimated, reason = actual, "provider_usage", False, None
        elif method in {"deepseek_v41_tokenizer_v1", "deepseek_v41_json_estimate_v1"}:
            input_tokens, reason = measured.get("input_tokens"), None
        else:
            reason = "legacy_byte_estimate"
    return {
        "basis": basis,
        "model": measured.get("model", profile.model),
        "model_window": window,
        "working_window": working,
        "input_tokens": input_tokens,
        "percentage": round(input_tokens / working * 100, 1) if input_tokens is not None else None,
        "method": method,
        "estimated": estimated,
        "unavailable_reason": reason,
        "output_reserve": measured.get("output_tokens", profile.output_reserve),
        "framing_reserve": measured.get("framing_reserve", profile.framing_reserve),
        "summary_fraction": fraction,
        "summary_trigger_basis": measured.get("summary_trigger_basis", "total_tokens"),
        "summary_trigger_tokens": int(working * fraction),
        "request_id": row["id"] if row else None,
        "run_id": row["run_id"] if row else None,
        "measured_at": measured_at,
    }
