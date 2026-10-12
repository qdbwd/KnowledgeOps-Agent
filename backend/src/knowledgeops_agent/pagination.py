"""时间+ID稳定游标。分页之前先过滤，新增记录不会把旧记录挤到不可达位置。"""

import base64
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import tuple_


def page_rows(session, query, model, *, cursor: str | None, limit: int):
    """只返回一页数据库实体及下一游标；排序字段不由外部请求控制。"""
    if not 1 <= limit <= 100:
        raise ValueError("page size must be between 1 and 100")
    if cursor:
        try:
            if len(cursor) > 512:
                raise ValueError("oversize cursor")
            stamp, identifier = json.loads(base64.urlsafe_b64decode(cursor).decode())
            instant = datetime.fromisoformat(stamp)
            if instant.tzinfo is None:
                raise ValueError("timezone is required")
            UUID(identifier)
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("invalid page cursor") from None
        query = query.where(tuple_(model.created_at, model.id) < tuple_(instant, identifier))
    rows = list(
        session.scalars(query.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1))
    )
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more:
        last = rows[-1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps([last.created_at.isoformat(), last.id]).encode()
        ).decode()
    return rows, next_cursor
