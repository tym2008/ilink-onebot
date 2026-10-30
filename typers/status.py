"""
`ilink-onebot status` — 查看运行状态

输出 Bridge 当前状态、登录信息、长轮询游标等关键字段，供运维诊断。

注意：此命令不会探测正在运行的 `run` 进程（无 IPC），只读取数据库快照。
"""
import asyncio

import typer

from meta_config import meta_config
from sqlmodels.bridge import Bridge
from sqlmodels.media_file import MediaFile
from sqlmodels.server_config import ServerConfig
from typers import app
from typers.bootstrap import infra_lifespan


async def _status_impl() -> None:
    """异步状态查询主流程"""
    async with infra_lifespan() as session_factory:
        async with session_factory() as session:
            bridges = await Bridge.get_all(session)
            config = await ServerConfig.get_instance(session)
            media_count = await MediaFile.count(session)

        typer.echo(f"=== iLink Bridge 账号状态（共 {len(bridges)} 个）===")
        for b in bridges:
            typer.echo(f"  [{b.account_name}] (self_id={b.self_id}):")
            typer.echo(f"    state          : {b.state}")
            typer.echo(f"    wechat_id      : {b.wechat_id or '<未登录>'}")
            typer.echo(f"    wechat_nickname: {b.wechat_nickname or '<未登录>'}")
            typer.echo(f"    bot_token      : {'<已设置>' if b.bot_token else '<未设置>'}")
            typer.echo(f"    poll cursor    : {'<有>' if b.get_updates_buf else '<空>'}")

        typer.echo("\n=== 运行配置（ServerConfig） ===")
        typer.echo(f"  bot_display_name           : {config.bot_display_name}")
        typer.echo(f"  poll_interval_seconds      : {config.poll_interval_seconds}")
        typer.echo(f"  heartbeat_interval_seconds : {config.heartbeat_interval_seconds}")
        typer.echo(f"  auto_logout_on_expired     : {config.auto_logout_on_session_expired}")

        typer.echo("\n=== 网络配置（meta_config） ===")
        typer.echo(f"  OneBot WS  : ws://{meta_config.ONEBOT_WS_HOST}:{meta_config.ONEBOT_WS_PORT}")
        typer.echo(
            f"  Media HTTP : http://{meta_config.MEDIA_SERVE_HOST}:{meta_config.MEDIA_SERVE_PORT}"
        )
        typer.echo(f"  OneBot self_id : {meta_config.ONEBOT_SELF_ID}")

        typer.echo("\n=== 媒体缓存 ===")
        typer.echo(f"  total records : {media_count}")
        typer.echo(f"  cache dir     : {meta_config.MEDIA_CACHE_DIR}")
        typer.echo(f"  cache ttl     : {meta_config.MEDIA_CACHE_TTL_SECONDS}s")


@app.command()
def status() -> None:
    """
    显示桥接当前的运行状态、登录信息、配置和媒体缓存统计。

    本命令只读取数据库快照，不会与正在运行的 `run` 进程通信。
    """
    asyncio.run(_status_impl())
