"""
``ilink-onebot account`` — 多账号管理子命令组

提供：
- ``ilink-onebot account list``：列出所有已配置/已登录账号
- ``ilink-onebot account remove <name>``：删除指定账号及关联的 context token
"""
import asyncio

import typer
from loguru import logger as l
from sqlmodel import col

from sqlmodels.bridge import Bridge
from sqlmodels.bridge.context_token import ContextToken
from typers.bootstrap import infra_lifespan

account_app = typer.Typer(
    name='account',
    help='多账号管理命令（查看列表、删除账号）',
    no_args_is_help=True,
)


async def _list_accounts_impl() -> None:
    async with infra_lifespan() as session_factory:
        async with session_factory() as session:
            bridges = await Bridge.get_all(session)

    if not bridges:
        typer.echo("当前暂无已配置账号，请使用 `ilink-onebot login [account_name]` 进行登录。")
        return

    typer.echo(f"=== 已配置账号列表（共 {len(bridges)} 个）===")
    typer.echo(
        f"{'账号名称':<12} {'OneBot self_id':<16} {'状态':<12} {'微信昵称':<16} {'微信 ID':<20}"
    )
    typer.echo("-" * 80)
    for b in bridges:
        acc = b.account_name
        sid = str(b.self_id)
        st = b.state.value if hasattr(b.state, 'value') else str(b.state)
        nick = b.wechat_nickname or '<未同步>'
        wid = b.wechat_id or '<未登录>'
        typer.echo(f"{acc:<12} {sid:<16} {st:<12} {nick:<16} {wid:<20}")


async def _remove_account_impl(account: str, force: bool) -> None:
    async with infra_lifespan() as session_factory:
        async with session_factory() as session:
            bridge = await Bridge.get_by_name(session, account)
            if bridge is None:
                typer.echo(f"✗ 未找到账号: '{account}'")
                raise typer.Exit(code=1)

            if not force and bridge.bot_token is not None:
                confirm = typer.confirm(
                    f"账号 '{account}' (self_id={bridge.self_id}) 当前已登录，确定要删除吗？"
                )
                if not confirm:
                    typer.echo("已取消操作。")
                    return

            bridge_id = bridge.id
            if bridge_id is not None:
                # 清除关联的 ContextToken
                tokens = await ContextToken.get(
                    session,
                    col(ContextToken.bridge_id) == bridge_id,
                    fetch_mode='all',
                )
                if tokens:
                    for t in tokens:
                        await session.delete(t)

            await session.delete(bridge)
            await session.commit()
            l.success(f"账号 '{account}' (id={bridge_id}) 及其关联的会话数据已删除")
            typer.echo(f"✓ 账号 '{account}' 已成功删除。")


@account_app.command(name='list')
def list_accounts() -> None:
    """列出所有已配置的账号及其状态。"""
    asyncio.run(_list_accounts_impl())


@account_app.command(name='remove')
def remove_account(
    account: str = typer.Argument(..., help="要删除的账号别名"),
    force: bool = typer.Option(False, '--force', '-f', help="强制删除，无需二次确认"),
) -> None:
    """删除指定的账号及其会话数据。"""
    asyncio.run(_remove_account_impl(account, force))
