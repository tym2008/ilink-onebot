"""Bridge 领域枚举

iLink 协议枚举严格映射官方 Tencent/openclaw-weixin types.ts 的定义。
注意：UploadMediaType 与 MessageItemType 的数字编码**不同**——这是 iLink 协议本身的
约定，不能简单复用同一份枚举。
"""
from enum import IntEnum, StrEnum


class BridgeStateEnum(StrEnum):
    """Bridge 会话状态"""
    idle = 'idle'
    """空闲：未登录，无 token"""

    awaiting_scan = 'awaiting_scan'
    """等待扫码：已获取 QR 码，等待用户扫码确认"""

    running = 'running'
    """运行中：已登录，正在长轮询接收消息"""

    expired = 'expired'
    """会话过期：iLink 返回 -14 / 401，需要重新扫码"""


class ILinkMessageItemType(IntEnum):
    """
    iLink ``MessageItem.type`` 取值（消息段类型）。

    映射官方 ``MessageItemType``：每个段对应一个具体的子对象字段
    （type=TEXT → text_item, type=IMAGE → image_item, ...）。
    """
    NONE = 0
    TEXT = 1
    IMAGE = 2
    VOICE = 3
    FILE = 4
    VIDEO = 5


class ILinkMessageType(IntEnum):
    """
    iLink ``WeixinMessage.message_type`` 取值（消息**发送角色**）。

    注意：这不是消息内容类型！内容类型在 item_list[].type 上。
    """
    NONE = 0
    USER = 1
    """用户发给 bot 的消息（入站方向）"""

    BOT = 2
    """bot 发给用户的消息（出站方向）"""


class ILinkMessageStateEnum(IntEnum):
    """
    iLink ``WeixinMessage.message_state``。

    - NEW：非流式消息或首次推送
    - GENERATING：流式消息生成中，OneBot v11 无对应概念应跳过
    - FINISH：流式消息最终态 / 一般完成态
    """
    NEW = 0
    GENERATING = 1
    FINISH = 2


class ILinkUploadMediaType(IntEnum):
    """
    iLink ``GetUploadUrlReq.media_type`` 取值。

    **顺序与 MessageItemType 不同**！这是 iLink 协议的怪癖：上传 API 用一个独立的
    枚举，IMAGE=1 / VIDEO=2 / FILE=3 / VOICE=4（不是 TEXT=1...VIDEO=5）。
    """
    IMAGE = 1
    VIDEO = 2
    FILE = 3
    VOICE = 4


class ILinkTypingStatus(IntEnum):
    """
    iLink ``sendtyping`` 的 ``status`` 取值（与官方 ``TypingStatus`` 对齐）。
    """
    TYPING = 1
    """正在输入"""

    CANCEL = 2
    """取消输入"""


class ILinkQRStatusEnum(StrEnum):
    """iLink ``get_qrcode_status`` 响应的 status 字段取值（共 8 种）"""
    wait = 'wait'
    """等待用户扫码"""

    scaned = 'scaned'
    """用户已扫码，等待确认"""

    confirmed = 'confirmed'
    """用户已确认，登录成功（响应会含 bot_token + ilink_bot_id）"""

    expired = 'expired'
    """QR 码已过期，需要重新申请"""

    scaned_but_redirect = 'scaned_but_redirect'
    """扫码成功但需 IDC 重定向，响应含 redirect_host，须用新 host 继续轮询"""

    need_verifycode = 'need_verifycode'
    """需要输入配对码（手机微信上显示的数字），客户端应提示用户输入并重轮询"""

    verify_code_blocked = 'verify_code_blocked'
    """配对码多次错误已阻断，需重新申请 QR"""

    binded_redirect = 'binded_redirect'
    """该 bot 已绑定到本应用，无需重复登录（视为成功）"""
