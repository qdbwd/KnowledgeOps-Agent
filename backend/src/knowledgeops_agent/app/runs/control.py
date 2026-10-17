"""短事务的统一控制锁，避免暂停与发布采用相反的行锁顺序。

沿用首版根任务串行领取锁；只保护数据库控制事务，不跨模型、工具或文件
网络调用持锁。当前应用本就只执行一个根任务，未来并行化时再按会话分片。
"""

from sqlalchemy import text


def lock_control(session):
    session.execute(text("SELECT pg_advisory_xact_lock(748616002)"))
