"""
Bridge — iLink 协议会话状态（充血模型单例）

严格映射 Tencent/openclaw-weixin@2.4.3 的协议实现：

- ``get_bot_qrcode``: **POST** ``?bot_type=3`` + body ``{local_token_list:[]}``
- ``get_qrcode_status``: **GET** ``?qrcode=<value>``（不带 Authorization header）
- ``getupdates`` / ``sendmessage`` / ``sendtyping`` 等：POST，body 拼接 ``base_info``
- ``X-WECHAT-UIN``: ``base64(str(uint32_be(random4bytes)))``
- ``iLink-App-ClientVersion``: 整数编码 ``(major<<16)|(minor<<8)|patch`` 的十进制字符串
- 错误码同时检查 ``ret``（请求级）和 ``errcode``（业务级）
"""
import asyncio
import base64
import secrets
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, ClassVar, TypeVar
from urllib.parse import urlencode

import aiohttp
import orjson
from loguru import logger as l
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import Field, col
from sqlmodel.ext.asyncio.session import AsyncSession

from sqlmodel_ext import (
    SQLModelBase,
    Str64,
    Str256,
    Str512,
    TableBaseMixin,
)

from constants import BRIDGE_SINGLETON_ID
from meta_config import meta_config
from sqlmodels.bridge.constants import (
    ILINK_APP_CLIENT_VERSION,
    ILINK_APP_ID,
    ILINK_BOT_AGENT,
    ILINK_BOT_TYPE,
    ILINK_CHANNEL_VERSION,
    ILINK_ENDPOINT_GETCONFIG,
    ILINK_ENDPOINT_GETUPDATES,
    ILINK_ENDPOINT_GETUPLOADURL,
    ILINK_ENDPOINT_GET_BOT_QRCODE,
    ILINK_ENDPOINT_GET_QRCODE_STATUS,
    ILINK_ENDPOINT_NOTIFYSTART,
    ILINK_ENDPOINT_NOTIFYSTOP,
    ILINK_ENDPOINT_SENDMESSAGE,
    ILINK_ENDPOINT_SENDTYPING,
    ILINK_ERR_SESSION_EXPIRED,
    ILINK_POLL_TIMEOUT_SECONDS,
    QRCODE_LONG_POLL_TIMEOUT_MS,
    QRCODE_POLL_INTERVAL_SECONDS,
    QRCODE_TOTAL_TIMEOUT_SECONDS,
)
from onebots.models import OneBotPrivateMessageEvent, OneBotSegment
from sqlmodels.bridge.context_token import ContextToken
from sqlmodels.bridge.converters import (
    convert_inbound_message,
    dispatch_outbound_segments,
)
from sqlmodels.bridge.enums import (
    BridgeStateEnum,
    ILinkMessageItemType,
    ILinkMessageStateEnum,
    ILinkMessageType,
    ILinkQRStatusEnum,
    ILinkTypingStatus,
)
from sqlmodels.bridge.exceptions import (
    ILinkAPIError,
    ILinkContextTokenMissingError,
    ILinkSessionExpiredError,
)
from sqlmodels.bridge.id_registry import OneBotMessageIdRegistry
from sqlmodels.bridge.ilink_models import (
    BaseInfo,
    CDNMedia,
    FileItem,
    GetConfigRequest,
    GetConfigResponse,
    GetUpdatesRequest,
    GetUpdatesResponse,
    GetUploadUrlRequest,
    GetUploadUrlResponse,
    ILinkResponseBase,
    ImageItem,
    MessageItem,
    NotifyRequest,
    NotifyResponse,
    QRCodeRequest,
    QRCodeResponse,
    QRCodeStatusResponse,
    SendMessageRequest,
    SendMessageResponse,
    SendTypingRequest,
    SendTypingResponse,
    TextItem,
    VideoItem,
    VoiceItem,
    WeixinMessage,
)
from sqlmodels.server_config.server_config import ServerConfig
from utils.aiohttp_mixin import AioHttpClientSessionMixin
from utils.qrcode_render import render_qrcode_to_terminal

if TYPE_CHECKING:
    from sqlmodels.media_file import MediaFile

_TResponse = TypeVar('_TResponse', bound=ILinkResponseBase)
"""_post_ilink/_get_ilink 辅助方法的泛型参数"""


# ===== Base 与业务模型 =====

