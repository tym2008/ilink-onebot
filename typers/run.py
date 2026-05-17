"""
`ilink-onebot run` — 启动主服务（长生命周期 daemon）

编排：
1. 基础设施初始化（`infra_lifespan` yield session_factory）
2. 检查 Bridge 是否已登录，未登录则提示先 `login` 退出
3. 启动 OneBot v11 WS 服务端（监听框架连接）
4. 启动 FastAPI 媒体服务（uvicorn.Server 作为 asyncio.Task）
5. 启动 OneBot 心跳广播
6. 启动长轮询循环（接收 iLink 消息 → 推送 OneBot 事件）— Phase 6 实现
7. 信号处理：Ctrl+C → 优雅关闭

依赖注入原则：session_factory 在最外层 `_run_impl` 中通过 `infra_lifespan` 获得，
显式传递给所有下游（心跳任务、动作回调闭包、uvicorn 媒体服务）。
方法内部禁止自行调用 `DatabaseManager.get_session_factory()`。
"""
import asyncio
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress

import typer
import uvicorn
from loguru import logger as l
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from fastapis.app import create_app
from meta_config import meta_config
from onebots.action_handler import dispatch_action
from onebots.heartbeat import OneBotHeartbeat
from onebots.models import OneBotAction, OneBotActionResponse
from onebots.ws_client import OneBotWSClient
from onebots.ws_server import OneBotWSServer
from sqlmodels.bridge import Bridge
from sqlmodels.bridge.enums import BridgeStateEnum
from sqlmodels.bridge.polling import PollingLoop
from sqlmodels.server_config import ServerConfig
from typers import app
from typers.bootstrap import infra_lifespan


async def _run_uvicorn_task(stop_event: asyncio.Event) -> None:
    """
    在 asyncio.Task 中运行 uvicorn 服务器。

    uvicorn.Server.serve() 是协程，可以在现有事件循环中运行。
    外层用 stop_event 通知服务器优雅关闭：设置 server.should_exit = True。
    """
    fastapi_app = create_app()
    config = uvicorn.Config(
        app=fastapi_app,
        host=meta_config.MEDIA_SERVE_HOST,
        port=meta_config.MEDIA_SERVE_PORT,
        log_config=None,  # 用 loguru 而非 uvicorn 的默认日志
        access_log=False,
    )
    server = uvicorn.Server(config)

    async def _watch_stop() -> None:
        await stop_event.wait()
        server.should_exit = True

    watch_task = asyncio.create_task(_watch_stop())
    try:
        await server.serve()
    finally:
        if not watch_task.done():
            _ = watch_task.cancel()
            with suppress(asyncio.CancelledError):
                await watch_task


def _make_onebot_action_callback(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[OneBotAction], Awaitable[OneBotActionResponse]]:
    """
    构造 OneBot WS 服务端的动作回调闭包。

    通过闭包显式捕获 session_factory（依赖注入），避免回调内部隐式
    `DatabaseManager.get_session_factory()` 自取。

    每次动作触发时重新加载 Bridge / ServerConfig：state 可能因为会话过期变化、
    config 可能被运行时编辑，必须取最新值。
    """
    async def _callback(action: OneBotAction) -> OneBotActionResponse:
        # 单独事务加载单例，避免污染 dispatch_action 内部的 session 生命周期
        async with session_factory() as session:
            bridge = await Bridge.get_instance(session)
            config = await ServerConfig.get_instance(session)
        return await dispatch_action(bridge, session_factory, config, action)

    return _callback


def _register_signal_handlers(shutdown_event: asyncio.Event) -> None:
    """
    注册 SIGINT / SIGTERM 处理器，触发优雅关闭。

    Windows 上 `loop.add_signal_handler` 抛 NotImplementedError，
    回退到 `signal.signal` 同步注册（在主线程上 Ctrl+C 仍能工作）。
    """
    def _trigger(sig_name: str) -> None:
        l.info(f"收到信号 {sig_name}，开始优雅关闭")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, _trigger, 'SIGINT')
        loop.add_signal_handler(signal.SIGTERM, _trigger, 'SIGTERM')
    except NotImplementedError:
        # Windows fallback：只支持 SIGINT
        signal.signal(signal.SIGINT, lambda s, f: _trigger('SIGINT'))


