"""
iLink 协议数据模型（非 DB 表）

严格映射 Tencent/openclaw-weixin@2.4.3 的 ``src/api/types.ts``。

类型严格性原则（重要）：
官方 ``types.ts`` 几乎所有字段都标 ``?`` 可选——这是 protobuf 编码风格，不代表
运行时缺失。本模块基于协议的**实际行为**判断必有性：

- **协议成功响应必返回的字段**：标为必填（不可空）
- **基于 status / type 等 discriminator 才有的字段**：标为 Optional（如 ``bot_token``
  仅在 ``status='confirmed'`` 时存在）
- **服务端可能省略但客户端可填默认值的字段**：用默认值代替 Optional（如 ``ret=0``）
- **协议明确允许缺失的字段**：保留 Optional（如 ``thumb_media`` 仅大图有）

错误码协议：
- ``ret``：请求级（0 或缺失 = 成功）
- ``errcode``：业务级（如 -14 会话过期，-401 token 无效）
- 成功响应可能两者都缺；错误响应可能只有一种或两种都有。统一通过
  ``ILinkResponseBase.get_failure()`` 判断（返回 None = 成功，返回 tuple = 失败）
"""
from sqlmodel import Field

from sqlmodel_ext import ExtraIgnoreModelBase

from sqlmodels.bridge.enums import (
    ILinkMessageItemType,
    ILinkMessageStateEnum,
    ILinkMessageType,
    ILinkQRStatusEnum,
    ILinkTypingStatus,
)


# ===== BaseInfo（每个 POST 请求都带） =====

class BaseInfo(ExtraIgnoreModelBase):
    """
    每个 CGI POST 请求的元信息（``base_info`` 字段）。

    类 HTTP User-Agent：用作请求来源识别 / 版本兼容 / 调用聚合统计，
    不参与认证或路由。两个字段都是本端自报，故均必填——不发空值。
    """
    channel_version: str
    """客户端实现的版本号"""

    bot_agent: str
    """上游 bot/app 自报身份，UA 风格 ``Name/Version`` token"""


# ===== 公共响应基类 =====

class ILinkResponseBase(ExtraIgnoreModelBase):
    """
    iLink 响应共享字段。

    ``ret`` 与 ``errcode`` 在协议层都是可选的（成功路径可能两者都缺），故本基类
    保留 Optional 但默认 None。用 ``get_failure()`` 统一判断。
    """
    ret: int | None = None
    """请求级错误码（0 或缺失 = 成功）"""

    err_msg: str | None = None
    """请求级错误描述"""

    errcode: int | None = None
    """业务级错误码（-14 = 会话过期，等）"""

    errmsg: str | None = None
    """业务级错误描述"""

    def get_failure(self) -> 'tuple[int, str] | None':
        """
        成功返回 None；失败返回 ``(error_code, error_message)`` 元组。

        判优先级：``errcode`` 优先于 ``ret``（业务级语义比请求级精确）；
        ``errmsg`` 优先于 ``err_msg``。

        这个设计让调用方一次性获得"是否失败 + 失败码 + 失败描述"，避免分散调用
        ``is_successful() / effective_error_code() / effective_error_message()``
        产生的"如果失败则 effective_error_code() 一定非 None"这种类型系统无法表达的
        不变式。
        """
        if self.errcode is not None and self.errcode != 0:
            return (self.errcode, self.errmsg or self.err_msg or '')
        if self.ret is not None and self.ret != 0:
            return (self.ret, self.err_msg or self.errmsg or '')
        return None


# ===== Request 通用基类 =====

class ILinkRequestWithBaseInfo(ExtraIgnoreModelBase):
    """
    所有需要在 body 注入 ``base_info`` 的 iLink 请求基类。

    协议要求所有业务 CGI 请求（getupdates / sendmessage / sendtyping / getconfig /
    getuploadurl / notifystart / notifystop）都携带 ``base_info``。QR 流程的请求
    不含此字段，单独继承 ``ExtraIgnoreModelBase``。
    """
    base_info: BaseInfo


# ===== QR 登录流程 =====

class QRCodeRequest(ExtraIgnoreModelBase):
    """
    ``POST /ilink/bot/get_bot_qrcode?bot_type=3`` 请求体。

    ``local_token_list`` 用于服务端识别"已登录账号"，single-bot 模式下传当前 bot_token
    （如有）或空列表。本请求**不带** ``base_info``（与官方一致）。

    无默认值：调用方必须显式传 ``local_token_list``（即使是空列表也要明确传 ``[]``），
    让"未设置"和"设置为空"的语义区分明确。
    """
    local_token_list: list[str]


