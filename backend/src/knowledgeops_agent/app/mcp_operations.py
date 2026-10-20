"""MCP依赖准备的持久投递，不创建收费Agent Run，也不把Redis结果当成事实。"""

from datetime import timedelta

from sqlalchemy import or_, select

from knowledgeops_agent.app.runs.queue import MCP_PREPARE_TASK
from knowledgeops_agent.persistence.models import ExtensionOperation, utcnow


class MCPPreparationPublisher:
    def __init__(self, sessions, queue):
        self.sessions, self.queue = sessions, queue

    def publish_due(self, *, limit=5) -> int:
        sent, now = 0, utcnow()
        with self.sessions.begin() as session:
            rows = session.scalars(select(ExtensionOperation).where(
                ExtensionOperation.kind == 'mcp_candidate', ExtensionOperation.status == 'queued',
                ExtensionOperation.cancel_requested.is_(False),
                or_(ExtensionOperation.lease_until.is_(None), ExtensionOperation.lease_until <= now),
            ).order_by(ExtensionOperation.created_at).limit(limit).with_for_update(skip_locked=True))
            for row in rows:
                try:
                    self.queue.send_task(MCP_PREPARE_TASK, args=[row.id, row.attempts],
                                         task_id=f'mcp:{row.id}:{row.attempts}')
                except Exception:
                    row.error_code, row.lease_until = 'dispatch_failed', now + timedelta(seconds=5)
                else:
                    row.error_code, row.lease_until = None, now + timedelta(seconds=30)
                    sent += 1
        return sent
