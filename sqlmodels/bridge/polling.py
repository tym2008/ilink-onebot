"""
长轮询循环管理器

把 ``Bridge.poll_updates()`` + ``Bridge.handle_inbound_message()`` + 事件广播
组装成一个可启停的 asyncio.Task。

错误处理策略（按 ServerConfig 行为可调）：

- 网络错误 / iLink 临时错误：指数退避后重试，连续失败超阈值时停止并告警
- iLink 会话过期（errcode -14）：调用 ``Bridge.handle_session_expired`` 转入
  expired 状态后立刻调用 ``Bridge.run_qrcode_login`` 自动重登（在终端展示新 QR
  并阻塞等待管理员扫码）。成功 → 继续轮询；超时 / 失败 → 退出循环
- CancelledError：优雅退出，正常关闭路径

依赖注入：``session_factory`` 由调用方（``typers/run.py``）显式传入，
循环内每轮重新 open 一个 session 处理一批消息然后释放，避免长生命周期 session。

session 边界设计：
- 每个独立工作单元（一轮轮询、一次错误判定）open 一个 session，session 内完成
  全部 ORM 操作 + 字段访问
- 跨 session 不传递 ORM 实例引用（detached 后访问字段会触发 ``DetachedInstanceError``）
- ``_TickOutcome`` 只持有标量决策值（``next_interval_seconds`` / ``backoff_seconds``），
  ``_run_forever`` 中需要 config 字段时单独 open session 重新 ``ServerConfig.get_instance(session)``
"""
import asyncio
import time
from typing import ClassVar

