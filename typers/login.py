"""
``ilink-onebot login`` — 手动 QR 码扫码登录

薄包装层：把 ``Bridge.run_qrcode_login`` 充血方法的布尔返回值映射为 ``typer.Exit``
退出码，并在终端附加面向用户的额外提示。

实际的"请求 QR → 渲染 → 轮询状态"流程由 ``Bridge.run_qrcode_login`` 提供，
``PollingLoop`` 的自动重登流程也复用同一充血方法，避免 DRY 违规。
"""
import asyncio

import typer
from loguru import logger as l

from sqlmodels.bridge import Bridge
from typers import app
from typers.bootstrap import infra_lifespan


async def _login_impl() -> None:
    """异步登录主流程"""
    async with infra_lifespan() as session_factory:
        async with session_factory() as session:
            bridge = await Bridge.get_instance(session)

        # 已登录直接提示，不必重复扫码
        if bridge.bot_token is not None:
            l.success(f"已登录: {bridge.wechat_nickname or bridge.wechat_id}")
            typer.echo("已处于登录状态。若要重新扫码，请先在数据库中清除 bot_token 字段。")
            return

        typer.echo("\n请用微信扫描以下 QR 码（在 5 分钟内完成）:\n")
        success = await bridge.run_qrcode_login(
            session_factory,
            prompt_message=None,  # CLI 上面已经 typer.echo 提示，避免重复
        )
        typer.echo()

        if success:
            typer.echo(
                f"\n✓ 登录成功: {bridge.wechat_nickname or bridge.wechat_id}"
            )
        else:
            typer.echo("\n✗ 扫码登录失败（QR 过期或超时），请重新执行 `ilink-onebot login`")
            raise typer.Exit(code=1)


@app.command()
def login() -> None:
    """
    扫码登录微信账号。

    会在终端渲染一个 QR 码，请用微信"扫一扫"扫描。登录成功后 bot_token 会被
    保存到数据库，后续运行 ``ilink-onebot run`` 时自动恢复会话。
    """
    asyncio.run(_login_impl())
