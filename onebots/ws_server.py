"""
OneBot v11 WebSocket 服务端（Reverse WS 服务端模式）

本桥接扮演 OneBot v11 实现的角色，监听 WebSocket 连接。NoneBot2 等框架
按 Reverse WS 客户端模式连接到本服务端。

架构：
- **连接管理**：维护已连接的 OneBot 框架集合，事件广播到所有连接
- **认证**：连接时校验 Bearer token 或 access_token query param
- **双向通信**：本端推事件（消息事件、心跳），框架推动作（send_msg 等）
- **优雅关闭**：stop() 取消所有任务，关闭所有连接，等待清理完成

纯 classmethod 单例模式（项目规范）：所有方法操作 ClassVar 状态，
通过 `OneBotWSServer.start(...)` / `OneBotWSServer.stop()` 控制生命周期。

事件广播：`OneBotWSServer.broadcast_event(event)` 把事件 JSON 序列化后
推送给所有当前连接的客户端。

动作处理：通过 `set_action_handler(callback)` 注册回调，每收到动作 JSON 时
反序列化为 OneBotAction → 调用回调 → 序列化响应返回客户端。

:author: Foxerine <https://github.com/Foxerine>
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import parse_qs, urlparse

import orjson
from loguru import logger as l
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from meta_config import meta_config
from onebots.action_handler import push_pending_bootstrap_self_id
from onebots.constants import BEARER_PREFIX, QUERY_ACCESS_TOKEN_KEY
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

# 动作回调签名：接收 action，返回响应（异步）
ActionCallback = Callable[[OneBotAction], Awaitable[OneBotActionResponse]]


def _extract_token(request: Request) -> str | None:
    """
    从 HTTP Authorization 头或 query string 提取 access_token。

    OneBot v11 规范允许两种方式：
    - `Authorization: Bearer <token>` 头部
    - `?access_token=<token>` 查询参数

    :return: 提取到的 token，未提供时返回 None
    """
    auth_header = request.headers.get('Authorization')
    if auth_header is not None and auth_header.startswith(BEARER_PREFIX):
        return auth_header[len(BEARER_PREFIX):]

    query_string = urlparse(request.path).query
    if query_string:
        params = parse_qs(query_string)
        token_values = params.get(QUERY_ACCESS_TOKEN_KEY)
        if token_values:
            return token_values[0]

    return None


class OneBotWSServer:
    """
    OneBot v11 Reverse WS 服务端（纯 classmethod 单例）。

    此类永不实例化。通过 start/stop 管理生命周期，通过 broadcast_event 推送事件。
    """

    _server_task: ClassVar[asyncio.Task[None] | None] = None
    """监听任务（serve() 的 wrapper），None 表示未启动"""

    _connections: ClassVar[set[ServerConnection]] = set()
    """当前所有连接的 OneBot 框架（事件广播目标）"""

    _action_callback: ClassVar[ActionCallback | None] = None
    """处理入站动作的回调"""

    _session_factory: ClassVar[async_sessionmaker[AsyncSession] | None] = None
    """数据库会话工厂，用于握手时查询所有已就绪账号"""

    _self_id: ClassVar[int | None] = None
    """本桥接的 OneBot self_id（启动时由调用方传入，作为 X-Self-ID 头）"""

    _expected_token: ClassVar[str | None] = None
    """期望的 access_token（None 则不校验）"""

    def __new__(cls) -> 'OneBotWSServer':
        raise RuntimeError("OneBotWSServer 是纯 classmethod 单例，禁止实例化")

    @classmethod
    def set_action_callback(cls, callback: ActionCallback) -> None:
        """
        注册动作处理回调（在 start 之前调用）。

        每收到客户端发来的 JSON 动作时调用 callback(action)，把返回的响应
        序列化后发回客户端。
        """
        cls._action_callback = callback

    @classmethod
    def set_session_factory(cls, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """注册数据库会话工厂，用于动态探测所有已登录账号"""
        cls._session_factory = session_factory

    @classmethod
    async def start(
        cls,
        host: str,
        port: int,
        self_id: int,
        access_token: str | None = None,
    ) -> None:
        """
        启动 WebSocket 服务端，监听指定 host:port。

        :param host: 监听 host（如 '0.0.0.0' 或 '127.0.0.1'）
        :param port: 监听端口
        :param self_id: 桥接的 OneBot self_id（作为 X-Self-ID 头随事件推送）
        :param access_token: 期望的 access_token（None 则不校验任何连接）
        """
        if cls._server_task is not None:
            l.warning("OneBotWSServer 已在运行，幂等返回")
            return

        cls._self_id = self_id
        cls._expected_token = access_token

        async def _serve_forever() -> None:
            async with serve(
                cls._handle_connection,
                host=host,
                port=port,
                process_request=cls._authenticate_request,
                max_size=meta_config.ONEBOT_WS_MAX_SIZE,
            ) as server:
                l.success(f"OneBot v11 WS 服务端启动: ws://{host}:{port}")
                # 阻塞到 server 关闭（stop() 时取消此任务）
                await server.serve_forever()

        cls._server_task = asyncio.create_task(_serve_forever())  # pyright: ignore[reportAttributeAccessIssue]

    @classmethod
    async def stop(cls) -> None:
        """
        优雅关闭：取消监听任务，关闭所有现有连接，等待清理完成。
        """
        if cls._server_task is None:
            return

        # 取消监听任务（终止 serve_forever）
        _ = cls._server_task.cancel()
        try:
            await cls._server_task
        except asyncio.CancelledError:
            pass
        cls._server_task = None  # pyright: ignore[reportAttributeAccessIssue]

        # 关闭所有连接
        if cls._connections:
            l.info(f"关闭 {len(cls._connections)} 个 OneBot WS 连接")
            await asyncio.gather(
                *(conn.close() for conn in cls._connections),
                return_exceptions=True,
            )
            cls._connections.clear()

        l.info("OneBot v11 WS 服务端已停止")

    @classmethod
    async def broadcast_event(cls, event: OneBotEventBase) -> None:
        """
        把事件广播到所有连接的 OneBot 框架。

        失败的连接会被记录但不阻塞其他连接的发送。
        无连接时静默忽略（事件已落入空集合，逻辑正确）。
        """
        if not cls._connections:
            return

        payload = orjson.dumps(event.model_dump(exclude_none=True))
        # 并发发送，失败的连接独立处理
        results = await asyncio.gather(
            *(cls._send_to_connection(conn, payload) for conn in list(cls._connections)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, ConnectionClosed):
                l.warning(f"事件广播失败: {type(result).__name__}: {result}")

    @classmethod
    async def _send_to_connection(cls, conn: ServerConnection, payload: bytes) -> None:
        """向单个连接发送 payload（自动处理已关闭的连接）"""
        try:
            await conn.send(payload)
        except ConnectionClosed:
            # 连接已关闭，从集合中移除
            cls._connections.discard(conn)
            raise

    @classmethod
    async def _authenticate_request(
        cls,
        connection: ServerConnection,
        request: Request,
    ) -> Response | None:
        """
        WebSocket 握手前的认证 hook。

        若 access_token 校验失败，返回 401 拒绝握手；通过则返回 None 让握手继续。

        :return: 拒绝时返回 Response 对象，通过时返回 None
        """
        if cls._expected_token is None:
            return None

        provided = _extract_token(request)
        if provided != cls._expected_token:
            l.warning(
                f"OneBot WS 认证失败: provided_token={'<missing>' if provided is None else '<wrong>'}"
            )
            return connection.respond(401, "Unauthorized\n")

        return None

    @classmethod
    async def _handle_connection(cls, connection: ServerConnection) -> None:
        """
        单个 WebSocket 连接的生命周期处理：注册→读循环→注销。

        握手成功后通过 X-Self-ID 和 X-Client-Role 头告知客户端身份。
        然后进入读循环：接收 JSON 动作 → 调用回调 → 发回 JSON 响应。
        """
        cls._connections.add(connection)
        peer = connection.remote_address
        l.info(f"OneBot WS 客户端已连接: {peer}（当前 {len(cls._connections)} 连接）")

        await cls._send_bootstrap_events(connection)

        try:
            async for raw in connection:
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8')
                await cls._handle_incoming_message(connection, raw)
        except ConnectionClosed:
            pass
        finally:
            cls._connections.discard(connection)
            l.info(f"OneBot WS 客户端断开: {peer}（剩余 {len(cls._connections)} 连接）")

    @classmethod
    async def _send_bootstrap_events(cls, connection: ServerConnection) -> None:
        """
        连接建立后向该客户端发送 bootstrap 元事件（connect / enable / heartbeat）。
        按活跃账号依次发送，并在动作分发器中推入待匹配队列。
        """
        from sqlmodels.bridge.bridge import Bridge

        active_bridges: list[Bridge] = []
        if cls._session_factory is not None:
            async with cls._session_factory() as session:
                active_bridges = await Bridge.get_all_active(session)

        target_self_ids: list[int] = (
            [b.self_id for b in active_bridges]
            if active_bridges
            else ([cls._self_id] if cls._self_id is not None else [])
        )

        for self_id in target_self_ids:
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
                try:
                    await connection.send(payload)
                except ConnectionClosed:
                    return
            await asyncio.sleep(0.05)

    @classmethod
    async def _handle_incoming_message(
        cls,
        connection: ServerConnection,
        raw: str,
    ) -> None:
        """
        处理一条来自客户端的 JSON 消息（应该是 OneBot 动作请求）。

        反序列化 → 调用 action_callback → 序列化响应 → 发回客户端。
        所有错误都包装为 OneBotActionResponse.failed 返回，不让连接断开。
        """
        try:
            data = orjson.loads(raw)
        except orjson.JSONDecodeError as e:
            l.warning(f"OneBot WS 收到非法 JSON: {e}")
            response = OneBotActionResponse.failed(retcode=OneBotRetCodeEnum.BAD_REQUEST)
            await connection.send(orjson.dumps(response.model_dump(exclude_none=True)))
            return

        try:
            action = OneBotAction.model_validate(data)
        except Exception as e:
            l.warning(f"OneBot WS 收到非法动作请求: {e}")
            echo = data.get('echo') if isinstance(data, dict) else None
            response = OneBotActionResponse.failed(
                retcode=OneBotRetCodeEnum.BAD_REQUEST,
                echo=echo,
            )
            await connection.send(orjson.dumps(response.model_dump(exclude_none=True)))
            return

        if cls._action_callback is None:
            l.error("OneBotWSServer 未注册 action_callback，无法处理入站动作")
            response = OneBotActionResponse.failed(
                retcode=OneBotRetCodeEnum.NOT_FOUND,
                echo=action.echo,
            )
        else:
            response = await cls._action_callback(action)

        await connection.send(orjson.dumps(response.model_dump(exclude_none=True)))
