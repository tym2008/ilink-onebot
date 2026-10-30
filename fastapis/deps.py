"""
FastAPI 依赖注入

提供给路由函数注入会话、Bridge、ServerConfig 等单例的 TypeAlias。

设计原则：单例由依赖注入获取，避免路由内部当场查询（性能 + 测试可注入 mock）。
Bridge / ServerConfig 都走 sqlmodel-ext 的 `get_instance(session)`，
对 SQLite 单条主键查询而言成本可以接受。
"""
from typing import Annotated

from fastapi import Depends
from sqlmodel.ext.asyncio.session import AsyncSession

from sqlmodels.bridge import Bridge
from sqlmodels.database_manager import DatabaseManager
from sqlmodels.server_config import ServerConfig

SessionDep = Annotated[AsyncSession, Depends(DatabaseManager.get_session)]
"""为 FastAPI 路由注入一个 AsyncSession（请求级生命周期）"""


async def _get_bridge(session: SessionDep) -> Bridge:
    """从 session 加载 Bridge 实例（优先活跃账号，回退 default）"""
    active = await Bridge.get_all_active(session)
    if active:
        return active[0]
    return await Bridge.get_instance(session)


BridgeDep = Annotated[Bridge, Depends(_get_bridge)]
"""为 FastAPI 路由注入 Bridge 单例"""


async def _get_server_config(session: SessionDep) -> ServerConfig:
    """从 session 加载 ServerConfig 单例"""
    return await ServerConfig.get_instance(session)


ServerConfigDep = Annotated[ServerConfig, Depends(_get_server_config)]
"""为 FastAPI 路由注入 ServerConfig 单例"""
