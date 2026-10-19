"""展示类别目录：可增删改，不参与记忆证据判断、召回优先级或操作权限。"""

import builtins
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.persistence.models import MemoryCategoryDefinition, Preference

DEFAULT_NAMES = {
    "preference": "表达偏好",
    "background": "用户背景",
    "behavior": "协作习惯",
    "cognitive": "思考与学习习惯",
    "goal": "持续目标",
    "correction": "明确纠正",
}


class UnsetCategory:
    """区分旧调用没有传类别、新调用显式选择未分类。"""


UNSET = UnsetCategory()
CategorySelection = str | None | UnsetCategory


def normalize_name(name: str) -> tuple[str, str]:
    if not isinstance(name, str) or any(unicodedata.category(c).startswith("C") for c in name):
        raise ValueError("类别名称不能包含控制字符")
    name = " ".join(unicodedata.normalize("NFKC", name).split())
    key = name.casefold()
    if not 1 <= len(name) <= 100 or len(key) > 200:
        raise ValueError("类别名称须为1至100字符")
    return name, key


def _view(row):
    return {key: getattr(row, key) for key in ("id", "name", "revision", "deleted", "origin")}


def category_rows(session) -> list[dict]:
    """迁移有真实种子；新空测试目录保留同一基线，GET 不写库，墓碑优先。"""
    values = {
        "memory-category-" + key: {
            "id": "memory-category-" + key,
            "name": name,
            "revision": 1,
            "deleted": False,
            "origin": "legacy",
        }
        for key, name in DEFAULT_NAMES.items()
    }
    values.update({row.id: _view(row) for row in session.scalars(select(MemoryCategoryDefinition))})
    return [row for row in values.values() if not row["deleted"]]


def require_category(session, identity: str) -> MemoryCategoryDefinition:
    """写调用已持有控制锁；缺省种子可以落地，已删除种子绝不复活。"""
    row = session.get(MemoryCategoryDefinition, identity)
    if row is None:
        seed = next((value for value in category_rows(session) if value["id"] == identity), None)
        if seed:
            row = MemoryCategoryDefinition(**seed, name_key=normalize_name(seed["name"])[1])
            session.add(row)
            session.flush()
    if row is None or row.deleted:
        raise ValueError("所选类别已删除或不存在，请刷新类别目录")
    return row


def assign_category(session, row: Preference, identity: str | None, *, source="user") -> None:
    if identity is not None:
        identity = require_category(session, identity).id
    row.display_category_id = identity
    row.classification = {"status": "classified" if identity else "unclassified", "source": source}


def legacy_category(session, row: Preference) -> None:
    """旧语义映射只在未设置展示属性时补一次；不会重分类历史记录。"""
    identity = "memory-category-" + row.category
    available = {item["id"] for item in category_rows(session)}
    assign_category(session, row, identity if identity in available else None, source="legacy")


class ModelCategoryChoice(BaseModel):
    """两个模型入口共用窄输出；模型不能改类别定义或借分类改记忆属性。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    category_id: str | None = Field(max_length=36)
    new_category: str | None = Field(max_length=100)

    @model_validator(mode="after")
    def one_choice(self):
        if bool(self.category_id) == bool(self.new_category):
            raise ValueError("classification requires exactly one choice")
        return self


def model_catalog(session) -> list[dict]:
    """仅冻结有界类别目录，不把全部记忆正文加入分类或意图请求。"""
    return sorted(
        ({key: row[key] for key in ("id", "name", "revision")} for row in category_rows(session)),
        key=lambda row: row["id"],
    )


def resolve_model_choice(session, choice: ModelCategoryChoice, known: list[dict]) -> str:
    """调用者持有控制锁；用户已删/改名的目录项不能由旧请求复活。"""
    current = model_catalog(session)
    if choice.category_id:
        previous = next((row for row in known if row["id"] == choice.category_id), None)
        if previous is None or previous not in current:
            raise ValueError("分类目录已变化或类别不在本次请求中")
        return require_category(session, choice.category_id).id
    name, key = normalize_name(choice.new_category or "")
    old = next((row for row in known if normalize_name(row["name"])[1] == key), None)
    if old is not None and old not in current:
        raise ValueError("分类目录已变化，不能重建被删除或改名的类别")
    existing = next((row for row in current if normalize_name(row["name"])[1] == key), None)
    if existing:
        return require_category(session, existing["id"]).id
    if len(current) >= 200:
        raise ValueError("类别数量已达到上限")
    row = MemoryCategoryDefinition(name=name, name_key=key, origin="model")
    session.add(row)
    session.flush()
    return row.id


class MemoryCategories:
    def __init__(self, sessions):
        self.sessions = sessions

    def list(self) -> builtins.list[dict]:
        with self.sessions() as session:
            counts = dict(
                session.execute(
                    select(Preference.display_category_id, func.count())
                    .where(Preference.status != "deleted")
                    .group_by(Preference.display_category_id)
                ).all()
            )
            return [
                {**row, "memory_count": counts.get(row["id"], 0)} for row in category_rows(session)
            ]

    def create(self, name: str, *, origin="user") -> dict:
        name, key = normalize_name(name)
        with self.sessions.begin() as session:
            lock_control(session)
            rows = category_rows(session)
            if any(normalize_name(row["name"])[1] == key for row in rows):
                raise ValueError("同名类别已存在，请选择已有类别")
            if len(rows) >= 200:
                raise ValueError("类别数量已达到200项，请整理已有类别")
            row = MemoryCategoryDefinition(name=name, name_key=key, origin=origin)
            session.add(row)
            session.flush()
            return {**_view(row), "memory_count": 0}

    def update(self, identity: str, *, expected_revision: int, name: str) -> dict:
        name, key = normalize_name(name)
        with self.sessions.begin() as session:
            lock_control(session)
            row = require_category(session, identity)
            if row.revision != expected_revision:
                raise ValueError("类别修订已变化，请刷新后再操作")
            if any(
                item["id"] != identity and normalize_name(item["name"])[1] == key
                for item in category_rows(session)
            ):
                raise ValueError("同名类别已存在，请选择已有类别")
            row.name, row.name_key, row.revision = name, key, row.revision + 1
            return _view(row)

    def delete(self, identity: str, *, expected_revision: int) -> dict:
        from knowledgeops_agent.memory.service import MemoryService

        with self.sessions.begin() as session:
            lock_control(session)
            category = require_category(session, identity)
            if category.revision != expected_revision:
                raise ValueError("类别修订已变化，请刷新后再操作")
            rows = list(
                session.scalars(
                    select(Preference)
                    .where(Preference.display_category_id == identity)
                    .order_by(Preference.id)
                    .with_for_update()
                )
            )
            for row in rows:
                assign_category(session, row, None)
                row.revision += 1
                # 类别删除仅改变展示；不重置确认时间、证据或撤回代次。
                MemoryService._save(session, row)
            category.deleted, category.revision = True, category.revision + 1
            return {**_view(category), "moved_memories": len(rows)}