class QRCodeResponse(ILinkResponseBase):
    """
    ``POST /ilink/bot/get_bot_qrcode?bot_type=3`` 响应。

    成功响应（``get_failure() is None``）保证 ``qrcode`` 和 ``qrcode_img_content``
    都存在——没有它们 QR 流程无法继续。失败响应则只有 ret/errcode 有意义，
    两个字段可能缺失。

    为了类型干净，这两个字段保留 Optional：调用方先 ``get_failure()`` 校验，
    通过后就当作非空使用（运行时若依然 None 视为协议异常抛错）。
    """
    qrcode: str | None = None
    """QR 码 token，作为 ``get_qrcode_status`` 的 query 参数"""

    qrcode_img_content: str | None = None
    """QR 码内容字符串（终端 ASCII 渲染 + 用户手动访问备用链接共用）"""


class QRCodeStatusResponse(ILinkResponseBase):
    """
    ``GET /ilink/bot/get_qrcode_status?qrcode=...`` 响应。

    成功路径必有 ``status``；其他字段按 status 条件可选：
    - status=confirmed → ``bot_token`` + ``ilink_bot_id`` 必有
    - status=scaned_but_redirect → ``redirect_host`` 必有
    - 其他 status → 仅 status 字段
    """
    status: ILinkQRStatusEnum
    """当前扫码状态（成功响应必有；服务端发空响应会导致 pydantic 校验失败）"""

    bot_token: str | None = None
    """登录成功后返回的 Bearer token（仅 status=confirmed 时存在）"""

    ilink_bot_id: str | None = None
    """登录用 bot 的 ID（仅 status=confirmed 时存在）"""

    baseurl: str | None = None
    """登录成功后服务端建议的轮询 base URL（部分 IDC 走专用域名）"""

    ilink_user_id: str | None = None
    """扫码用户的 iLink user_id（status=confirmed 时存在）"""

    redirect_host: str | None = None
    """status=scaned_but_redirect 时给出的 IDC 重定向主机"""


# ===== 媒体引用对象 =====

class CDNMedia(ExtraIgnoreModelBase):
    """
    CDN 媒体引用。

    协议层全部字段都可选（不同消息子类型用不同字段组合），运行时按需检查。
    """
    encrypt_query_param: str | None = None
    """加密的 query 参数（拼接到 full_url 上才能下载）"""

    aes_key: str | None = None
    """AES 密钥（base64 编码）"""

    encrypt_type: int | None = None
    """加密类型（0=只加密 fileid，1=打包缩略图/中图等信息）"""

    full_url: str | None = None
    """完整下载 URL（服务端返回，无需客户端拼接）"""


# ===== 各类 item 子对象 =====

class TextItem(ExtraIgnoreModelBase):
    """
    文本段子对象。

    ``text`` 在协议层可选，但实际场景 TEXT 段必有 text 字段；保持 Optional 以兼容
    服务端可能下发的空 item（虽然罕见）。
    """
    text: str | None = None


class ImageItem(ExtraIgnoreModelBase):
    """
    图片段子对象。

    ``media`` 是核心，几乎必有；``thumb_media`` 仅大图场景会有；``aeskey``（hex 字符串）
    在入站方向**优先**于 ``media.aes_key`` 用于解密。
    """
    media: CDNMedia | None = None
    thumb_media: CDNMedia | None = None
    aeskey: str | None = None
    """16 字节 AES 密钥的 hex 字符串；入站方向优先用此而非 ``media.aes_key``"""

    url: str | None = None
    mid_size: int | None = None
    thumb_size: int | None = None
    thumb_height: int | None = None
    thumb_width: int | None = None
    hd_size: int | None = None


class VoiceItem(ExtraIgnoreModelBase):
    """语音段子对象"""
    media: CDNMedia | None = None
    encode_type: int | None = None
    """1=pcm 2=adpcm 3=feature 4=speex 5=amr 6=silk 7=mp3 8=ogg-speex"""

    bits_per_sample: int | None = None
    sample_rate: int | None = None
    playtime: int | None = None
    """语音长度（毫秒）"""

    text: str | None = None
    """语音转文字内容（若服务端已转写）"""


class FileItem(ExtraIgnoreModelBase):
    """文件段子对象"""
    media: CDNMedia | None = None
    file_name: str | None = None
    md5: str | None = None
    len: str | None = None  # noqa: A003 — 协议字段，不可更名


