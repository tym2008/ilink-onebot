"""
``ilink-onebot login`` — 手动 QR 码扫码登录（支持多账号）

薄包装层：把 ``Bridge.run_qrcode_login`` 充血方法的布尔返回值映射为 ``typer.Exit``
退出码，并在终端附加面向用户的额外提示。

支持指定账号别名（默认 'default'）及可选的 OneBot self_id 分配。
"""
import asyncio

import typer
from loguru import logger as l

from meta_config import meta_config
from sqlmodel import col
from sqlmodels.bridge import Bridge
from sqlmodels.bridge.enums import BridgeStateEnum
from typers import app
from typers.bootstrap import infra_lifespan


async def _login_impl(account: str | None, self_id: int | None, force: bool) -> None:
    """异步登录主流程"""
    async with infra_lifespan() as session_factory:
        async with session_factory() as session:
            all_bridges = await Bridge.get_all(session)
            used_self_ids = {b.self_id for b in all_bridges if b.self_id is not None}
            used_account_names = {b.account_name for b in all_bridges}
            default_bridge = await Bridge.get_by_name(session, 'default')

            target_account: str
            if account is not None:
                target_account = account
            else:
                # 若未指定账号：若 default 未就绪或指定了 --force，使用 default；否则自动分配新账号 bot2, bot3...
                if default_bridge is None or default_bridge.bot_token is None or default_bridge.state != BridgeStateEnum.running or force:
                    target_account = 'default'
                else:
                    idx = 2
                    while f'bot{idx}' in used_account_names:
                        b = await Bridge.get_by_name(session, f'bot{idx}')
                        if b is not None and (b.bot_token is None or b.state != BridgeStateEnum.running):
                            break
                        idx += 1
                    target_account = f'bot{idx}'
                    typer.echo(
                        f"💡 检测到默认账号 'default' 已处于登录状态，自动为新账号 '{target_account}' 获取登录二维码..."
                    )

            bridge = await Bridge.get_by_name(session, target_account)

            if bridge is None:
                # 寻找未占用的 self_id
                if self_id is not None:
                    if self_id in used_self_ids:
                        typer.echo(f"✗ self_id {self_id} 已被其他账号使用，请更换")
                        raise typer.Exit(code=1)
                    allocated_self_id = self_id
                else:
                    if target_account == 'default' and meta_config.ONEBOT_SELF_ID not in used_self_ids:
                        allocated_self_id = meta_config.ONEBOT_SELF_ID
                    else:
                        candidate = meta_config.ONEBOT_SELF_ID
                        while candidate in used_self_ids:
                            candidate += 1
                        allocated_self_id = candidate

                bridge = Bridge(
                    account_name=target_account,
                    self_id=allocated_self_id,
                )
                bridge = await bridge.save(session)
                l.info(f"创建新账号 '{target_account}', self_id={bridge.self_id}")
            else:
                if self_id is not None and self_id != bridge.self_id:
                    if self_id in used_self_ids:
                        typer.echo(f"✗ self_id {self_id} 已被其他账号使用，请更换")
                        raise typer.Exit(code=1)
                    bridge.self_id = self_id
                    bridge = await bridge.save(session)

                if force:
                    bridge.bot_token = None
                    bridge.state = BridgeStateEnum.idle
                    bridge = await bridge.save(session)
                elif bridge.bot_token is not None and bridge.state == BridgeStateEnum.running:
                    l.success(
                        f"账号 '{target_account}' (self_id={bridge.self_id}) 已登录: "
                        f"{bridge.wechat_nickname or bridge.wechat_id}"
                    )
                    typer.echo(
                        f"账号 '{target_account}' 已处于登录状态。若要重新扫码，请添加 `--force` 参数。\n"
                        f"若要登录新账号，可直接执行 `ilink-onebot login` 或指定新账号名（如 `ilink-onebot login bot2`）。"
                    )
                    return

        typer.echo(
            f"\n请用微信扫描以下 QR 码为账号 '{target_account}' (self_id={bridge.self_id}) "
            f"登录（在 5 分钟内完成）:\n"
        )
        success = await bridge.run_qrcode_login(
            session_factory,
            prompt_message=None,  # CLI 上面已经 typer.echo 提示，避免重复
        )
        typer.echo()

        if success:
            async with session_factory() as session:
                refreshed = await Bridge.get(session, col(Bridge.id) == bridge.id)
                if refreshed is not None:
                    bridge = refreshed
            typer.echo(
                f"\n✓ 登录成功: 账号 '{target_account}' (self_id={bridge.self_id}) - "
                f"{bridge.wechat_nickname or bridge.wechat_id}"
            )
        else:
            typer.echo(
                f"\n✗ 扫码登录失败（QR 过期或超时），请重新执行 `ilink-onebot login {target_account}`"
            )
            raise typer.Exit(code=1)


@app.command()
def login(
    account: str | None = typer.Argument(
        None,
        help="账号标识别名（如 'default', 'bot2', 'wechat1'）。若未指定且默认账号已登录，将自动创建下一个账号（bot2, bot3...）",
    ),
    self_id: int | None = typer.Option(
        None,
        '--self-id',
        '-s',
        help="OneBot self_id（虚拟 QQ 号），不指定则自动分配",
    ),
    force: bool = typer.Option(
        False,
        '--force',
        '-f',
        help="强制重新扫码登录（清除现有登录会话）",
    ),
) -> None:
    """
    扫码登录微信账号（支持多账号）。

    会在终端渲染一个 QR 码，请用对应微信"扫一扫"扫描。登录成功后 bot_token 会被
    保存到数据库，后续运行 ``ilink-onebot run`` 时自动恢复所有已登录账号的会话。
    若默认账号已登录，直接再次执行 `ilink-onebot login` 会自动分配新账号（bot2, bot3...）。
    """
    asyncio.run(_login_impl(account, self_id, force))
