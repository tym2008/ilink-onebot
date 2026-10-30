"""
OneBot v11 WebSocket 客户端（Reverse WS 客户端模式）

本桥接主动作为 WS 客户端连接到 OneBot 框架的反向 WS 服务端 URL（如 NoneBot2 的
``reverse_ws_urls`` / Koishi 的 ``adapter-onebot`` 反向连接端点）。与
``OneBotWSServer``（让框架连过来）互补，两种模式可同时启用。

行为：

- ``start(url, self_id, access_token=None)``：开启后台连接循环；连接断开后自动重连
  （指数退避 1s → 30s 上限）
- 连接握手：发送 ``X-Self-ID`` / ``X-Client-Role: Universal`` / 可选 ``Authorization: Bearer``
- 入站消息：视为 OneBot action 请求，反序列化 → 调 ``action_callback`` → 序列化响应回发
- 出站事件：通过 ``broadcast_event`` 主动 push（与 server 模式共享语义，事件 JSON 一致）
- 无连接时 ``broadcast_event`` 静默返回——客户端断线在协议层是正常状态，不视为错误

action 回调与 ``OneBotWSServer.set_action_callback`` 设的是同一份（``onebots/_event_dispatch`` 在
``typers/run.py`` 启动时把 callback 注册到两个 transport），保证两边收 action 行为一致。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, ClassVar

import orjson
from loguru import logger as l
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from meta_config import meta_config
from onebots.bootstrap_queue import push_pending_bootstrap_self_id
from onebots.constants import (
    CLIENT_ROLE_UNIVERSAL,
    DEFAULT_USER_AGENT,
    HEADER_CLIENT_ROLE,
    HEADER_SELF_ID,
)
from onebots.models import (
    OneBotAction,
    OneBotActionResponse,
    OneBotEventBase,
    OneBotHeartbeatStatus,
    OneBotLifecycleSubTypeEnum,
    OneBotMetaEvent,
    OneBotMetaEventTypeEnum,
    OneBotPostTypeEnum,
    OneBotRetCodeEnum,
)

if TYPE_CHECKING:
    from sqlmodels.bridge.bridge import Bridge


# 与 ws_server 同一签名（注册到 server 和 client 的是同一个 callback 实例）
ActionCallback = Callable[[OneBotAction], Awaitable[OneBotActionResponse]]


# ===== 重连退避参数 =====

_RECONNECT_INITIAL_DELAY_SECONDS: float = 1.0
"""首次重连前的延迟（秒）"""

_RECONNECT_MAX_DELAY_SECONDS: float = 30.0
"""重连延迟上限（秒）；指数退避达到此值后稳定不再加大"""

_OPEN_TIMEOUT_SECONDS: float = 10.0
"""单次 WS 握手超时（秒）"""

_BEARER_HEADER_PREFIX: str = 'Bearer '
"""Authorization header 的 Bearer 前缀（含尾部空格）"""


class OneBotWSClient:
    """
    OneBot v11 Reverse WS 客户端（纯 classmethod 单例）。

    此类永不实例化。通过 ``start(url, ...)`` 启动后台连接循环，``stop()`` 终止。
    """

    _tasks: ClassVar[dict[int, asyncio.Task[None]]] = {}
    """每个 self_id 对应的后台连接 + 重连循环任务"""

    _connections: ClassVar[dict[int, ClientConnection]] = {}
    """每个 self_id 对应的当前活动连接"""

    _action_callback: ClassVar[ActionCallback | None] = None
    """处理入站动作的回调（由 ``typers/run.py`` 在启动前注册）"""

    _session_factory: ClassVar[async_sessionmaker[AsyncSession] | None] = None
    """数据库会话工厂，用于握手时查询所有已就绪账号"""

    _url: ClassVar[str | None] = None
    """目标反向 WS URL"""

    _self_id: ClassVar[int | None] = None
    """本桥接默认的 OneBot self_id（备用）"""

    _access_token: ClassVar[str | None] = None
    """可选 Bearer token（与 server 模式共享同一份 ``meta_config.ONEBOT_ACCESS_TOKEN``）"""

    _stopping: ClassVar[bool] = False
    """优雅关闭标志；置 True 后重连循环不再尝试新连接"""

    def __new__(cls) -> 'OneBotWSClient':
        raise RuntimeError("OneBotWSClient 是纯 classmethod 单例，禁止实例化")

    @classmethod
    def set_action_callback(cls, callback: ActionCallback) -> None:
        """注册入站动作回调（在 start 之前调用）"""
        cls._action_callback = callback

    @classmethod
    def set_session_factory(cls, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """注册数据库会话工厂，用于动态探测所有已登录账号"""
        cls._session_factory = session_factory

    @classmethod
    async def start(
        cls,
        url: str,
        self_id: int,
        access_token: str | None = None,
    ) -> None:
        """
        启动后台连接 + 重连循环（多账号并发多连接）。

        :param url: 目标反向 WS URL（``ws://`` 或 ``wss://``）
        :param self_id: 本桥接的 OneBot self_id（作为兜底 ``X-Self-ID`` 头）
        :param access_token: 可选 Bearer token；若框架配置了 ``access_token`` 校验须填
        """
        cls._url = url
        cls._self_id = self_id
        cls._access_token = access_token
        cls._stopping = False

        from sqlmodels.bridge.bridge import Bridge
        target_self_ids: list[int] = []
        if cls._session_factory is not None:
            async with cls._session_factory() as session:
                active_bridges = await Bridge.get_all_active(session)
                target_self_ids = [b.self_id for b in active_bridges]

        if not target_self_ids:
            target_self_ids = [self_id]

        for sid in target_self_ids:
            existing = cls._tasks.get(sid)
            if existing is not None and not existing.done():
                continue
            cls._tasks[sid] = asyncio.create_task(cls._connect_loop(sid))
            l.success(f"OneBot v11 反向 WS 客户端已启动 (self_id={sid}): {url}")

    @classmethod
    async def stop(cls) -> None:
        """
        优雅关闭：取消所有重连循环，关闭所有连接。
        """
        cls._stopping = True
        for conn in list(cls._connections.values()):
            try:
                await conn.close()
            except Exception as e:  # noqa: BLE001
                l.warning(f"关闭反向 WS 连接时出错（已忽略）: {type(e).__name__}: {e}")
        cls._connections.clear()

        for task in list(cls._tasks.values()):
            task.cancel()
        if cls._tasks:
            _ = await asyncio.gather(*cls._tasks.values(), return_exceptions=True)
        cls._tasks.clear()
        l.info("OneBot v11 反向 WS 客户端已停止")

    @classmethod
    async def broadcast_event(cls, event: OneBotEventBase) -> None:
        """
        向当前活动连接 push 事件。若事件指定了 self_id，优先路由到该账号的专属连接。
        """
        payload = orjson.dumps(event.model_dump(exclude_none=True))
        target_self_id = getattr(event, 'self_id', None)
        target_conns: list[ClientConnection] = []
        if target_self_id is not None and target_self_id in cls._connections:
            target_conns = [cls._connections[target_self_id]]
        else:
            target_conns = list(cls._connections.values())

        if not target_conns:
            l.debug(f"反向 WS 未连接，事件未 push: post_type={event.post_type}")
            return

        for conn in target_conns:
            try:
                await conn.send(payload)
                l.debug(
                    f"反向 WS 已 push 事件: post_type={event.post_type}, "
                    f"payload={payload.decode('utf-8', errors='replace')[:400]}"
                )
            except ConnectionClosed:
                l.warning("反向 WS 连接已关闭，事件 push 失败（等待重连）")

    # ===== 内部：连接 + 重连循环 =====

    @classmethod
    async def _connect_loop(cls, self_id: int) -> None:
        """
        单个 self_id 的后台主循环：连接 → 消费消息 → 断开 → 退避 → 重连，直到 stop。
        """
        if cls._url is None:
            raise RuntimeError("OneBotWSClient.start 必须先于 _connect_loop 调用")

        delay = _RECONNECT_INITIAL_DELAY_SECONDS
        try:
            while not cls._stopping:
                try:
                    await cls._connect_once(self_id)
                    delay = _RECONNECT_INITIAL_DELAY_SECONDS
                    if cls._stopping:
                        break
                    l.warning(f"反向 WS 对端断开 (self_id={self_id})，{delay:.1f}s 后重连")
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise
                except (OSError, asyncio.TimeoutError, ConnectionClosed, InvalidStatus) as e:
                    l.warning(
                        f"反向 WS 连接 / 通信失败 (self_id={self_id}): {type(e).__name__}: {e}，"
                        f"{delay:.1f}s 后重试"
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, _RECONNECT_MAX_DELAY_SECONDS)
        except asyncio.CancelledError:
            raise

    @classmethod
    async def _connect_once(cls, self_id: int) -> None:
        """
        为指定 self_id 建立一次连接，进入读循环消费 action 请求。
        """
        assert cls._url is not None

        headers: dict[str, str] = {
            'User-Agent': DEFAULT_USER_AGENT,
            HEADER_SELF_ID: str(self_id),
            HEADER_CLIENT_ROLE: CLIENT_ROLE_UNIVERSAL,
        }
        if cls._access_token is not None:
            headers['Authorization'] = f"{_BEARER_HEADER_PREFIX}{cls._access_token}"

        async with connect(
            cls._url,
            additional_headers=headers,
            user_agent_header=DEFAULT_USER_AGENT,
            open_timeout=_OPEN_TIMEOUT_SECONDS,
            max_size=meta_config.ONEBOT_WS_MAX_SIZE,
        ) as connection:
            cls._connections[self_id] = connection
            l.success(f"反向 WS 已连接 (self_id={self_id}): {cls._url}")
            try:
                await cls._send_bootstrap_events(connection, self_id)
                await cls._consume_messages(connection, self_id)
            finally:
                cls._connections.pop(self_id, None)

    @classmethod
    async def _send_bootstrap_events(cls, connection: ClientConnection, self_id: int) -> None:
        """
        连接建立后立即发送当前 self_id 的 OneBot v11 bootstrap 元事件。
        """
        push_pending_bootstrap_self_id(self_id)
        now = int(time.time())
        events = [
            OneBotMetaEvent(
                time=now,
                self_id=self_id,
                post_type=OneBotPostTypeEnum.meta_event,
                meta_event_type=OneBotMetaEventTypeEnum.lifecycle,
                sub_type=OneBotLifecycleSubTypeEnum.connect,
                status=OneBotHeartbeatStatus(online=True, good=True),
            ),
            OneBotMetaEvent(
                time=now,
                self_id=self_id,
                post_type=OneBotPostTypeEnum.meta_event,
                meta_event_type=OneBotMetaEventTypeEnum.lifecycle,
                sub_type=OneBotLifecycleSubTypeEnum.enable,
                status=OneBotHeartbeatStatus(online=True, good=True),
            ),
            OneBotMetaEvent(
                time=now,
                self_id=self_id,
                post_type=OneBotPostTypeEnum.meta_event,
                meta_event_type=OneBotMetaEventTypeEnum.heartbeat,
                interval=15000,
                status=OneBotHeartbeatStatus(online=True, good=True),
            ),
        ]
        for event in events:
            payload = orjson.dumps(event.model_dump(exclude_none=True))
            await connection.send(payload)
            l.debug(
                f"反向 WS 已发送 bootstrap 事件 (self_id={self_id}): {event.meta_event_type}."
                f"{event.sub_type or 'heartbeat'}"
            )

    @classmethod
    async def _consume_messages(cls, connection: ClientConnection, self_id: int) -> None:
        """
        读循环：从连接接收 OneBot action 请求，调回调，发回响应。
        """
        async for raw in connection:
            if isinstance(raw, bytes):
                raw = raw.decode('utf-8')
            await cls._handle_incoming_message(connection, raw, self_id)

    @classmethod
    async def _handle_incoming_message(
        cls,
        connection: ClientConnection,
        raw: str,
        self_id: int,
    ) -> None:
        """
        处理一条入站消息（OneBot action 请求）：反序列化 → 回调 → 序列化响应。
        """
        try:
            data = orjson.loads(raw)
        except orjson.JSONDecodeError as e:
            l.warning(f"反向 WS 收到非法 JSON: {e}")
            response = OneBotActionResponse.failed(retcode=OneBotRetCodeEnum.BAD_REQUEST)
            await connection.send(orjson.dumps(response.model_dump(exclude_none=True)))
            return

        try:
            action = OneBotAction.model_validate(data)
        except Exception as e:  # noqa: BLE001
            l.warning(f"反向 WS 收到非法动作请求: {e}")
            echo = data.get('echo') if isinstance(data, dict) else None
            response = OneBotActionResponse.failed(
                retcode=OneBotRetCodeEnum.BAD_REQUEST,
                echo=echo,
            )
            await connection.send(orjson.dumps(response.model_dump(exclude_none=True)))
            return

        # 标记当前连接专属的 self_id，保证动作精准由对应账号执行
        if action.self_id is None:
            action.self_id = self_id
        if isinstance(action.params, dict) and 'self_id' not in action.params:
            action.params['self_id'] = self_id

        if cls._action_callback is None:
            l.error("OneBotWSClient 未注册 action_callback，无法处理入站动作")
            response = OneBotActionResponse.failed(
                retcode=OneBotRetCodeEnum.NOT_FOUND,
                echo=action.echo,
            )
        else:
            response = await cls._action_callback(action)

        await connection.send(orjson.dumps(response.model_dump(exclude_none=True)))