class VideoItem(ExtraIgnoreModelBase):
    """视频段子对象"""
    media: CDNMedia | None = None
    video_size: int | None = None
    play_length: int | None = None
    video_md5: str | None = None
    thumb_media: CDNMedia | None = None
    thumb_size: int | None = None
    thumb_height: int | None = None
    thumb_width: int | None = None


# ===== 消息段（item） =====

class RefMessage(ExtraIgnoreModelBase):
    """
    引用回复的被引消息。

    ``message_item`` 是被引的消息内容；``title`` 是摘要（可能由服务端生成）。
    都可选——某些场景仅有 title，某些场景仅有 message_item。
    """
    message_item: 'MessageItem | None' = None
    title: str | None = None


class MessageItem(ExtraIgnoreModelBase):
    """
    消息段（``WeixinMessage.item_list`` 的元素）。

    ``type`` 是 discriminator：实际入站每条 item 都有 type；出站构造时也必须给。
    其他字段按 type 条件可选。
    """
    type: ILinkMessageItemType  # noqa: A003
    """段类型（discriminator）"""

    create_time_ms: int | None = None
    update_time_ms: int | None = None
    is_completed: bool | None = None
    msg_id: str | None = None

    ref_msg: RefMessage | None = None
    """如果该段是回复某条消息，引用的原消息信息"""

    text_item: TextItem | None = None
    image_item: ImageItem | None = None
    voice_item: VoiceItem | None = None
    file_item: FileItem | None = None
    video_item: VideoItem | None = None


# 解析前向引用：RefMessage 引用 MessageItem 但顺序在前
RefMessage.model_rebuild()


# ===== 顶层消息 =====

class WeixinMessage(ExtraIgnoreModelBase):
    """
    iLink 顶层消息（``getupdates.msgs[]`` 与 ``sendmessage.msg`` 共享结构）。

    必填字段在入站 / 出站两个方向都协议要求必有（仅值不同）：

    - **``from_user_id``**：入站=用户 iLink ID；出站=空串 ``""``（``send.ts:30`` 明确字面值）
    - **``to_user_id``**：入站=bot 的 iLink ID；出站=目标用户 ID
    - **``client_id``**：入站=服务端生成；出站=客户端用 ``Bridge._generate_client_id()`` 生成
    - **``message_type``**：入站 USER；出站 BOT
    - **``message_state``**：入站含流式状态语义；出站固定 FINISH
    - **``context_token``**：入站=本条消息的回复凭据；出站=回复时用对应入站消息的 token

    剩余字段（``seq`` / ``message_id`` / ``create_time_ms`` 等）按方向条件可选。

    协议层面这些字段缺失会让 iLink 服务端 **静默丢弃** 消息（HTTP 200 + 空响应 body），
    业务层完全无感知——所以必须在 schema 层强制声明，杜绝 ``= None`` 占位让调用方"忘填又跑通"。
    """
    # ===== 两个方向都必填的字段 =====

    from_user_id: str
    to_user_id: str
    client_id: str
    message_type: ILinkMessageType
    message_state: ILinkMessageStateEnum
    context_token: str

    # ===== 仅入站方向有（从 getupdates 响应解析） =====

    seq: int | None = None
    message_id: int | None = None
    """消息 ID（入站方向 iLink 给的；出站方向服务端分配，请求 body 不填）"""

    create_time_ms: int | None = None
    update_time_ms: int | None = None
    delete_time_ms: int | None = None

    session_id: str | None = None
    group_id: str | None = None
    """群消息 ID（iLink 当前不支持群聊，始终缺失或空）"""

    # ===== 内容段（入站含正常消息时必有，GENERATING 等特殊状态可能为 None） =====

    item_list: list[MessageItem] | None = None


# ===== getupdates =====

class GetUpdatesRequest(ILinkRequestWithBaseInfo):
    """
    ``POST /ilink/bot/getupdates`` 请求体。

    协议规定首次请求或重置后须发空串 ""，后续请求带上次响应的 ``get_updates_buf``。
    调用方必须显式传 ``get_updates_buf``——None 表示"未传"，空串 ""
    表示"协议预期的首次请求"，两者语义不同。
    """
    get_updates_buf: str
    """长轮询游标。首次请求由调用方显式传空串 ""，后续传上次响应的值"""


