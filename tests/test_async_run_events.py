"""主运行等待子结果时 SSE 必须持续接收事件，不能要求用户刷新才继续。"""

import asyncio

from test_async_run_scheduling import family
from test_jobs import jobs as _jobs

from knowledgeops_agent.app.runs.children import ChildRuns
from knowledgeops_agent.app.runs.events import iter_run_events

jobs = _jobs


async def test_waiting_children_keeps_stream_open_until_result_and_parent_completion(jobs):
    _, _, service, _, _ = jobs
    root, first, second = family(service)
    service.claim("first", run_id=first["id"], generation=1)
    service.claim("second", run_id=second["id"], generation=1)
    waiting = ChildRuns(service).wait(root["id"], "main", [first["id"], second["id"]])

    async def connected():
        return False

    stream = iter_run_events(service, None, root["id"], waiting["last_event"], connected)
    received = asyncio.create_task(anext(stream))
    try:
        await asyncio.sleep(.1)
        assert not received.done(), "不能在 waiting_children 发送 closed"
        service.finish(first["id"], "first", outcome={"answer": "来源核对完成"})
        assert '"kind": "child_finished"' in await asyncio.wait_for(received, 3)
        service.finish(second["id"], "second", outcome={"answer": "边界核对完成"})
        queued = service.get_run(root["id"])
        service.claim("main-again", run_id=root["id"], generation=queued["generation"])
        service.finish(root["id"], "main-again", outcome={"answer": "已整合"})
        remaining = [event async for event in stream]
        assert any('"kind": "children_ready"' in event for event in remaining)
        assert remaining[-1].startswith("event: closed")
    finally:
        if not received.done():
            received.cancel()
            await asyncio.gather(received, return_exceptions=True)
        await stream.aclose()
