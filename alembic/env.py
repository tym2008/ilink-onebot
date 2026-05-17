"""
Alembic 迁移环境配置（SQLite + aiosqlite）

异步数据库连接 + 自动发现 SQLModel metadata。
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlmodel import SQLModel

from alembic import context
from meta_config import meta_config

# 导入所有模型以注册到 SQLModel.metadata
# 必须在 target_metadata 赋值之前完成
import sqlmodels  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def _resolve_database_url() -> str:
    """
    解析迁移使用的数据库 URL。

    优先用 alembic Config 中显式注入的 `sqlalchemy.url`（DatabaseManager 在程序
    内调用 `set_main_option` 注入运行时 URL），回退到 meta_config 的默认值。
    这让"程序内迁移"和"命令行 alembic upgrade head"都指向同一个 DB。
    """
    injected = config.get_main_option('sqlalchemy.url')
    if injected:
        return injected
    return meta_config.DATABASE_URL


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本而不连接数据库"""
    context.configure(
        url=_resolve_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
        # SQLite 需要批处理模式支持 ALTER TABLE
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在给定连接上执行迁移"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite 的 ALTER TABLE 限制需要 batch 模式
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """异步模式：连接数据库执行迁移"""
    connectable = async_engine_from_config(
        {'sqlalchemy.url': _resolve_database_url()},
        prefix='sqlalchemy.',
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式入口"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
