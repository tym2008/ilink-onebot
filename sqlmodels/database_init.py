"""
启动期默认数据初始化

`DatabaseManager.init()` 完成 Alembic 迁移之后调用 `init_default_data()`，
确保所有单例表（Bridge / ServerConfig）的固定 ID 行已经存在。

为什么需要这步：
- Bridge.get_instance() / ServerConfig.get_instance() 在首次访问时会创建并 commit
- 在同一 session 中连续访问两个单例会有"第二个 commit 让第一个实例过期"的问题
- 启动时预先一次性创建，运行时 get_instance() 始终命中已存在的行，无 commit 副作用
"""
from loguru import logger as l
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from sqlmodels.bridge import Bridge
from sqlmodels.server_config import ServerConfig


async def init_default_data(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """
    确保所有单例表的固定行存在。

    每个单例用独立 session，避免互相 commit 触发过期。幂等：已存在的行不重建。
    """
    async with session_factory() as session:
        _ = await Bridge.get_instance(session)
    async with session_factory() as session:
        _ = await ServerConfig.get_instance(session)
    l.info("默认数据初始化完成（Bridge / ServerConfig 单例已就绪）")
