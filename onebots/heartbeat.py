"""
OneBot v11 心跳事件发射器

定时通过 OneBotWSServer 广播 heartbeat meta_event。OneBot 框架依赖心跳判断
桥接是否存活，间隔过长会触发框架侧的连接超时重连。

心跳间隔由 ServerConfig.heartbeat_interval_seconds 决定（运行时可调）。
状态来自 Bridge.state（running 才算 online + good）。

设计为纯 classmethod 单例，与 OneBotWSServer 风格一致。

:author: Foxerine <https://github.com/Foxerine>
"""
import asyncio
import time
from typing import ClassVar

from loguru import logger as l
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from meta_config import meta_config
from onebots.models import (
    OneBotHeartbeatStatus,
    OneBotMetaEvent,
    OneBotMetaEventTypeEnum,
)
from onebots.ws_client import OneBotWSClient
from onebots.ws_server import OneBotWSServer
from sqlmodels.bridge.enums import BridgeStateEnum
from sqlmodels.server_config import ServerConfig


class OneBotHeartbeat:
    """
    OneBot v11 心跳广播器（纯 classmethod 单例）。

    通过 `start(session_factory)` 启动后台任务，每 `heartbeat_interval_seconds` 秒
    广播一次 heartbeat meta_event。`stop()` 取消任务并等待清理。
    """

    _task: ClassVar[asyncio.Task[None] | None] = None

    def __new__(cls) -> 'OneBotHeartbeat':
        raise RuntimeError("OneBotHeartbeat 是纯 classmethod 单例，禁止实例化")

    @classmethod
    async def start(cls, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """启动心跳后台任务（幂等：已运行则直接返回）"""
        if cls._task is not None:
            l.warning("OneBotHeartbeat 已在运行，幂等返回")
            return
        cls._task = asyncio.create_task(cls._run_forever(session_factory))  # pyright: ignore[reportAttributeAccessIssue]
        l.success("OneBot 心跳任务已启动")

    @classmethod
    async def stop(cls) -> None:
        """取消心跳任务并等待清理"""
        if cls._task is None:
            return
        _ = cls._task.cancel()
        try:
            await cls._task
        except asyncio.CancelledError:
            pass
        cls._task = None  # pyright: ignore[reportAttributeAccessIssue]
        l.info("OneBot 心跳任务已停止")

    @classmethod
    async def _run_forever(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """
        心跳循环：每 interval 秒构造并广播一次心跳事件。

        每次循环都重新读 ServerConfig 和 Bridge 状态，让运行时配置变更立即生效。
        """
        try:
            from sqlmodels.bridge.bridge import Bridge

            while True:
                async with session_factory() as session:
                    config = await ServerConfig.get_instance(session)
                    active_bridges = await Bridge.get_all_active(session)
                    interval_seconds = config.heartbeat_interval_seconds

                target_self_ids: list[tuple[int, bool]] = (
                    [(b.self_id, b.state == BridgeStateEnum.running) for b in active_bridges]
                    if active_bridges
                    else [(meta_config.ONEBOT_SELF_ID, False)]
                )

                now = int(time.time())
                for self_id, online in target_self_ids:
                    event = OneBotMetaEvent(
                        time=now,
                        self_id=self_id,
                        meta_event_type=OneBotMetaEventTypeEnum.heartbeat,
                        interval=interval_seconds * 1000,  # OneBot 规范要求毫秒
                        status=OneBotHeartbeatStatus(online=online, good=online),
                    )
                    await OneBotWSServer.broadcast_event(event)
                    await OneBotWSClient.broadcast_event(event)

                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            # 优雅退出：在 stop() 被显式取消
            raise
        except Exception as e:
            # 心跳循环不应该崩溃，记录后继续（除非 CancelledError）
            l.exception(f"OneBot 心跳循环出错: {e}")