class GetUpdatesResponse(ILinkResponseBase):
    """
    ``POST /ilink/bot/getupdates`` 响应。

    成功路径下 ``get_updates_buf`` 必有；``msgs`` 可能为 None（无新消息批次）或非空列表
    （有新消息）。调用方应当区分"协议返回 None"与"协议返回 []"——本桥接的轮询循环
    统一按"None 与空列表等价"处理（无可处理消息），但字段层保留 None 区分语义。
    """
    msgs: list[WeixinMessage] | None = None
    """新消息列表（None 或空列表都表示本轮无新消息）"""

    get_updates_buf: str | None = None
    """更新后的 buf 游标，调用方应持久化并下次请求携带（成功时必有）"""

    longpolling_timeout_ms: int | None = None
    """服务端建议下次长轮询的超时（毫秒）"""


# ===== sendmessage =====

class SendMessageRequest(ILinkRequestWithBaseInfo):
    """``POST /ilink/bot/sendmessage`` 请求体"""
    msg: WeixinMessage
    """要发送的消息体"""


class SendMessageResponse(ILinkResponseBase):
    """``POST /ilink/bot/sendmessage`` 响应（无业务字段，仅错误码）"""


# ===== sendtyping =====

class SendTypingRequest(ILinkRequestWithBaseInfo):
    """``POST /ilink/bot/sendtyping`` 请求"""
    ilink_user_id: str
    """目标用户 iLink user_id（必填）"""

    typing_ticket: str
    """typing 凭据（从 ``getconfig`` 获取，必填）"""

    status: ILinkTypingStatus = ILinkTypingStatus.TYPING
    """输入状态枚举"""


class SendTypingResponse(ILinkResponseBase):
    """``POST /ilink/bot/sendtyping`` 响应"""


# ===== getconfig =====

class GetConfigRequest(ILinkRequestWithBaseInfo):
    """``POST /ilink/bot/getconfig`` 请求体"""
    ilink_user_id: str
    """要查询配置的用户 iLink user_id（必填）"""

    context_token: str | None = None
    """该用户的 context_token（可选）"""


class GetConfigResponse(ILinkResponseBase):
    """
    ``POST /ilink/bot/getconfig`` 响应。

    成功路径下 ``typing_ticket`` 必有（服务端必返回，用于后续 sendtyping）。
    """
    typing_ticket: str | None = None
    """base64 编码的 typing 凭据（成功时必有）"""


# ===== getuploadurl（媒体上传） =====

class GetUploadUrlRequest(ILinkRequestWithBaseInfo):
    """
    ``POST /ilink/bot/getuploadurl`` 请求。

    所有字段都是协议字段，部分按 media_type 条件必填（如 IMAGE/VIDEO 必须带
    thumb_* 字段除非 no_need_thumb=true）。
    """
    filekey: str
    """本地生成的 file key（一般用 UUID 或类似）"""

    media_type: int
    """见 ILinkUploadMediaType（IMAGE=1/VIDEO=2/FILE=3/VOICE=4）"""

    to_user_id: str
    """目标用户 iLink user_id"""

    rawsize: int
    """原文件明文大小"""

    rawfilemd5: str
    """原文件明文 MD5（hex）"""

    filesize: int
    """加密后文件大小"""

    aeskey: str
    """16 字节 AES key 的 hex 字符串"""

    no_need_thumb: bool = False
    """是否跳过缩略图上传（默认 false）"""

    thumb_rawsize: int | None = None
    """缩略图明文大小（IMAGE/VIDEO 且 no_need_thumb=false 时必填）"""

    thumb_rawfilemd5: str | None = None
    """缩略图明文 MD5（同上）"""

    thumb_filesize: int | None = None
    """缩略图加密后大小（同上）"""


class GetUploadUrlResponse(ILinkResponseBase):
    """
    ``POST /ilink/bot/getuploadurl`` 响应。

    成功时 ``upload_full_url`` 必有；``upload_param`` 通常必有；
    ``thumb_upload_param`` 仅当请求 no_need_thumb=false 才有。
    """
    upload_param: str | None = None
    """原图上传加密参数（成功时必有）"""

    thumb_upload_param: str | None = None
    """缩略图上传加密参数（仅请求时 no_need_thumb=false 才有）"""

    upload_full_url: str | None = None
    """完整上传 URL（成功时必有）"""


# ===== notifystart / notifystop =====

class NotifyRequest(ILinkRequestWithBaseInfo):
    """``notifystart`` / ``notifystop`` 共用请求（仅 base_info）"""


class NotifyResponse(ILinkResponseBase):
    """``notifystart`` / ``notifystop`` 共用响应类型（仅错误码）"""
