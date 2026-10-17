"""删除聊天的来源副本，不删除由用户保留的记忆正文、学习目标和配置值。"""

from sqlalchemy import select, tuple_

from knowledgeops_agent.persistence import models as db


class DeletedConversationSources:
    def __init__(self, session, conversation_id):
        self.conversation_id = conversation_id
        self.messages = set(session.scalars(select(db.ChatMessage.id).where(db.ChatMessage.conversation_id == conversation_id)))
        self.runs = set(session.scalars(select(db.Run.id).where(db.Run.conversation_id == conversation_id)))

    def belongs(self, value):
        return (value.get("conversation_id") == self.conversation_id
                or value.get("message_id") in self.messages
                or value.get("source_message_id") in self.messages
                or value.get("run_id") in self.runs
                or value.get("context_key") == "conversation:" + self.conversation_id)

    def scrub(self, value, *, source=False):
        if isinstance(value, list):
            return [self.scrub(item, source=source) for item in value]
        if not isinstance(value, dict):
            return value
        if source and self.belongs(value):
            return {"kind": "deleted_conversation", "available": False}
        result = {key: self.scrub(item, source=source or key in {"source", "sources", "evidence"})
                  for key, item in value.items()}
        for key in ("message_id", "source_message_id"):
            if result.get(key) in self.messages:
                result[key] = None
                if "quote" in result:
                    result["quote"] = ""
        if result.get("conversation_id") == self.conversation_id:
            result["conversation_id"] = None
            for key in ("conversation_available", "selected"):
                if key in result:
                    result[key] = False
        return result


def scrub_source_batch(session, conversation_id, cursor, limit):
    """旧修订也可能嵌套原话；按表/复合主键分页扫描，不只改当前指针。"""
    models = (db.Preference, db.PreferenceRevision, db.MemoryEvidence, db.LearningTask,
              db.LearningTaskRevision, db.LearningProfileRevision)
    index = int(cursor.get("table", 0))
    model = models[index]
    columns = list(model.__mapper__.primary_key)
    query = select(model)
    if cursor.get("after"):
        query = query.where(tuple_(*columns) > tuple_(*cursor["after"]))
    rows = list(session.scalars(query.order_by(*columns).limit(limit + 1)))
    scrubber = DeletedConversationSources(session, conversation_id)
    for row in rows[:limit]:
        if isinstance(row, db.Preference):
            if row.source_message_id in scrubber.messages:
                row.source_message_id = None
            row.source = scrubber.scrub(row.source, source=True)
        elif isinstance(row, (db.PreferenceRevision, db.LearningTaskRevision)):
            row.content = scrubber.scrub(row.content)
        elif isinstance(row, db.MemoryEvidence):
            if row.message_id in scrubber.messages or row.context_key == "conversation:" + conversation_id:
                row.message_id, row.quote, row.context_key = None, "", None
                row.kind = "source_deleted"
        elif isinstance(row, db.LearningTask):
            row.configuration = scrubber.scrub(row.configuration)
            row.creation_input = scrubber.scrub(row.creation_input)
        elif isinstance(row, db.LearningProfileRevision):
            row.source = scrubber.scrub(row.source, source=True)
    if len(rows) > limit:
        return {"table": index, "after": [getattr(rows[limit - 1], str(col.key)) for col in columns]}
    return {"table": index + 1} if index + 1 < len(models) else None
