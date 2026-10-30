"""
typer CLI 端点层

提供三个子命令：
- `ilink-onebot login`：QR 码扫码登录（短生命周期）
- `ilink-onebot run`：启动主服务（长生命周期 daemon）
- `ilink-onebot status`：查看运行状态（短生命周期）

typer 本身是同步的（不原生支持 async），所有命令都用 `asyncio.run()` 包装内部
异步函数，进入 asyncio 事件循环后接管所有 IO。

入口点（pyproject.toml `[project.scripts]`）：`ilink-onebot = "typers:app"`。
"""
import typer

app = typer.Typer(
    name='ilink-onebot',
    help='WeChat iLink ↔ OneBot v11 bridge: 将微信 iLink 协议桥接到 OneBot v11 框架',
    no_args_is_help=True,
)

# 触发子命令模块的副作用导入（typer 的 @app.command 装饰器）
import typers.login  # noqa: E402, F401
import typers.run  # noqa: E402, F401
import typers.status  # noqa: E402, F401
from typers.account import account_app  # noqa: E402

app.add_typer(account_app, name='account')