class BridgeBase(SQLModelBase):
    """
    Bridge 共享字段基类。

    字段全部由 iLink 协议产生：
    - 扫码登录后由 ``get_qrcode_status`` 返回 bot_token / ilink_user_id (存为 wechat_id)
    - getupdates 增量更新 get_updates_buf
    """
    state: BridgeStateEnum = BridgeStateEnum.idle
    """
    会话生命周期状态。

    - idle: 初始状态，未扫码
    - awaiting_scan: 已获取 QR 码，等待用户扫码
    - running: 扫码成功，正在长轮询
    - expired: 会话过期（errcode/ret 异常），需要重新扫码
    """

    bot_token: Annotated[Str256 | None, Field()] = None
    """iLink Bearer token（扫码登录后由 iLink 返回，过期后置 None）"""

    wechat_id: Annotated[Str64 | None, Field()] = None
    """
    已登录 bot 的 iLink 标识。

    存的是 ``get_qrcode_status`` 响应的 ``ilink_bot_id``（区别于 ``ilink_user_id`` —
    后者是扫码用户的标识）。字段名保留 wechat_id 是历史兼容（DB 列名不动）。
    """

    wechat_nickname: Annotated[Str64 | None, Field()] = None
    """
    昵称占位字段。iLink 协议本身的 QR 流程不返回 bot 昵称，因此通常保持为 None；
    用户在 ServerConfig.bot_display_name 中配置展示名。
    """

    get_updates_buf: Annotated[Str512 | None, Field()] = None
    """
    长轮询游标（iLink ``get_updates_buf`` 字段）。

    每次轮询后持久化，进程重启后从此处恢复，避免重复接收已处理的消息。
    首次请求发空串 ""，每次响应可能返回新 buf。
    """

    account_name: Annotated[Str64, Field(default='default', unique=True, index=True)]
    """账号唯一别名/名称（如 'default', 'bot2'）"""

    self_id: Annotated[int, Field(default=10000, unique=True, index=True)]
    """OneBot v11 虚拟 QQ 号，唯一标识（如 10000, 10001）"""


