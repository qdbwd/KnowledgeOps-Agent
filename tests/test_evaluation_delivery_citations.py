"""保留真实K16交付形态，正文真链接不能掩盖最终回复只有代码链接。

样本取自a765037e/K16-multi-1，材料均为合成测试规则，无账号或密钥。
仅把历史证据ID映射到隔离测试库的真实证据，原回答布局保持。
"""

from pathlib import Path

import pytest
from test_api_worker import services  # noqa: F401

from evaluation.checks import evaluate


@pytest.mark.parametrize(
    "presentation", ["original_code_only", "clickable", "receipt", "example_with_source"]
)
def test_split_delivery_citation_contract(services, presentation):  # noqa: F811
    refs = []
    for title, body in [
        ("根任务预算", "上限40次模型请求。"),
        ("中断与幂等", "服务重启后由用户恢复。"),
    ]:
        source = services.sources.import_text(title, body)
        refs.append(services.sources.read_segment(source["segments"][0]["id"], root_id="root"))
    answer = Path("tests/fixtures/k16_split_answer.md").read_text(encoding="utf-8")
    for old, ref in zip(
        [
            "8f316e3b-6762-4402-b81d-5639bc930a93",
            "d27fb833-fbfb-4936-ae16-dca85af48125",
        ],
        refs,
        strict=True,
    ):
        answer = answer.replace(old, ref["id"])
    if presentation == "clickable":
        # 只去掉包裹citation的代码标记，不改事实或引用目标。
        import re

        answer = re.sub(r"`(\[[^\]]+\]\(/api/evidence/[^)]+\))`", r"\1", answer)
    elif presentation == "receipt":
        answer = "草稿已准备，请在变更页面查看完整正文并确认影响。"
    elif presentation == "example_with_source":
        answer = "正文结论" + refs[0]["citation"] + "\n链接写法示例：`" + refs[0]["citation"] + "`"
    result = {
        "run": {
            "id": "root",
            "status": "completed",
            "outcome": {"answer": answer},
            "budgets": {"model_requests": 10},
        },
        "drafts": [
            {
                "root_id": "root",
                "status": "draft",
                "requires_impact_confirmation": True,
                "operations": [{"action": "create", "body": ref["citation"]} for ref in refs]
                + [{"action": "archive"}],
            }
        ],
        "agent_documents": [],
        "documents": [],
        "fixture": {"before": []},
        "events": [],
        "children": [],
        "models": [],
        "phases": {},
    }
    _, hard, _ = evaluate(services, {"scenario": "split"}, result)
    assert hard["citations_resolve"]
    assert hard["article_citations_complete"]
    assert hard["answer_citations_complete"] is (presentation != "original_code_only")
    assert result["run"]["outcome"]["answer"] == answer
