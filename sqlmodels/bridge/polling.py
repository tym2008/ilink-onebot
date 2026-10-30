"""
长轮询循环管理器（多账号并发支持）

为每一个已登录的 Bridge 账号维护独立的轮询 worker（asyncio.Task），
把 ``Bridge.poll_updates()`` + ``Bridge.handle_inbound_message()`` + 事件广播
解耦并发执行。单个账号的会话过期或网络抖动不影响其他账号正常工作。

错误处理策略（按 ServerConfig 行为可调）：
- 网络错误 / iLink 临时错误：指数退避后重试，连续失败超阈值时停止该账号 worker 并告警
- iLink 会话过期（errcode -14）：调用 ``Bridge.handle_session_expired`` 转入
  expired 状态并广播 lifecycle disable；尝试自动扫码重登（阻塞等待扫码）
- CancelledError：优雅退出，正常关闭路径

依赖注入：``session_factory`` 由调用方（``typers/run.py``）显式传入，
每个独立工作单元（一轮轮询、一次状态更新）打开独立 session，避免长生命周期 session。
"""
import asyncio
from contextlib import suppress
import time
from typing import ClassVar

from loguru import logger as l
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from onebots.models import (
    OneBotLifecycleSubTypeEnum,
    OneBotMetaEvent,
    OneBotMetaEventTypeEnum,
)
from onebots.ws_client import OneBotWSClient
from onebots.ws_server import OneBotWSServer
from sqlmodels.bridge.bridge import Bridge
from sqlmodels.bridge.enums import BridgeStateEnum
from sqlmodels.bridge.exceptions import ILinkAPIError, ILinkSessionExpiredError
from sqlmodels.server_config import ServerConfig


