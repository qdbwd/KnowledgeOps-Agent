"""背景目录筛选后，旧证据仍能按固定版本分页定位；重复读取不淹没目录。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.persistence.models import Evidence

runtime_services = _runtime_services


def test_ranked_evidence_pages_dedupe_and_keep_older_relevant_or_used_references(runtime_services):
    _, context, run = setup_run(runtime_services)
    first = context.sources.import_text("当前指定材料", "原始材料" * 1000)
    second = context.sources.import_text("其他材料", "另一段正文")
    entries = []
    with context.jobs.sessions.begin() as session:
        for index in range(210):
            item = Evidence(root_id=run["id"], source_segment_id=second["segments"][0]["id"],
                            start_offset=index, end_offset=index + 1, quote="关键词" if index == 2 else "内容",
                            created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index))
            session.add(item)
            session.flush()
            entries.append(item.id)
    current = context.sources.read_segment(first["segments"][0]["id"], root_id=run["id"])
    context.sources.read_segment(first["segments"][0]["id"], root_id=run["id"])
    foreign = context.sources.read_segment(first["segments"][0]["id"], root_id="another-root")
    ranked = context.sources.evidence_page(run["id"], current_source_ids=[first["id"]],
                                           used_ids=[entries[0]], query="关键词", limit=3)
    assert ranked["total"] == 211 and ranked["next_offset"] == 3
    assert ranked["items"][0]["id"] == entries[0]
    assert ranked["items"][1]["read_args"] == {"segment_id": first["segments"][0]["id"]}
    assert ranked["items"][2]["id"] == entries[2]
    all_ids, offset = [], 0
    while offset is not None:
        page = context.sources.evidence_page(run["id"], offset=offset, limit=50)
        all_ids.extend(item["id"] for item in page["items"])
        offset = page["next_offset"]
    assert len(all_ids) == len(set(all_ids)) == 211
    assert set(entries) <= set(all_ids) and foreign["id"] not in all_ids
    assert context.sources.get_evidence(current["id"])["root_id"] == run["id"]
    with context.jobs.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Evidence)) == 213
