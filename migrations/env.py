"""仅迁移应用自己拥有的表；模型账本与 LangGraph 检查点各自维护。

命令行读取应用配置，不将 DSN 写入 alembic.ini 或日志。测试可以注入连接，
让同一套正式迁移在隔离数据库/临时 schema 上验证。
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from knowledgeops_agent.core.config import get_settings
from knowledgeops_agent.persistence.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)


def include_object(obj, name, type_, reflected, compare_to):
    """自动生成只比较业务表，避免把框架检查点或其他表误识别为应删除。"""
    return not (type_ == "table" and reflected and name not in Base.metadata.tables)


def migrate(connection):
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=get_settings().database_url,
        target_metadata=Base.metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    migrate(config.attributes["connection"])
else:
    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        migrate(connection)
    engine.dispose()
