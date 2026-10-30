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
import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import ClassVar

import orjson
from loguru import logger as l
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from meta_config import meta_config
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

    _task: ClassVar[asyncio.Task[None] | None] = None
    """后台连接 + 重连循环任务"""

    _connection: ClassVar[ClientConnection | None] = None
    """当前活动连接（None 表示未连接 / 重连中）"""

    _action_callback: ClassVar[ActionCallback | None] = None
    """处理入站动作的回调（由 ``typers/run.py`` 在启动前注册）"""

    _url: ClassVar[str | None] = None
    """目标反向 WS URL"""

    _self_id: ClassVar[int | None] = None
    """本桥接的 OneBot self_id（作为 X-Self-ID 头）"""

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
    async def start(
        cls,
        url: str,
        self_id: int,
        access_token: str | None = None,
    ) -> None:
        """
        启动后台连接 + 重连循环。

        :param url: 目标反向 WS URL（``ws://`` 或 ``wss://``）
        :param self_id: 本桥接的 OneBot self_id（作为 ``X-Self-ID`` 头）
        :param access_token: 可选 Bearer token；若框架配置了 ``access_token`` 校验须填
        """
        if cls._task is not None:
            l.warning("OneBotWSClient 已在运行，幂等返回")
            return

        cls._url = url
        cls._self_id = self_id
        cls._access_token = access_token
        cls._stopping = False
        cls._task = asyncio.create_task(cls._connect_loop())  # pyright: ignore[reportAttributeAccessIssue]
        l.success(f"OneBot v11 反向 WS 客户端已启动: {url}")

    @classmethod
    async def stop(cls) -> None:
        """
        优雅关闭：取消重连循环，关闭当前连接（如有）。
        """
        if cls._task is None:
            return

        cls._stopping = True
        connection = cls._connection
        if connection is not None:
            try:
                await connection.close()
            except Exception as e:  # noqa: BLE001 — close 阶段任何错都该 swallow + log，不影响后续 cleanup
                l.warning(f"关闭反向 WS 连接时出错（已忽略）: {type(e).__name__}: {e}")

        _ = cls._task.cancel()
        try:
            await cls._task
        except asyncio.CancelledError:
            pass
        cls._task = None  # pyright: ignore[reportAttributeAccessIssue]
        cls._connection = None  # pyright: ignore[reportAttributeAccessIssue]
        l.info("OneBot v11 反向 WS 客户端已停止")

    @classmethod
    async def broadcast_event(cls, event: OneBotEventBase) -> None:
        """
        向当前活动连接 push 事件。

        未连接（``_connection is None`` 或已关闭）时静默返回——客户端模式下断线
        是协议层的正常状态（连接重建后下一条事件会自然送达）。
        """
        connection = cls._connection
        if connection is None:
            l.debug(f"反向 WS 未连接，事件未 push: post_type={event.post_type}")
            return
        payload = orjson.dumps(event.model_dump(exclude_none=True))
        try:
            await connection.send(payload)
            l.debug(
                f"反向 WS 已 push 事件: post_type={event.post_type}, "
                f"payload={payload.decode('utf-8', errors='replace')[:400]}"
            )
        except ConnectionClosed:
            l.warning("反向 WS 连接已关闭，事件 push 失败（等待重连）")

    # ===== 内部：连接 + 重连循环 =====

    @classmethod
    async def _connect_loop(cls) -> None:
        """
        后台主循环：连接 → 消费消息 → 断开 → 退避 → 重连，直到 stop。

        每次进入连接尝试，``delay`` 从 ``_RECONNECT_INITIAL_DELAY_SECONDS`` 开始；
        每次连接失败 / 异常断开后翻倍直到 ``_RECONNECT_MAX_DELAY_SECONDS`` 上限。
        成功握手后 ``delay`` 重置为初始值。
        """
        if cls._url is None or cls._self_id is None:
            raise RuntimeError("OneBotWSClient.start 必须先于 _connect_loop 调用")

        delay = _RECONNECT_INITIAL_DELAY_SECONDS
        try:
            while not cls._stopping:
                try:
                    await cls._connect_once()
                    # 正常退出（连接被对端关闭）：重置退避后立即重连
                    delay = _RECONNECT_INITIAL_DELAY_SECONDS
                    if cls._stopping:
                        break
                    l.warning(f"反向 WS 对端断开，{delay:.1f}s 后重连")
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise
                except (OSError, asyncio.TimeoutError, ConnectionClosed, InvalidStatus) as e:
                    l.warning(
                        f"反向 WS 连接 / 通信失败: {type(e).__name__}: {e}，"
                        f"{delay:.1f}s 后重试"
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, _RECONNECT_MAX_DELAY_SECONDS)
        except asyncio.CancelledError:
            raise

    @classmethod
    async def _connect_once(cls) -> None:
        """
        建立一次连接，进入读循环消费 action 请求。

        正常返回 = 对端关闭连接（应自动重连）。
        异常抛出 = 连接 / 通信错误（让外层退避后重连）。
        """
        assert cls._url is not None and cls._self_id is not None  # checked in _connect_loop

        headers: dict[str, str] = {
            'User-Agent': DEFAULT_USER_AGENT,
            HEADER_SELF_ID: str(cls._self_id),
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
            cls._connection = connection  # pyright: ignore[reportAttributeAccessIssue]
            l.success(f"反向 WS 已连接: {cls._url}")
            try:
                await cls._send_bootstrap_events(connection)
                await cls._consume_messages(connection)
            finally:
                cls._connection = None  # pyright: ignore[reportAttributeAccessIssue]

    @classmethod
    async def _send_bootstrap_events(cls, connection: ClientConnection) -> None:
        """
        连接建立后立即发送 OneBot v11 bootstrap 元事件。

        按 OneBot 规范和常见框架（如 TRSS-Yunzai / NoneBot2）预期，依序发送：
        1. lifecycle: connect
        2. lifecycle: enable
        3. heartbeat（附带当前状态）
        """
        if cls._self_id is None:
            return

        now = int(time.time())
        events = [
            OneBotMetaEvent(
                time=now,
                self_id=cls._self_id,
                post_type=OneBotPostTypeEnum.meta_event,
                meta_event_type=OneBotMetaEventTypeEnum.lifecycle,
                sub_type=OneBotLifecycleSubTypeEnum.connect,
                status=OneBotHeartbeatStatus(online=True, good=True),
            ),
            OneBotMetaEvent(
                time=now,
                self_id=cls._self_id,
                post_type=OneBotPostTypeEnum.meta_event,
                meta_event_type=OneBotMetaEventTypeEnum.lifecycle,
                sub_type=OneBotLifecycleSubTypeEnum.enable,
                status=OneBotHeartbeatStatus(online=True, good=True),
            ),
            OneBotMetaEvent(
                time=now,
                self_id=cls._self_id,
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
                f"反向 WS 已发送 bootstrap 事件: {event.meta_event_type}."
                f"{event.sub_type or 'heartbeat'}"
            )

    @classmethod
    async def _consume_messages(cls, connection: ClientConnection) -> None:
        """
        读循环：从连接接收 OneBot action 请求，调回调，发回响应。

        所有错误都包装为 ``OneBotActionResponse.failed`` 返回，让单条非法消息不
        断开连接；只有 ``ConnectionClosed`` 会让循环退出（外层据此重连）。
        """
        async for raw in connection:
            if isinstance(raw, bytes):
                raw = raw.decode('utf-8')
            await cls._handle_incoming_message(connection, raw)

    @classmethod
    async def _handle_incoming_message(
        cls,
        connection: ClientConnection,
        raw: str,
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
        except Exception as e:  # noqa: BLE001 — pydantic 校验失败种类多，统一拦
            l.warning(f"反向 WS 收到非法动作请求: {e}")
            echo = data.get('echo') if isinstance(data, dict) else None
            response = OneBotActionResponse.failed(
                retcode=OneBotRetCodeEnum.BAD_REQUEST,
                echo=echo,
            )
            await connection.send(orjson.dumps(response.model_dump(exclude_none=True)))
            return

        if cls._action_callback is None:
            l.error("OneBotWSClient 未注册 action_callback，无法处理入站动作")
            response = OneBotActionResponse.failed(
                retcode=OneBotRetCodeEnum.NOT_FOUND,
                echo=action.echo,
            )
        else:
            response = await cls._action_callback(action)

        await connection.send(orjson.dumps(response.model_dump(exclude_none=True)))
