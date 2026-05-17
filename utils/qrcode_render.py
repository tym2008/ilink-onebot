"""
终端 QR 码渲染工具

提供把任意字符串内容渲染为 ASCII QR 码到 stdout 的工具函数。

为什么放在 utils/：QR 渲染的"消费者"既有 ``typers/`` CLI 层（``login`` 命令），
也有 ``sqlmodels/`` 领域层（``Bridge.relogin`` 自动重登流程在终端展示新 QR）。
放在最底层的 utils/ 让两个上层都能直接 import 而不构成循环依赖。
"""
import sys

import qrcode
import qrcode.constants


def render_qrcode_to_terminal(content: str) -> None:
    """
    在终端用 ASCII 字符渲染 QR 码到 stdout。

    使用 ``ERROR_CORRECT_L``（最低纠错级别）让 QR 码更紧凑；扫码时人在屏幕前
    不会有遮挡，不需要高纠错。``print_ascii`` 输出到 stdout 默认无颜色，适用于
    所有终端（含远程 SSH）。
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=1,
    )
    qr.add_data(content)
    qr.make(fit=True)
    qr.print_ascii(out=sys.stdout, invert=False)