async def _ensure_logged_in(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """
    启动前的登录状态守卫。

    未登录或会话过期则提示用户先执行 login 后退出。
    """
    async with session_factory() as session:
        bridge = await Bridge.get_instance(session)

    if bridge.state == BridgeStateEnum.idle or bridge.bot_token is None:
        typer.echo("✗ 尚未登录，请先执行 `ilink-onebot login`")
        raise typer.Exit(code=1)
    if bridge.state == BridgeStateEnum.expired:
        typer.echo("✗ 会话已过期，请重新执行 `ilink-onebot login`")
        raise typer.Exit(code=1)
    l.info(f"Bridge 已就绪: {bridge.wechat_nickname} ({bridge.wechat_id})")


async def _run_impl() -> None:
    """异步主服务编排（session_factory 由 infra_lifespan 显式提供）"""
    async with infra_lifespan() as session_factory:
        # 1. 登录守卫
        await _ensure_logged_in(session_factory)

        # 2. 注册 OneBot 动作回调（闭包捕获 session_factory，同一份 callback 同时
        # 提供给 server 模式（被动监听）和 client 模式（主动连框架））
        action_callback = _make_onebot_action_callback(session_factory)
        OneBotWSServer.set_action_callback(action_callback)
        OneBotWSClient.set_action_callback(action_callback)

        # 3. 通知 iLink 服务端本客户端已启动（与官方 channel.ts 对齐，避免被误判离线）
        async with session_factory() as session:
            bridge = await Bridge.get_instance(session)
        await bridge.notify_start()

        # 4. 启动 OneBot WS 服务端（被动监听，让框架连过来）
        l.info("启动 OneBot v11 WS 服务端")
        await OneBotWSServer.start(
            host=meta_config.ONEBOT_WS_HOST,
            port=meta_config.ONEBOT_WS_PORT,
            self_id=meta_config.ONEBOT_SELF_ID,
            access_token=meta_config.ONEBOT_ACCESS_TOKEN,
        )

        # 4.5. 若配置了反向 WS URL，启动反向客户端（主动连接到框架的反向 WS 服务端）
        if meta_config.ONEBOT_REVERSE_WS_URL is not None:
            l.info(f"启动 OneBot v11 反向 WS 客户端 → {meta_config.ONEBOT_REVERSE_WS_URL}")
            await OneBotWSClient.start(
                url=meta_config.ONEBOT_REVERSE_WS_URL,
                self_id=meta_config.ONEBOT_SELF_ID,
                access_token=meta_config.ONEBOT_ACCESS_TOKEN,
            )

        # 5. 启动 OneBot 心跳（显式注入 session_factory）
        l.info("启动 OneBot 心跳")
        await OneBotHeartbeat.start(session_factory)

        # 5. 启动 FastAPI 媒体服务（uvicorn.Server 作为 asyncio.Task）
        uvicorn_stop_event = asyncio.Event()
        uvicorn_task = asyncio.create_task(_run_uvicorn_task(uvicorn_stop_event))
        l.success(
            f"FastAPI 媒体服务启动: "
            f"http://{meta_config.MEDIA_SERVE_HOST}:{meta_config.MEDIA_SERVE_PORT}"
        )

        # 6. 启动 iLink 长轮询循环（显式注入 session_factory）
        l.info("启动 iLink 长轮询循环")
        await PollingLoop.start(session_factory)

        # 7. 等待 SIGINT/SIGTERM
        shutdown_event = asyncio.Event()
        _register_signal_handlers(shutdown_event)
        await shutdown_event.wait()

        # 8. 优雅关闭：反向顺序停止所有服务
        l.info("关闭 iLink 长轮询循环")
        await PollingLoop.stop()

        l.info("关闭 FastAPI 媒体服务")
        uvicorn_stop_event.set()
        with suppress(asyncio.CancelledError):
            await uvicorn_task

        l.info("关闭 OneBot 心跳")
        await OneBotHeartbeat.stop()

        l.info("关闭 OneBot 反向 WS 客户端（如启用）")
        await OneBotWSClient.stop()

        l.info("关闭 OneBot WS 服务端")
        await OneBotWSServer.stop()

        # 最后通知 iLink 服务端本客户端即将下线（best-effort）
        async with session_factory() as session:
            bridge_for_stop = await Bridge.get_instance(session)
        await bridge_for_stop.notify_stop()


@app.command()
def run() -> None:
    """
    启动桥接主服务（长生命周期）。

    Ctrl+C 触发优雅关闭，所有正在进行的请求会等待完成。
    """
    asyncio.run(_run_impl())
