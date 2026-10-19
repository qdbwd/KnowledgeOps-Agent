"""低频能力配置写入串行化；组、角色和策略必须在同一事务中一致更新。"""

from sqlalchemy import text


def lock_configuration(session, *, shared: bool = False) -> None:
    """先取配置锁，再取具体行锁；新根快照取共享锁，避免读到半套新配置。

    锁只覆盖本数据库的配置事务，不锁住模型/工具执行，也不访问运行控制行。
    所有配置写入口遵守同一顺序，空配置首次保存也能参与互斥。
    """
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    session.execute(text(f"SELECT {function}(73154, 36)"))


class GroupSelectionError(ValueError):
    """HTTP 可映射为参数错误；组的存在性只能在当前数据库目录中校验。"""