class Bridge(
    BridgeBase,
    TableBaseMixin,
    AioHttpClientSessionMixin,
    table=True,
):
    """
    iLink 协议会话状态充血模型。

    支持多账号：每个已登录的微信 Bot 对应一张 Bridge 记录。
    """

    # ===== 运行时组件（非 DB 字段） =====

    _id_registry: ClassVar[OneBotMessageIdRegistry | None] = None
    """OneBot ID 映射注册表（ClassVar，进程内单例）"""

    @classmethod
    def get_id_registry(cls) -> OneBotMessageIdRegistry:
        """获取 OneBot ID 映射注册表（lazy 初始化）"""
        if cls._id_registry is None:
            cls._id_registry = OneBotMessageIdRegistry()  # pyright: ignore[reportAttributeAccessIssue]
        return cls._id_registry

    # ===== 多账号查询与单例访问 =====

    @classmethod
    async def get_by_name(cls, session: AsyncSession, account_name: str) -> 'Bridge | None':
        """按 account_name 查询 Bridge 实例"""
        return await cls.get(session, col(cls.account_name) == account_name)

    @classmethod
    async def get_by_self_id(cls, session: AsyncSession, self_id: int) -> 'Bridge | None':
        """按 OneBot self_id 查询 Bridge 实例"""
        return await cls.get(session, col(cls.self_id) == self_id)

    @classmethod
    async def get_all(cls, session: AsyncSession) -> list['Bridge']:
        """获取所有已配置的 Bridge 实例列表"""
        bridges = await cls.get(session, fetch_mode='all')
        return [b for b in bridges] if bridges else []

    @classmethod
    async def get_all_active(cls, session: AsyncSession) -> list['Bridge']:
        """获取所有已就绪（bot_token 非空且非 expired）的 Bridge 实例列表"""
        all_bridges = await cls.get_all(session)
        return [b for b in all_bridges if b.bot_token is not None]

    @classmethod
    async def get_instance(cls, session: AsyncSession, account_name: str = 'default') -> 'Bridge':
        """获取默认或指定 account_name 的 Bridge 实例（向下兼容原有单账号单例用法）"""
        bridge = await cls.get_by_name(session, account_name)
        if bridge is None:
            # 兼容历史数据：若存在 ID=BRIDGE_SINGLETON_ID 的行予以复用
            bridge = await cls.get(session, col(cls.id) == BRIDGE_SINGLETON_ID)
            if bridge is not None:
                bridge.account_name = account_name
                if not bridge.self_id:
                    bridge.self_id = meta_config.ONEBOT_SELF_ID
                bridge = await bridge.save(session)
            else:
                bridge = cls(
                    account_name=account_name,
                    self_id=meta_config.ONEBOT_SELF_ID,
                )
                bridge = await bridge.save(session)
                l.info(f"Bridge 账号 '{account_name}' (self_id={bridge.self_id}) 已创建")
        return bridge

    # ===== iLink HTTP 请求构造 =====

    def _build_endpoint_url(self, endpoint: str, query: Mapping[str, str] | None = None) -> str:
        """拼接完整端点 URL + 可选 query string"""
        base = meta_config.ILINK_API_BASE_URL.rstrip('/')
        # endpoint 是相对路径不带前导斜杠
        url = f"{base}/{endpoint}"
        if query:
            url = f"{url}?{urlencode(query)}"
        return url

    def _generate_client_id(self) -> str:
        """
        生成出站消息的 ``client_id``（``sendmessage`` 必填，iLink 服务端用作去重 / 路由键）。

        格式对齐官方 ``Tencent/openclaw-weixin@2.4.3`` ``util/random.ts:generateId``:
        ``{prefix}:{ms_timestamp}-{8-char hex}``。prefix 用本桥接的项目名以便日志区分来源。

        缺失此字段会让 iLink 静默丢弃消息（HTTP 200 + 空响应 body，但不投递）。
        """
        return f"ilink-onebot:{int(time.time() * 1000)}-{secrets.token_hex(4)}"

    def _generate_wechat_uin_header(self) -> str:
        """
        生成 X-WECHAT-UIN 请求头值。

        协议格式：``base64(str(uint32_be(random4bytes)))``
        步骤：4 随机字节 → 大端解码为 uint32 → 十进制字符串 → base64
        """
        uint32_value = int.from_bytes(secrets.token_bytes(4), 'big')
        decimal_str = str(uint32_value)
        return base64.b64encode(decimal_str.encode('utf-8')).decode('ascii')

    def _build_common_headers(self) -> dict[str, str]:
        """GET 和 POST 都需要的最小公共 header（含 iLink-App-* 标识）"""
        return {
            'iLink-App-Id': ILINK_APP_ID,
            'iLink-App-ClientVersion': str(ILINK_APP_CLIENT_VERSION),
        }

    def _build_post_headers(self) -> dict[str, str]:
        """POST 请求 header（含 Content-Type / AuthorizationType / X-WECHAT-UIN / 可选 Bearer）"""
        headers: dict[str, str] = {
            'Content-Type': 'application/json',
            'AuthorizationType': 'ilink_bot_token',
            'X-WECHAT-UIN': self._generate_wechat_uin_header(),
            **self._build_common_headers(),
        }
        if self.bot_token is not None:
            headers['Authorization'] = f"Bearer {self.bot_token}"
        return headers

    def _build_base_info(self) -> BaseInfo:
        """构造 base_info 元信息（每个 POST body 都拼接）"""
        return BaseInfo(
            channel_version=ILINK_CHANNEL_VERSION,
            bot_agent=ILINK_BOT_AGENT,
        )

    def _check_response_status(
        self,
        response: ILinkResponseBase,
        endpoint: str,
    ) -> None:
        """
        检查响应的 ret / errcode 并在需要时抛异常。

        :raises ILinkSessionExpiredError: errcode == -14（会话过期）
        :raises ILinkAPIError: 其他非 0 错误码
        """
        failure = response.get_failure()
        if failure is None:
            return
        code, message = failure
        if code == ILINK_ERR_SESSION_EXPIRED:
            raise ILinkSessionExpiredError()
        raise ILinkAPIError(errcode=code, errmsg=message, endpoint=endpoint)

    async def _post_ilink(
        self,
        endpoint: str,
        body: SQLModelBase,
        response_model: type[_TResponse],
        *,
        query: Mapping[str, str] | None = None,
        total_timeout: float | None = None,
    ) -> _TResponse:
        """
        发起 POST 请求到 iLink。

        ``body`` 必须是 SQLModel 实例——业务方需要的请求都有专属 Request 模型类
        （如 ``GetUpdatesRequest`` / ``SendMessageRequest``），它们自身携带 ``base_info``
        字段保证协议合规。本方法不做 dict 字面操作，类型严格。

        :param endpoint: 相对路径（不带前导斜杠）
        :param body: 请求体（SQLModel 实例）
        :param response_model: 响应反序列化目标类
        :param query: 可选 query 参数（如 bot_type=3）
        :param total_timeout: 覆盖本次请求的总超时（秒）
        """
        url = self._build_endpoint_url(endpoint, query)
        body_bytes = orjson.dumps(body.model_dump(exclude_none=True))
        timeout = aiohttp.ClientTimeout(total=total_timeout) if total_timeout is not None else None

        async with self._http_request_with_retry(
            'POST',
            url,
            headers=self._build_post_headers(),
            data=body_bytes,
            timeout=timeout,
        ) as resp:
            raw = await resp.read()

        l.debug(
            f"iLink {endpoint} response body: "
            f"{raw.decode('utf-8', errors='replace')[:800]}"
        )
        data = orjson.loads(raw)
        response = response_model.model_validate(data)
        self._check_response_status(response, endpoint)
        return response

    async def _get_ilink(
        self,
        endpoint: str,
        response_model: type[_TResponse],
        *,
        query: Mapping[str, str] | None = None,
        total_timeout: float | None = None,
    ) -> _TResponse:
        """
        发起 GET 请求到 iLink（仅 ``get_qrcode_status`` 使用）。

        GET 不带 Authorization / AuthorizationType / X-WECHAT-UIN，
        只发 iLink-App-Id + iLink-App-ClientVersion 让服务端识别客户端身份。
        """
        url = self._build_endpoint_url(endpoint, query)
        timeout = aiohttp.ClientTimeout(total=total_timeout) if total_timeout is not None else None

        async with self._http_request_with_retry(
            'GET',
            url,
            headers=self._build_common_headers(),
            timeout=timeout,
        ) as resp:
            raw = await resp.read()

        data = orjson.loads(raw)
        response = response_model.model_validate(data)
        self._check_response_status(response, endpoint)
        return response

    # ===== iLink 业务方法：QR 登录 =====

    async def request_qrcode(
        self,
        session: AsyncSession,
        *,
        commit: bool = True,
    ) -> QRCodeResponse:
        """
        调用 ``POST /ilink/bot/get_bot_qrcode?bot_type=3`` 获取扫码 QR。

        请求体携带 ``local_token_list`` 让服务端做去重 / 复用检测；本桥接是
        single-bot 设计，正常发空列表即可。
        """
        # local_token_list：当前已知的 bot_token（用于服务端识别"已登录账号"）
        # single-bot 模式正常为空
        local_token_list: list[str] = []
        if self.bot_token is not None:
            local_token_list.append(self.bot_token)

        # QR 请求不含 base_info（与官方一致，见 login-qr.ts:fetchQRCode）
        request = QRCodeRequest(local_token_list=local_token_list)
        response = await self._post_ilink(
            ILINK_ENDPOINT_GET_BOT_QRCODE,
            body=request,
            response_model=QRCodeResponse,
            query={'bot_type': ILINK_BOT_TYPE},
        )
        self.state = BridgeStateEnum.awaiting_scan
        _ = await self.save(session, commit=commit)
        qrcode_token_preview = (response.qrcode or '')[:8]
        l.info(f"已获取 QR 码 (qrcode={qrcode_token_preview}***)，等待扫码")
        return response

    async def check_qrcode_status(
        self,
        session: AsyncSession,
        qrcode_token: str,
        *,
        commit: bool = True,
        verify_code: str | None = None,
    ) -> QRCodeStatusResponse:
        """
        调用 ``GET /ilink/bot/get_qrcode_status?qrcode=<token>`` 查询扫码进度。

        登录成功时（status=confirmed）自动保存 bot_token + ilink_bot_id 并转入 running。
        """
        query: dict[str, str] = {'qrcode': qrcode_token}
        if verify_code:
            query['verify_code'] = verify_code

        # 服务端长轮询最长 hold 35s，给本地额外 5s 余量再 timeout
        total_timeout = (QRCODE_LONG_POLL_TIMEOUT_MS / 1000.0) + 5.0
        response = await self._get_ilink(
            ILINK_ENDPOINT_GET_QRCODE_STATUS,
            response_model=QRCodeStatusResponse,
            query=query,
            total_timeout=total_timeout,
        )

        if (
            response.status == ILinkQRStatusEnum.confirmed
            and response.bot_token is not None
            and response.ilink_bot_id is not None
        ):
            self.bot_token = response.bot_token
            self.wechat_id = response.ilink_bot_id
            # 协议不返回 nickname；用户在 ServerConfig.bot_display_name 配置展示名
            self.state = BridgeStateEnum.running
            _ = await self.save(session, commit=commit)
            l.success(
                f"扫码登录成功: ilink_bot_id={self.wechat_id}, "
                f"扫码用户 ilink_user_id={response.ilink_user_id}"
            )

        return response

    # ===== iLink 业务方法：长轮询 =====

    async def poll_updates(
        self,
        session: AsyncSession,
        *,
        commit: bool = True,
    ) -> list[WeixinMessage]:
        """
        调用 ``POST /ilink/bot/getupdates`` 长轮询新消息。

        请求体携带当前游标 ``get_updates_buf``（首次为空串）；服务端 hold 最多 35s。
        返回后持久化新游标，供进程重启后恢复。
        """
        request = GetUpdatesRequest(
            get_updates_buf=self.get_updates_buf or '',
            base_info=self._build_base_info(),
        )

        # 服务端 hold 长轮询，给本地额外 10s 余量
        total_timeout = float(ILINK_POLL_TIMEOUT_SECONDS + 10)
        response = await self._post_ilink(
            ILINK_ENDPOINT_GETUPDATES,
            body=request,
            response_model=GetUpdatesResponse,
            total_timeout=total_timeout,
        )

        # 更新游标
        if response.get_updates_buf is not None and response.get_updates_buf != self.get_updates_buf:
            self.get_updates_buf = response.get_updates_buf
            _ = await self.save(session, commit=commit)

        return response.msgs or []

    # ===== iLink 业务方法：发送消息 =====

    async def send_text(
        self,
        session: AsyncSession,
        user_id: str,
        content: str,
    ) -> str | None:
        """
        发送文本消息给指定 iLink user_id。

        :param user_id: iLink user_id（ContextToken 表中存的 user_name 字段）
        :param content: 文本内容
        :return: iLink 分配的 message_id（None 表示服务端未返回）
        :raises ILinkContextTokenMissingError: 该用户还未向 bot 发过消息（无 context_token）
        """
        context_token_row = await ContextToken.get(
            session,
            (col(ContextToken.user_name) == user_id) & (col(ContextToken.bridge_id) == self.id),
        )
        if context_token_row is None:
            context_token_row = await ContextToken.get(
                session, col(ContextToken.user_name) == user_id,
            )
        if context_token_row is None:
            raise ILinkContextTokenMissingError(user_id)

        item = MessageItem(
            type=ILinkMessageItemType.TEXT,
            text_item=TextItem(text=content),
        )
        # ``from_user_id=""`` 与 ``client_id`` 是 iLink 服务端的硬性要求（与官方
        # ``send.ts:buildTextMessageReq`` 对齐）。缺失任一字段 iLink 会返回 200 +
        # 空 body 但静默丢弃消息——业务层完全无感知，必须显式填上。
        msg = WeixinMessage(
            from_user_id='',
            to_user_id=user_id,
            client_id=self._generate_client_id(),
            message_type=ILinkMessageType.BOT,
            message_state=ILinkMessageStateEnum.FINISH,
            context_token=context_token_row.token,
            item_list=[item],
        )
        request = SendMessageRequest(msg=msg, base_info=self._build_base_info())
        # SendMessageResp 是空 body；用其 ret/errcode 状态判断
        _ = await self._post_ilink(
            ILINK_ENDPOINT_SENDMESSAGE,
            body=request,
            response_model=SendMessageResponse,
        )
        # iLink sendmessage 响应不返回 message_id，调用方据此理解为"成功标志"
        return None

    async def send_media(
        self,
        session: AsyncSession,
        user_id: str,
        media_file: 'MediaFile',
        *,
        caption: str | None = None,
    ) -> None:
        """
        发送媒体消息给指定 iLink user_id。

        前置：``media_file`` 必须已经走完 ``prepare_and_upload(bridge, session, user_id)``，
        即 ``cdn_encrypt_query_param`` / ``cdn_aes_key_base64`` / ``filekey`` 等字段已填好。

        按 ``media_file.item_type`` 构造对应 ``MessageItem.<type>_item`` 嵌套结构 →
        包装成 ``WeixinMessage`` → 发 sendmessage。可选的 ``caption`` 作为额外 TEXT 段
        随媒体段一起发送（iLink 协议允许 ``item_list`` 含多个 item）。

        :raises ILinkContextTokenMissingError: 该用户没有 context_token
        :raises ValueError: ``media_file`` 未走完 ``prepare_and_upload`` 流程
        """
        context_token_row = await ContextToken.get(
            session,
            (col(ContextToken.user_name) == user_id) & (col(ContextToken.bridge_id) == self.id),
        )
        if context_token_row is None:
            context_token_row = await ContextToken.get(
                session, col(ContextToken.user_name) == user_id,
            )
        if context_token_row is None:
            raise ILinkContextTokenMissingError(user_id)

        media_item = _build_message_item_from_media(media_file)
        item_list: list[MessageItem] = [media_item]
        if caption is not None and caption.strip():
            # caption 作为独立 TEXT item 与媒体 item 一起送出
            item_list.append(MessageItem(
                type=ILinkMessageItemType.TEXT,
                text_item=TextItem(text=caption),
            ))

        # 与 ``send_text`` 同样必须填 ``from_user_id=""`` + ``client_id``
        msg = WeixinMessage(
            from_user_id='',
            to_user_id=user_id,
            client_id=self._generate_client_id(),
            message_type=ILinkMessageType.BOT,
            message_state=ILinkMessageStateEnum.FINISH,
            context_token=context_token_row.token,
            item_list=item_list,
        )
        request = SendMessageRequest(msg=msg, base_info=self._build_base_info())
        _ = await self._post_ilink(
            ILINK_ENDPOINT_SENDMESSAGE,
            body=request,
            response_model=SendMessageResponse,
        )

    async def get_upload_url(
        self,
        *,
        filekey: str,
        media_type: int,
        to_user_id: str,
        rawsize: int,
        rawfilemd5: str,
        filesize: int,
        no_need_thumb: bool = True,
        aeskey: str,
    ) -> GetUploadUrlResponse:
        """
        请求 iLink CDN 上传 URL。

        对齐官方 ``api.ts:getUploadUrl``，``no_need_thumb`` 默认 True（与上传管道
        一致——本桥接不生成缩略图）。

        :param filekey: 本地生成的 16 字节 hex file key
        :param media_type: 见 ``ILinkUploadMediaType``（IMAGE=1/VIDEO=2/FILE=3/VOICE=4）
        :param to_user_id: 目标用户 iLink user_id
        :param rawsize: 明文字节大小
        :param rawfilemd5: 明文 MD5（hex）
        :param filesize: AES 加密后字节大小
        :param no_need_thumb: 是否跳过缩略图上传 URL（默认 True）
        :param aeskey: 16 字节 AES key 的 hex 字符串
        :return: 含 ``upload_full_url`` 的响应
        """
        request = GetUploadUrlRequest(
            filekey=filekey,
            media_type=media_type,
            to_user_id=to_user_id,
            rawsize=rawsize,
            rawfilemd5=rawfilemd5,
            filesize=filesize,
            no_need_thumb=no_need_thumb,
            aeskey=aeskey,
            base_info=self._build_base_info(),
        )
        return await self._post_ilink(
            ILINK_ENDPOINT_GETUPLOADURL,
            body=request,
            response_model=GetUploadUrlResponse,
        )

    async def get_config(
        self,
        ilink_user_id: str,
        *,
        context_token: str | None = None,
    ) -> GetConfigResponse:
        """
        获取指定用户的 typing 配置（含 ``typing_ticket``）。

        :param ilink_user_id: 目标用户 iLink user_id
        :param context_token: 该用户的 context_token（可选）
        :return: 含 ``typing_ticket`` 的响应
        """
        request = GetConfigRequest(
            ilink_user_id=ilink_user_id,
            context_token=context_token,
            base_info=self._build_base_info(),
        )
        return await self._post_ilink(
            ILINK_ENDPOINT_GETCONFIG,
            body=request,
            response_model=GetConfigResponse,
        )

    async def send_typing(
        self,
        session: AsyncSession,
        ilink_user_id: str,
        *,
        status: ILinkTypingStatus = ILinkTypingStatus.TYPING,
    ) -> None:
        """
        给指定用户发 typing 状态。

        先调 ``get_config`` 获取 ``typing_ticket``，再发 sendtyping。

        :param ilink_user_id: 目标用户 iLink user_id
        :param status: 输入状态（默认 TYPING）
        :raises ILinkContextTokenMissingError: 该用户没有 context_token（无法 get_config）
        :raises ILinkAPIError: 上游协议错误
        """
        context_token_row = await ContextToken.get(
            session,
            (col(ContextToken.user_name) == ilink_user_id) & (col(ContextToken.bridge_id) == self.id),
        )
        if context_token_row is None:
            context_token_row = await ContextToken.get(
                session, col(ContextToken.user_name) == ilink_user_id,
            )
        if context_token_row is None:
            raise ILinkContextTokenMissingError(ilink_user_id)

        config_response = await self.get_config(
            ilink_user_id=ilink_user_id,
            context_token=context_token_row.token,
        )
        if config_response.typing_ticket is None:
            raise ILinkAPIError(
                errcode=0,
                errmsg=f"getconfig 未返回 typing_ticket: {config_response}",
                endpoint=ILINK_ENDPOINT_GETCONFIG,
            )

        request = SendTypingRequest(
            ilink_user_id=ilink_user_id,
            typing_ticket=config_response.typing_ticket,
            status=status,
            base_info=self._build_base_info(),
        )
        _ = await self._post_ilink(
            ILINK_ENDPOINT_SENDTYPING,
            body=request,
            response_model=SendTypingResponse,
        )

    async def notify_start(self) -> None:
        """
        通知 iLink 服务端本客户端已启动（``notifystart``）。

        在 ``typers/run.py`` 启动主服务时调用，让服务端不把本 bot 误判为离线。
        失败时仅日志告警，不阻塞启动（best-effort，与官方语义一致）。
        """
        try:
            _ = await self._post_ilink(
                ILINK_ENDPOINT_NOTIFYSTART,
                body=NotifyRequest(base_info=self._build_base_info()),
                response_model=NotifyResponse,
            )
            l.info("iLink notifystart 已发送")
        except (ILinkAPIError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            l.warning(f"iLink notifystart 失败（不阻塞启动）: {type(e).__name__}: {e}")

    async def notify_stop(self) -> None:
        """
        通知 iLink 服务端本客户端即将下线（``notifystop``）。

        在 ``typers/run.py`` 优雅关闭时调用。失败时仅日志告警，不阻塞 shutdown。
        """
        try:
            _ = await self._post_ilink(
                ILINK_ENDPOINT_NOTIFYSTOP,
                body=NotifyRequest(base_info=self._build_base_info()),
                response_model=NotifyResponse,
            )
            l.info("iLink notifystop 已发送")
        except (ILinkAPIError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            l.warning(f"iLink notifystop 失败（不阻塞 shutdown）: {type(e).__name__}: {e}")

    # ===== 入站消息处理（薄包装，委托 converters 模块） =====

    async def handle_inbound_message(
        self,
        session: AsyncSession,
        msg: WeixinMessage,
        *,
        commit: bool = True,
    ) -> OneBotPrivateMessageEvent | None:
        """
        处理单条 iLink 入站消息：UPSERT context_token → 转换为 OneBot 事件。

        :return: OneBot 私聊事件，GENERATING 状态时返回 None（应跳过广播）
        """
        # 入站消息必有 from_user_id；防御性返回 None 跳过损坏数据
        if msg.from_user_id is None:
            l.warning(f"iLink 入站消息缺少 from_user_id，跳过: msg_id={msg.message_id}")
            return None

        # GENERATING 跳过：OneBot v11 无流式概念
        if msg.message_state == ILinkMessageStateEnum.GENERATING:
            return None

        # UPSERT context_token（每条消息都可能更新该用户的 token）
        if msg.context_token:
            existing = await ContextToken.get(
                session,
                (col(ContextToken.user_name) == msg.from_user_id) & (col(ContextToken.bridge_id) == self.id),
            )
            if existing is None:
                _ = await ContextToken(
                    bridge_id=self.id,
                    user_name=msg.from_user_id,
                    token=msg.context_token,
                ).save(session, commit=commit)
            elif existing.token != msg.context_token:
                existing.token = msg.context_token
                _ = await existing.save(session, commit=commit)

        # 委托转换器构造 OneBot 事件
        return await convert_inbound_message(
            bridge=self,
            session=session,
            msg=msg,
            id_registry=self.get_id_registry(),
        )

    # ===== 出站消息处理（薄包装，委托 converters 模块） =====

    async def resolve_user_id_from_onebot(
        self,
        session: AsyncSession,
        onebot_user_id: int,
    ) -> str | None:
        """
        反查 OneBot user_id 对应的 iLink user_id。

        SHA-256 截断哈希是单向的，必须曾经被 ``resolve_user_id`` 处理过才能反查。
        逻辑：先查内存缓存，未命中则扫描 ContextToken 表逐行 hash 填充并反查。
        """
        registry = self.get_id_registry()
        cached = registry.lookup_user_name_cached(onebot_user_id)
        if cached is not None:
            return cached

        all_tokens = await ContextToken.get(
            session,
            col(ContextToken.bridge_id) == self.id,
            fetch_mode='all',
        )
        if not all_tokens:
            all_tokens = await ContextToken.get(session, fetch_mode='all')
        if all_tokens is None:
            return None
        for row in all_tokens:
            registry.resolve_user_id(row.user_name)
            name = registry.lookup_user_name_cached(onebot_user_id)
            if name is not None:
                return name

        return registry.lookup_user_name_cached(onebot_user_id)

    async def handle_outbound_send_message(
        self,
        session: AsyncSession,
        onebot_user_id: int,
        segments: list[OneBotSegment],
    ) -> str | None:
        """
        处理 OneBot send_private_msg：把段数组转为 iLink 调用。

        :raises ValueError: 找不到 onebot_user_id 对应的 iLink user_id
        """
        ilink_user_id = await self.resolve_user_id_from_onebot(session, onebot_user_id)
        if ilink_user_id is None:
            raise ValueError(
                f"OneBot user_id {onebot_user_id} 对应的 iLink user_id 未知，"
                "该用户必须先向 bot 发一条消息建立 context_token 才能被回复"
            )

        return await dispatch_outbound_segments(
            bridge=self,
            session=session,
            target_user_id=ilink_user_id,
            segments=segments,
        )

    # ===== 会话状态管理 =====

    async def handle_session_expired(
        self,
        session: AsyncSession,
        config: ServerConfig,
        *,
        commit: bool = True,
    ) -> None:
        """处理会话过期（errcode -14）"""
        self.state = BridgeStateEnum.expired
        if config.auto_logout_on_session_expired:
            self.bot_token = None
        _ = await self.save(session, commit=commit)
        l.warning(
            f"iLink 会话过期，Bridge 状态转为 expired "
            f"(auto_logout={config.auto_logout_on_session_expired})"
        )

    # ===== 扫码登录流程（首次 login + 自动 relogin 共用） =====

    async def run_qrcode_login(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        prompt_message: str | None = None,
    ) -> bool:
        """
        完整扫码登录流程：请求 QR → 终端渲染 → 轮询状态直到结束。

        :return: True=登录成功；False=超时 / QR 过期 / 协议错误
        """
        # Step 1: 请求 QR 码
        async with session_factory() as session:
            qr_response = await self.request_qrcode(session)

        if qr_response.qrcode is None or qr_response.qrcode_img_content is None:
            l.error(
                f"iLink 返回的 QR 码缺少 qrcode 或 qrcode_img_content: {qr_response}"
            )
            return False

        # Step 2: 提示 + 终端渲染
        if prompt_message is not None:
            l.warning(prompt_message)
        render_qrcode_to_terminal(qr_response.qrcode_img_content)

        # Step 3: 轮询扫码状态
        elapsed = 0.0
        qrcode_token = qr_response.qrcode

        while elapsed < QRCODE_TOTAL_TIMEOUT_SECONDS:
            await asyncio.sleep(QRCODE_POLL_INTERVAL_SECONDS)
            elapsed += QRCODE_POLL_INTERVAL_SECONDS

            async with session_factory() as session:
                fresh_bridge = await Bridge.get_instance(session)
                status_response = await fresh_bridge.check_qrcode_status(
                    session, qrcode_token,
                )

            if status_response.status == ILinkQRStatusEnum.confirmed:
                l.success(
                    f"扫码登录成功: ilink_bot_id={status_response.ilink_bot_id}"
                )
                # check_qrcode_status 已经把 bridge 转回 running 并保存
                self.state = BridgeStateEnum.running
                self.bot_token = status_response.bot_token
                self.wechat_id = status_response.ilink_bot_id
                return True

            if status_response.status == ILinkQRStatusEnum.binded_redirect:
                l.success("此 bot 已绑定到本应用（binded_redirect），视为登录成功")
                # 不重写 bot_token（保持现有 DB 中的）
                self.state = BridgeStateEnum.running
                return True

            if status_response.status == ILinkQRStatusEnum.expired:
                l.error("QR 码已过期")
                return False

            if status_response.status == ILinkQRStatusEnum.verify_code_blocked:
                l.error("配对码多次错误已被阻断")
                return False

            if status_response.status == ILinkQRStatusEnum.need_verifycode:
                l.error(
                    "iLink 要求输入配对码（need_verifycode）。当前实现暂不支持交互输入，"
                    "请重新扫码或在新版本中实现 verify_code 流程"
                )
                return False

            # wait / scaned / scaned_but_redirect：继续轮询
            # （IDC 重定向较少见，暂不实现 baseurl 切换；如真出现服务端会持续返回该状态直到超时）

        l.error(f"扫码超时（{int(QRCODE_TOTAL_TIMEOUT_SECONDS)} 秒未完成）")
        return False


# ===== 模块级辅助函数 =====

# silk 编码类型（VoiceItem.encode_type）：6 对应 silk
# 与官方 ``types.ts`` 注释一致：1=pcm 2=adpcm 3=feature 4=speex 5=amr 6=silk 7=mp3 8=ogg-speex
_VOICE_ENCODE_TYPE_SILK: int = 6


def _build_message_item_from_media(media_file: 'MediaFile') -> MessageItem:
    """
    把出站 ``MediaFile`` 转换为 iLink ``MessageItem``。

    按 ``media_file.item_type`` 构造对应 ``<type>_item`` 嵌套结构：

    - IMAGE：``ImageItem.media`` + ``ImageItem.aeskey`` + ``mid_size``
    - VOICE：``VoiceItem.media`` + ``encode_type=6`` (silk)
    - FILE：``FileItem.media`` + ``file_name`` + ``md5`` + ``len``
    - VIDEO：``VideoItem.media`` + ``video_size`` + ``video_md5``

    前置条件：``media_file`` 已经走完 ``prepare_and_upload(...)``，``cdn_encrypt_query_param``
    / ``cdn_aes_key_base64`` 等字段已填好。

    :raises ValueError: ``media_file`` 缺少 CDN 字段，或 ``item_type`` 不是媒体类型
    """
    if media_file.cdn_encrypt_query_param is None:
        raise ValueError(
            f"MediaFile {media_file.id} 缺少 cdn_encrypt_query_param，"
            "请先调用 prepare_and_upload"
        )
    if media_file.cdn_aes_key_base64 is None:
        raise ValueError(f"MediaFile {media_file.id} 缺少 cdn_aes_key_base64")

    cdn_media = CDNMedia(
        encrypt_query_param=media_file.cdn_encrypt_query_param,
        aes_key=media_file.cdn_aes_key_base64,
        encrypt_type=media_file.cdn_encrypt_type or 1,
    )

    if media_file.item_type == ILinkMessageItemType.IMAGE:
        if media_file.image_aeskey_hex is None:
            raise ValueError(f"IMAGE MediaFile {media_file.id} 缺少 image_aeskey_hex")
        return MessageItem(
            type=ILinkMessageItemType.IMAGE,
            image_item=ImageItem(
                media=cdn_media,
                aeskey=media_file.image_aeskey_hex,
                mid_size=media_file.ciphertext_size,
            ),
        )

    if media_file.item_type == ILinkMessageItemType.VOICE:
        return MessageItem(
            type=ILinkMessageItemType.VOICE,
            voice_item=VoiceItem(
                media=cdn_media,
                encode_type=_VOICE_ENCODE_TYPE_SILK,
            ),
        )

    if media_file.item_type == ILinkMessageItemType.FILE:
        return MessageItem(
            type=ILinkMessageItemType.FILE,
            file_item=FileItem(
                media=cdn_media,
                file_name=media_file.file_name,
                md5=media_file.plaintext_md5_hex,
                len=str(media_file.plaintext_size) if media_file.plaintext_size else None,
            ),
        )

    if media_file.item_type == ILinkMessageItemType.VIDEO:
        return MessageItem(
            type=ILinkMessageItemType.VIDEO,
            video_item=VideoItem(
                media=cdn_media,
                video_size=media_file.plaintext_size,
                video_md5=media_file.plaintext_md5_hex,
            ),
        )

    raise ValueError(
        f"_build_message_item_from_media 不支持 item_type={media_file.item_type!r}"
    )
