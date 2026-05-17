"""
``python -m typers`` 入口 / PyInstaller 打包后 exe 入口

让本包既能通过 ``pyproject.toml`` 的 ``[project.scripts] ilink-onebot = "typers:app"``
注册为系统命令，也能直接 ``python -m typers <command>`` 运行（开发期方便调试）。

打包后的 exe 也是从这里启动（spec 文件指向 ``typers/__main__.py``）。

Windows GBK 控制台兼容：rich 库通过 codepage 探测决定输出编码，PyInstaller frozen
exe 不响应 ``PYTHONIOENCODING`` 环境变量。在入口处显式 reconfigure stdout/stderr
为 UTF-8，让中文字符 + Unicode 符号（如 ``↔``）正常输出。
"""
import sys


def _ensure_utf8_streams() -> None:
    """
    在 Windows 控制台上把 stdout / stderr 切换为 UTF-8。

    Python 3.7+ 的 ``TextIOWrapper.reconfigure`` 允许运行时修改编码。
    在 Linux/macOS 上 stdout 本来就是 UTF-8，调用是安全 no-op。
    PyInstaller frozen exe 不响应 ``PYTHONIOENCODING``，必须显式 reconfigure。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            try:
                reconfigure(encoding='utf-8', errors='replace')
            except (OSError, ValueError):
                # 某些受限终端（重定向到非 tty）会拒绝 reconfigure，忽略即可
                pass


_ensure_utf8_streams()

from typers import app  # noqa: E402  # 先 reconfigure 再 import，让 typer/rich 初始化时取到正确编码

if __name__ == '__main__':
    app()