from loguru import logger as l
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from meta_config import meta_config
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
    iLink 长轮询循环管理器（纯 classmethod 单例）。

    通过 ``start(session_factory)`` 启动后台任务，``stop()`` 优雅取消并等待完成。
    """

    _task: ClassVar[asyncio.Task[None] | None] = None

    def __new__(cls) -> 'PollingLoop':
        raise RuntimeError("PollingLoop 是纯 classmethod 单例，禁止实例化")

    @classmethod
    async def start(cls, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """启动轮询任务（幂等：已运行则直接返回）"""
        if cls._task is not None:
            l.warning("PollingLoop 已在运行，幂等返回")
            return
        cls._task = asyncio.create_task(cls._run_forever(session_factory))  # pyright: ignore[reportAttributeAccessIssue]
        l.success("iLink 长轮询循环已启动")

    @classmethod
    async def stop(cls) -> None:
        """取消轮询任务并等待清理"""
        if cls._task is None:
            return
        _ = cls._task.cancel()
        try:
            await cls._task
        except asyncio.CancelledError:
            pass
        cls._task = None  # pyright: ignore[reportAttributeAccessIssue]
        l.info("iLink 长轮询循环已停止")

    @classmethod
    async def _run_forever(cls, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """
        长轮询主循环。

        每轮：``_tick`` → 检查 outcome → 按需睡眠或退避。

        错误阈值判定时单独 open session 获取 fresh ``ServerConfig`` 实例——避免传递
        detached ORM 引用。
        """
        consecutive_errors = 0
        try:
            while True:
                outcome = await cls._tick(session_factory)
                if outcome.fatal:
                    l.error("轮询循环检测到致命错误，停止")
                    return
                if outcome.error:
                    consecutive_errors += 1
                    async with session_factory() as session:
                        config = await ServerConfig.get_instance(session)
                        if consecutive_errors >= config.max_consecutive_poll_errors:
                            l.error(
                                f"连续 {consecutive_errors} 次轮询错误，超过阈值 "
                                f"{config.max_consecutive_poll_errors}，停止循环"
                            )
                            return
                    delay = outcome.backoff_seconds
                    l.warning(f"轮询错误第 {consecutive_errors} 次，{delay:.1f}s 后重试")
                    await asyncio.sleep(delay)
                else:
                    consecutive_errors = 0
                    await asyncio.sleep(outcome.next_interval_seconds)
        except asyncio.CancelledError:
            raise

    @classmethod
    async def _tick(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> '_TickOutcome':
        """
        单轮轮询：加载 config + bridge → 长轮询 → 处理消息 → 广播事件。

        ``ServerConfig`` 的所有字段访问都在 session 内完成，标量值在 session 关闭前
        capture 到局部变量。事件广播在 session 关闭后进行（避免 WS 阻塞期间持有
        DB 连接）。
        """
        try:
            async with session_factory() as session:
                config = await ServerConfig.get_instance(session)
                bridge = await Bridge.get_instance(session)

                # 未登录或过期：停止循环（不应该发生，run.py 启动前已校验）
                if bridge.state != BridgeStateEnum.running or bridge.bot_token is None:
                    l.warning(f"Bridge 状态变为 {bridge.state}，停止轮询")
                    return _TickOutcome(fatal=True)

                messages = await bridge.poll_updates(session)

                # 逐条处理：UPSERT context_token + 转换为 OneBot 事件 + 广播
                events_to_broadcast = []
                for msg in messages:
                    event = await bridge.handle_inbound_message(session, msg)
                    if event is not None:
                        events_to_broadcast.append(event)

                # 上述步骤多次 commit 后，之前加载的 config 实例已过期——重新
                # ``get_instance`` 拿 fresh config，在 session 内 capture 标量决策值
                config = await ServerConfig.get_instance(session)
                next_interval_seconds = config.poll_interval_seconds

            # 释放 session 之后再广播（避免 WS 发送阻塞期间持有 DB 连接）。
            # 同时向 server 模式（被动连接的框架）和 client 模式（主动连出的框架）push 事件。
            for event in events_to_broadcast:
                await OneBotWSServer.broadcast_event(event)
                await OneBotWSClient.broadcast_event(event)

            return _TickOutcome(
                error=False,
                next_interval_seconds=next_interval_seconds,
            )

        except ILinkSessionExpiredError:
            return await cls._handle_session_expired_with_relogin(session_factory)

        except ILinkAPIError as e:
            l.warning(f"iLink API 错误: errcode={e.errcode}, errmsg={e.errmsg}")
            return _TickOutcome(error=True, backoff_seconds=5.0)

        except Exception as e:
            l.exception(f"轮询循环异常: {e}")
            return _TickOutcome(error=True, backoff_seconds=5.0)

    @classmethod
    async def _handle_session_expired_with_relogin(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> '_TickOutcome':
        """
        会话过期处理 + 自动重登。

        1. 调用 ``Bridge.handle_session_expired`` 转入 expired 状态
        2. 广播 OneBot lifecycle ``disable`` 事件让 framework 感知桥接下线
        3. 调用 ``Bridge.run_qrcode_login`` 阻塞等待管理员扫码（最多 5 分钟）
        4. 成功 → 广播 lifecycle ``connect`` 事件 → 返回 success 让循环继续轮询
        5. 失败 / 超时 → 桥接保持 expired，返回 fatal 让循环退出
        """
        l.warning("iLink 会话过期，准备自动重登")
        async with session_factory() as session:
            config = await ServerConfig.get_instance(session)
            bridge = await Bridge.get_instance(session)
            await bridge.handle_session_expired(session, config)

        # 广播 disable 让 framework 感知桥接下线
        await cls._broadcast_lifecycle(OneBotLifecycleSubTypeEnum.disable)

        # 触发自动重登（阻塞 ≤ QRCODE_TOTAL_TIMEOUT_SECONDS）
        async with session_factory() as session:
            fresh_bridge = await Bridge.get_instance(session)
        success = await fresh_bridge.run_qrcode_login(
            session_factory,
            prompt_message=(
                "iLink 会话已过期，请用微信扫描以下 QR 码重新登录"
                "（5 分钟内完成；不扫描则桥接保持 expired 状态）"
            ),
        )

        if success:
            await cls._broadcast_lifecycle(OneBotLifecycleSubTypeEnum.connect)
            return _TickOutcome(error=False, next_interval_seconds=0.0)

        # 重登失败 / 超时：桥接保持 expired，循环退出
        return _TickOutcome(fatal=True)

    @classmethod
    async def _broadcast_lifecycle(
        cls,
        sub_type: OneBotLifecycleSubTypeEnum,
    ) -> None:
        """构造 + 广播 OneBot v11 lifecycle meta_event 给 server + client 两种 transport"""
        event = OneBotMetaEvent(
            time=int(time.time()),
            self_id=meta_config.ONEBOT_SELF_ID,
            meta_event_type=OneBotMetaEventTypeEnum.lifecycle,
            sub_type=sub_type,
        )
        await OneBotWSServer.broadcast_event(event)
        await OneBotWSClient.broadcast_event(event)
        l.info(f"已广播 OneBot lifecycle 事件: sub_type={sub_type.value}")


class _TickOutcome:
    """
    单轮轮询的结果对象。

    只持有标量决策值；ORM 实例引用不跨 session 边界，需要 config / bridge 时
    调用方应自行 ``ServerConfig.get_instance(new_session)``。
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
