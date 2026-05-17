"""
CLI 共享的基础设施启动 / 关闭辅助

所有 typer 子命令在进入异步函数体后都需要：
- 初始化日志
- 初始化 HTTP 共享会话（AioHttpClientSessionMixin）
- 初始化数据库引擎（DatabaseManager，含 Alembic upgrade head）

并在退出前优雅清理。本模块用 `@asynccontextmanager` 把这两步打包，
yield 出 `session_factory` 让命令体显式获得依赖，避免后续代码再从
`DatabaseManager.get_session_factory()` 隐式自取。

用法::

    async def _login_impl() -> None:
        async with infra_lifespan() as session_factory:
            async with session_factory() as session:
                bridge = await Bridge.get_instance(session)
                ...
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from loguru import logger as l
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from meta_config import meta_config
from sqlmodels.database_init import init_default_data
from sqlmodels.database_manager import DatabaseManager
from utils.aiohttp_mixin import AioHttpClientSessionMixin
from utils.logger import setup_logger


@asynccontextmanager
async def infra_lifespan() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """
    初始化全部基础设施并保证退出时优雅清理。

    yield 出 session_factory 供调用方显式持有，依赖通过调用链传递，
    避免下层方法擅自调用 `DatabaseManager.get_session_factory()`。

    启动顺序：logger → HTTP session → DB 引擎（Alembic upgrade head）。
    退出顺序：反向。`finally` 保证即使业务异常也会清理。

    :yield: 数据库会话工厂
    """
    setup_logger()
    l.info("初始化 HTTP 共享会话")
    await AioHttpClientSessionMixin.initialize_http_session()
    l.info("初始化数据库引擎")
    await DatabaseManager.init(database_url=meta_config.DATABASE_URL)
    session_factory = DatabaseManager.get_session_factory()
    await init_default_data(session_factory)

    try:
        yield session_factory
    finally:
        l.info("关闭数据库引擎")
        await DatabaseManager.close()
        l.info("关闭 HTTP 共享会话")
        await AioHttpClientSessionMixin.close_http_session()