class PollingLoop:
    """
    iLink 长轮询循环管理器（纯 classmethod 单例，支持多账号并发 worker）。

    通过 ``start(session_factory)`` 启动所有已就绪账号的后台轮询任务，
    ``stop()`` 优雅取消并等待所有 worker 清理完成。
    """

    _workers: ClassVar[dict[int, asyncio.Task[None]]] = {}
    """每个活跃 Bridge ID 对应的后台轮询任务"""

    def __new__(cls) -> 'PollingLoop':
        raise RuntimeError("PollingLoop 是纯 classmethod 单例，禁止实例化")

    @classmethod
    async def start(cls, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """
        启动所有已就绪账号的轮询 worker。
        """
        async with session_factory() as session:
            active_bridges = await Bridge.get_all_active(session)

        if not active_bridges:
            l.warning("未检测到任何已就绪的账号，请先通过 `ilink-onebot login` 登录至少一个账号")
            return

        for bridge in active_bridges:
            if bridge.id is not None:
                await cls.start_worker(bridge.id, session_factory)

        l.success(f"iLink 长轮询循环已启动（当前活跃 worker 数: {len(cls._workers)}）")

    @classmethod
    async def start_worker(
        cls,
        bridge_id: int,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """
        为指定 bridge_id 启动独立的后台轮询任务（幂等）。
        """
        existing_task = cls._workers.get(bridge_id)
        if existing_task is not None and not existing_task.done():
            l.warning(f"Bridge [id={bridge_id}] 轮询 worker 已在运行，幂等返回")
            return

        cls._workers[bridge_id] = asyncio.create_task(
            cls._run_bridge_worker(bridge_id, session_factory)
        )
        l.info(f"Bridge [id={bridge_id}] 轮询 worker 已启动")

    @classmethod
    async def stop_worker(cls, bridge_id: int) -> None:
        """
        停止指定 bridge_id 的轮询 worker。
        """
        task = cls._workers.pop(bridge_id, None)
        if task is None:
            return
        _ = task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        l.info(f"Bridge [id={bridge_id}] 轮询 worker 已停止")

    @classmethod
    async def stop(cls) -> None:
        """
        取消所有轮询任务并等待清理。
        """
        if not cls._workers:
            return

        tasks = list(cls._workers.values())
        for task in tasks:
            _ = task.cancel()

        _ = await asyncio.gather(*tasks, return_exceptions=True)
        cls._workers.clear()
        l.info("iLink 所有长轮询 worker 已停止")

    @classmethod
    async def _run_bridge_worker(
        cls,
        bridge_id: int,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """
        单个账号的长轮询工作协程。
        """
        consecutive_errors = 0
        try:
            while True:
                outcome = await cls._tick(bridge_id, session_factory)
                if outcome.fatal:
                    l.error(f"Bridge [id={bridge_id}] 轮询检测到致命错误，停止此 worker")
                    cls._workers.pop(bridge_id, None)
                    return
                if outcome.error:
                    consecutive_errors += 1
                    async with session_factory() as session:
                        config = await ServerConfig.get_instance(session)
                        if consecutive_errors >= config.max_consecutive_poll_errors:
                            l.error(
                                f"Bridge [id={bridge_id}] 连续 {consecutive_errors} 次轮询错误，"
                                f"超过阈值 {config.max_consecutive_poll_errors}，停止循环"
                            )
                            cls._workers.pop(bridge_id, None)
                            return
                    delay = outcome.backoff_seconds
                    l.warning(
                        f"Bridge [id={bridge_id}] 轮询错误第 {consecutive_errors} 次，"
                        f"{delay:.1f}s 后重试"
                    )
                    await asyncio.sleep(delay)
                else:
                    consecutive_errors = 0
                    await asyncio.sleep(outcome.next_interval_seconds)
        except asyncio.CancelledError:
            raise

    @classmethod
    async def _tick(
        cls,
        bridge_id: int,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> '_TickOutcome':
        """
        单个账号的单轮轮询。
        """
        try:
            async with session_factory() as session:
                config = await ServerConfig.get_instance(session)
                bridge = await Bridge.get(session, col(Bridge.id) == bridge_id)

                if bridge is None or bridge.state != BridgeStateEnum.running or bridge.bot_token is None:
                    l.warning(
                        f"Bridge [id={bridge_id}] 状态为 "
                        f"{bridge.state if bridge else 'None'}，停止轮询"
                    )
                    return _TickOutcome(fatal=True)

                messages = await bridge.poll_updates(session)

                events_to_broadcast = []
                for msg in messages:
                    event = await bridge.handle_inbound_message(session, msg)
                    if event is not None:
                        events_to_broadcast.append(event)

                config = await ServerConfig.get_instance(session)
                next_interval_seconds = config.poll_interval_seconds

            for event in events_to_broadcast:
                await OneBotWSServer.broadcast_event(event)
                await OneBotWSClient.broadcast_event(event)

            return _TickOutcome(
                error=False,
                next_interval_seconds=next_interval_seconds,
            )

        except ILinkSessionExpiredError:
            return await cls._handle_session_expired_with_relogin(bridge_id, session_factory)

        except ILinkAPIError as e:
            l.warning(f"Bridge [id={bridge_id}] iLink API 错误: errcode={e.errcode}, errmsg={e.errmsg}")
            return _TickOutcome(error=True, backoff_seconds=5.0)

        except Exception as e:
            l.exception(f"Bridge [id={bridge_id}] 轮询循环异常: {e}")
            return _TickOutcome(error=True, backoff_seconds=5.0)

    @classmethod
    async def _handle_session_expired_with_relogin(
        cls,
        bridge_id: int,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> '_TickOutcome':
        """
        单个账号会话过期处理 + 自动重登。
        """
        l.warning(f"Bridge [id={bridge_id}] iLink 会话过期，准备自动重登")
        async with session_factory() as session:
            config = await ServerConfig.get_instance(session)
            bridge = await Bridge.get(session, col(Bridge.id) == bridge_id)
            if bridge is None:
                return _TickOutcome(fatal=True)
            await bridge.handle_session_expired(session, config)
            self_id = bridge.self_id

        # 广播 disable 让 framework 感知桥接下线
        await cls._broadcast_lifecycle(OneBotLifecycleSubTypeEnum.disable, self_id)

        # 触发自动重登（阻塞 ≤ QRCODE_TOTAL_TIMEOUT_SECONDS）
        async with session_factory() as session:
            fresh_bridge = await Bridge.get(session, col(Bridge.id) == bridge_id)
            if fresh_bridge is None:
                return _TickOutcome(fatal=True)

        success = await fresh_bridge.run_qrcode_login(
            session_factory,
            prompt_message=(
                f"iLink 账号 '{fresh_bridge.account_name}' (self_id={fresh_bridge.self_id}) "
                f"会话已过期，请用微信扫描以下 QR 码重新登录（5 分钟内完成）"
            ),
        )

        if success:
            await cls._broadcast_lifecycle(OneBotLifecycleSubTypeEnum.connect, fresh_bridge.self_id)
            await cls._broadcast_lifecycle(OneBotLifecycleSubTypeEnum.enable, fresh_bridge.self_id)
            return _TickOutcome(error=False, next_interval_seconds=0.0)

        # 重登失败 / 超时：保持 expired，循环退出
        return _TickOutcome(fatal=True)

    @classmethod
    async def _broadcast_lifecycle(
        cls,
        sub_type: OneBotLifecycleSubTypeEnum,
        self_id: int,
    ) -> None:
        """构造 + 广播 OneBot v11 lifecycle meta_event 给 server + client 两种 transport"""
        event = OneBotMetaEvent(
            time=int(time.time()),
            self_id=self_id,
            meta_event_type=OneBotMetaEventTypeEnum.lifecycle,
            sub_type=sub_type,
        )
        await OneBotWSServer.broadcast_event(event)
        await OneBotWSClient.broadcast_event(event)
        l.info(f"已广播 OneBot lifecycle 事件: sub_type={sub_type.value}, self_id={self_id}")


class _TickOutcome:
    """
    单轮轮询的结果对象。
    """

    fatal: bool
    """是否致命错误（停止循环）"""

    error: bool
    """是否本轮失败（需要退避重试）"""

    next_interval_seconds: float
    """下一轮的延迟（仅成功路径有意义）"""

    backoff_seconds: float
    """失败时的退避延迟（指数退避起点，循环内部会按需放大）"""

    def __init__(
        self,
        *,
        fatal: bool = False,
        error: bool = False,
        next_interval_seconds: float = 0.5,
        backoff_seconds: float = 5.0,
    ) -> None:
        self.fatal = fatal
        self.error = error
        self.next_interval_seconds = next_interval_seconds
        self.backoff_seconds = backoff_seconds
