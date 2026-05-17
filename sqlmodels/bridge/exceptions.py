"""Bridge 领域异常

所有异常都携带完整的失败上下文（端点 / 错误码 / 用户 ID / 消息 ID 等），
让捕获方能据此判断重试 / 降级 / 上抛策略。绝不静默 swallow。
"""


class ILinkException(Exception):
    """iLink 协议相关异常基类"""


class ILinkAPIError(ILinkException):
    """
    iLink API 返回业务错误（``ret`` 非 0 或 ``errcode`` 非 0 且非会话过期）。

    :param errcode: 生效的错误码（``get_failure()`` 返回 tuple 的第一项）
    :param errmsg: 生效的错误描述（``get_failure()`` 返回 tuple 的第二项）
    :param endpoint: 调用的 iLink 端点（相对路径）
    """

    def __init__(self, errcode: int, errmsg: str, endpoint: str) -> None:
        self.errcode = errcode
        self.errmsg = errmsg
        self.endpoint = endpoint
        super().__init__(f"iLink API {endpoint} returned errcode={errcode}: {errmsg}")


class ILinkSessionExpiredError(ILinkException):
    """
    iLink 会话过期（errcode == -14）。

    PollingLoop 捕获此异常后会触发自动重新扫码登录流程。
    """


class ILinkContextTokenMissingError(ILinkException):
    """
    目标用户的 context_token 缺失。

    发生在发送消息时目标用户尚未给 bot 发过任何消息（无对应 context_token 行）。
    iLink 协议规定 bot 不能主动发起对话，必须由用户先发消息才能回复。

    :param user_id: 目标用户的 iLink user_id（``WeixinMessage.from_user_id`` / ``to_user_id``）
    """

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        super().__init__(
            f"No context_token for user '{user_id}'. "
            "The user must send a message to the bot first."
        )


# ===== 媒体相关异常 =====

class MediaError(ILinkException):
    """媒体收发相关异常基类"""


class MediaInboundError(MediaError):
    """
    入站媒体下载 / 解密失败。

    携带 iLink 消息 ID + 失败阶段，让上层日志能精确定位。

    :param ilink_msg_id: 关联的 iLink message_id（来自 ``WeixinMessage.message_id``）
    :param stage: 失败的阶段（``download`` / ``decrypt`` / ``persist``）
    :param reason: 详细错误信息
    """

    def __init__(self, ilink_msg_id: str | int, stage: str, reason: str) -> None:
        self.ilink_msg_id = ilink_msg_id
        self.stage = stage
        self.reason = reason
        super().__init__(
            f"Inbound media failed at '{stage}' for ilink msg_id={ilink_msg_id}: {reason}"
        )


class MediaOutboundError(MediaError):
    """
    出站媒体加密 / 上传 / 发送失败。

    :param user_id: 目标 iLink user_id
    :param segment_type: OneBot 段类型（``image`` / ``record`` / ``video`` / ``file``）
    :param stage: 失败阶段（``fetch_source`` / ``encrypt`` / ``get_upload_url`` / ``upload`` / ``send``）
    :param reason: 详细错误信息
    """

    def __init__(self, user_id: str, segment_type: str, stage: str, reason: str) -> None:
        self.user_id = user_id
        self.segment_type = segment_type
        self.stage = stage
        self.reason = reason
        super().__init__(
            f"Outbound {segment_type} failed at '{stage}' for user_id={user_id}: {reason}"
        )


# ===== ID 映射异常 =====

class OneBotMappingMissingError(ILinkException):
    """
    iLink ↔ OneBot message_id 映射查不到。

    通常发生在引用回复（reply 段）场景：iLink 收到一条带 ref_msg 的消息，
    但 ref_msg 引用的原消息不在本桥接的 ``OneBotMessageMapping`` 表里
    （可能消息保留期外 / 重启前的消息没注册 / 跨 bot 实例的消息）。

    转换层抛出此异常让上层决定降级（如丢弃 reply 段保留主体）还是上抛。

    :param ilink_msg_id: 查不到的 iLink message_id
    :param direction: ``ilink_to_onebot`` 或 ``onebot_to_ilink``
    """

    def __init__(self, ilink_msg_id: str | int, direction: str = 'ilink_to_onebot') -> None:
        self.ilink_msg_id = ilink_msg_id
        self.direction = direction
        super().__init__(
            f"No OneBot↔iLink mapping ({direction}) for ilink msg_id={ilink_msg_id}"
        )
