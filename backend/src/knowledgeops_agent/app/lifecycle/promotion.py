"""把正式内容真正引用的内部文本交接给来源版本，不自动赋予独立保存意图。"""

import hashlib

from knowledgeops_agent.app.lifecycle.references import replace_references
from knowledgeops_agent.persistence import models as db


def promote_material(session, row) -> str:
    """与旧身份失效同事务保存固定正文及别名；失败时仍由原内部副本承担读取。

    调用方已持控制锁并查明存活引用。这里不生成阅读Evidence、不改变读取授权，
    也不加library根；依赖以后消失，转换出的正文仍可沿正常来源回收流程释放。
    """
    if row.promoted_version_id:
        return row.promoted_version_id
    content = row.content
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if digest != row.content_hash:
        raise ValueError("material integrity check failed before promotion")
    owner = session.get(db.Run, row.run_id)
    source = db.Source(identity_key="run-artifact:" + row.id, kind="text", ownership_known=True,
                       title="引用文本 · " + ((owner.task if owner else "任务结果") or "任务结果")[:160],
                       origin="run-artifact:" + row.id)
    session.add(source)
    session.flush()
    version = db.SourceVersion(source_id=source.id, number=1, content_hash=digest,
                               content_hash_kind="body-v1", body_hash=digest, body_format="text",
                               storage_key=None, original_hash=None, mime_type="text/plain",
                               parser="run-artifact-v1", status="ready")
    session.add(version)
    session.flush()
    for position, offset in enumerate(range(0, len(content), 2400)):
        piece = content[offset:offset + 2400]
        session.add(db.SourceSegment(source_version_id=version.id, position=position,
                                     start_offset=offset, end_offset=offset + len(piece), text=piece,
                                     method="text", status="ready"))
    row.promoted_version_id = version.id
    session.flush()
    # JSON工具结果中的参数字段可能横跨分段，完整引用不能靠片段分别解析。
    replace_references(session, "asset", "source-body:" + version.id, content)
    return version.id
