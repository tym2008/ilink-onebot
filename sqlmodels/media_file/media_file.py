"""
MediaFile — 媒体文件充血模型（统一入站 + 出站）

对齐腾讯官方 ``Tencent/openclaw-weixin@2.4.3`` 的协议字段嵌套结构（``CDNMedia``
+ 各 ``<type>_item`` 子对象），管理 iLink 协议媒体文件的完整生命周期：

**入站方向**（iLink → OneBot 框架）：

1. ``create_inbound_from_message_item(session, item, ilink_msg_id)`` 从
   ``MessageItem.<type>_item.media`` / ``image_item.aeskey`` 提取字段创建记录
2. ``ensure_decrypted(bridge, session)`` 触发懒下载 + AES 解密落盘缓存
3. 通过 FastAPI 媒体服务 ``/api/v1/media/<id>`` 暴露给 OneBot 框架访问

**出站方向**（OneBot 框架 → iLink）：

1. ``create_outbound_from_onebot_segment(session, segment, ilink_msg_type)``
   从 OneBot segment 创建记录（含本地生成的 AES key / filekey）
2. ``fetch_outbound_source(bridge, session)`` 从 ``onebot_source_url`` 拉取明文到本地
3. ``prepare_and_upload(bridge, session, to_user_id)`` 计算 md5/size → 调
   ``Bridge.get_upload_url`` → AES 流式加密 → PUT 上传 → 回填 CDN 字段
4. 调用方据此调用 ``Bridge.send_media(media_file)`` 触发 ``sendmessage``

字段语义：

- **CDN media 三件套**（``cdn_full_url`` / ``cdn_encrypt_query_param`` / ``cdn_aes_key_base64``）：
  入站方向直接从协议响应取；出站方向是上传后 CDN 响应的回写
- **``image_aeskey_hex``**：仅 IMAGE 段使用。入站方向**优先**用此字段解密
  （比 ``cdn_aes_key_base64`` 优先）；出站方向作为 ``image_item.aeskey`` 协议字段填入
- **``filekey``**：出站本地生成的 16 字节 hex，用于 ``getuploadurl`` 的 ``filekey`` 字段
- **``plaintext_md5_hex`` / ``plaintext_size`` / ``ciphertext_size``**：出站 ``getuploadurl``
  必填字段，本地计算后持久化（支持中断重试）
- **``ilink_msg_id``**：入站方向关联的 iLink message_id，用于错误日志追踪

判断"是否已就绪"的真相源：``local_path is not None 且文件存在``。不引入额外
``is_cached`` 字段，避免双字段不一致。
"""
import base64
import hashlib
import secrets
from binascii import Error as BinasciiError
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from urllib.parse import unquote, urlparse

import aiofiles
import aiofiles.os
from loguru import logger as l
from sqlmodel import Field
from sqlmodel.ext.asyncio.session import AsyncSession

from sqlmodel_ext import (
    NonNegativeInt,
    SQLModelBase,
    Str64,
    Str256,
    Str512,
    Str2048,
    TableBaseMixin,
)

from meta_config import meta_config
from onebots.models import OneBotSegment
from sqlmodels.bridge.enums import (
    ILinkMessageItemType,
    ILinkUploadMediaType,
)
from sqlmodels.bridge.exceptions import (
    MediaInboundError,
    MediaOutboundError,
)
from sqlmodels.bridge.ilink_models import MessageItem
from utils.crypto import (
    StreamAESDecryptor,
    StreamAESEncryptor,
    aes_ecb_padded_size,
    cdn_aes_key_from_base64,
    cdn_aes_key_to_base64,
    generate_aes_key,
    image_aes_key_from_hex,
    image_aes_key_to_hex,
)

if TYPE_CHECKING:
    from sqlmodels.bridge.bridge import Bridge


# ===== 模块私有常量 =====

_DOWNLOAD_CHUNK_SIZE: int = 64 * 1024
"""HTTP 下载/上传时每个 chunk 的字节数（64 KB，aiohttp 推荐）"""

_MAX_BASE64_DECODED_BYTES: int = 32 * 1024 * 1024
"""``base64://`` 解码后允许的最大字节数（防御 OOM / 磁盘填满）"""


# ===== 枚举 =====

class MediaFileDirectionEnum(StrEnum):
    """媒体文件方向"""
    inbound = 'inbound'
    """入站：iLink CDN → 解密 → 本地缓存 → OneBot 访问"""

    outbound = 'outbound'
    """出站：OneBot source → 本地暂存 → 加密 → 上传到 iLink CDN → sendmessage"""


# ===== Base 与业务模型 =====

class MediaFileBase(SQLModelBase):
    """
    MediaFile 共享字段基类。

    所有协议字段都按 ``CDNMedia`` 嵌套结构平铺存储（DB 表单不支持嵌套），
    业务方法负责入参 ``MessageItem`` ↔ 表字段的映射。
    """

    direction: MediaFileDirectionEnum
    """方向（入站/出站）"""

    item_type: ILinkMessageItemType
    """iLink 消息段类型（决定 AES key 来源 + getuploadurl 的 media_type）"""

    # ===== CDN media 三件套（入站从协议取得，出站上传后回写） =====

    cdn_full_url: Annotated[Str2048 | None, Field()] = None
    """``CDNMedia.full_url``：完整 CDN URL（优先用于下载）。出站方向不直接用此字段"""

    cdn_encrypt_query_param: Annotated[Str2048 | None, Field()] = None
    """
    ``CDNMedia.encrypt_query_param``：加密 query 参数。

    入站：协议返回（``cdn_full_url`` 缺失时作为 fallback 拼接下载 URL）。
    出站：CDN upload 响应的 ``x-encrypted-param`` header，写入此字段供后续
    ``sendmessage`` 拼到 ``MessageItem.<type>_item.media.encrypt_query_param``。
    """

    cdn_aes_key_base64: Annotated[Str256 | None, Field()] = None
    """
    ``CDNMedia.aes_key``：base64 编码的 AES key。

    VOICE / FILE / VIDEO 段用此字段解密；IMAGE 段作为 ``image_aeskey_hex`` 的 fallback。
    出站方向所有类型都填此字段（与官方一致）。
    """

    cdn_encrypt_type: int | None = None
    """``CDNMedia.encrypt_type``（0=只加密 fileid / 1=打包缩略图等）。出站方向固定 1"""

    # ===== IMAGE 专用 hex AES key =====

    image_aeskey_hex: Annotated[Str64 | None, Field()] = None
    """
    ``ImageItem.aeskey``：16 字节 AES key 的 hex 字符串。

    入站方向：仅 ``item_type == IMAGE`` 时填充；**优先**于 ``cdn_aes_key_base64`` 解密。
    出站方向：仅 ``item_type == IMAGE`` 时填充，作为 ``image_item.aeskey`` 协议字段。
    """

    # ===== 出站方向独有 =====

    filekey: Annotated[Str64 | None, Field()] = None
    """
    ``getuploadurl.filekey``：本地生成的 16 字节 hex file key。

    用于服务端识别上传请求；出站方向必填，入站方向为 None。
    """

    onebot_source_url: Annotated[Str2048 | None, Field()] = None
    """
    OneBot 框架传来的媒体源（出站方向独有）。

    支持四种 scheme：``http(s)://`` / ``file://`` / ``base64://`` / 裸路径。
    入站方向为 None。
    """

    plaintext_md5_hex: Annotated[Str64 | None, Field()] = None
    """
    ``getuploadurl.rawfilemd5``：原文件明文 MD5（hex）。

    出站方向：``prepare_and_upload`` 读 ``local_path`` 计算后填入。
    """

    plaintext_size: Annotated[NonNegativeInt | None, Field()] = None
    """
    ``getuploadurl.rawsize``：原文明文大小（字节）。

    入站方向：解密完成后填（用于 OneBot 段元数据）。
    出站方向：``fetch_outbound_source`` 落盘后填（用于 ``getuploadurl`` 请求）。
    """

    ciphertext_size: Annotated[NonNegativeInt | None, Field()] = None
    """
    ``getuploadurl.filesize``：AES 加密后字节大小。

    公式：``ceil((plaintext_size + 1) / 16) * 16``。出站方向：``prepare_and_upload`` 填入。
    """

    # ===== 通用元数据 =====

    file_name: Str256
    """文件名（可能带扩展名）。入站从协议或默认推断；出站从 OneBot data['name'] 或 URL 提取"""

    mime_type: Annotated[Str64 | None, Field()] = None
    """MIME 类型（通过文件名或内容嗅探得到）"""

    local_path: Annotated[Str512 | None, Field()] = None
    """
    本地落盘绝对路径。

    入站：``ensure_decrypted`` 解密后的明文（VOICE 段为 silk → wav 转码后的 wav）。
    出站：``fetch_outbound_source`` 落盘的源明文。
    """

    # ===== 关联 iLink 消息 =====

    ilink_msg_id: Annotated[Str64 | None, Field()] = None
    """关联的 iLink ``message_id``（入站方向用于错误追踪；出站方向为 None）"""


class MediaFile(MediaFileBase, TableBaseMixin, table=True):
    """媒体文件充血模型（统一入站 / 出站）"""

    # ===== 通用属性 =====

    @property
    def url_suffix(self) -> str:
        """媒体文件的相对 URL 路径（如 ``/api/v1/media/42``），由媒体服务拼接成完整 URL"""
        if self.id is None:
            raise ValueError("MediaFile 必须先 save 获取 ID 才能生成 url_suffix")
        return f"/api/v1/media/{self.id}"

    def aes_key_bytes(self) -> bytes:
        """
        根据 ``item_type`` 决定使用 hex 还是 base64 字段，返回 16 字节 AES key。

        IMAGE：优先 ``image_aeskey_hex``，fallback ``cdn_aes_key_base64``。
        VOICE / FILE / VIDEO：``cdn_aes_key_base64``。

        :raises ValueError: 字段缺失或解码失败
        """
        if self.item_type == ILinkMessageItemType.IMAGE:
            if self.image_aeskey_hex is not None:
                return image_aes_key_from_hex(self.image_aeskey_hex)
            if self.cdn_aes_key_base64 is not None:
                return cdn_aes_key_from_base64(self.cdn_aes_key_base64)
            raise ValueError(
                f"MediaFile {self.id} (IMAGE) 缺少 image_aeskey_hex 和 cdn_aes_key_base64"
            )
        if self.cdn_aes_key_base64 is None:
            raise ValueError(
                f"MediaFile {self.id} ({self.item_type.name}) 缺少 cdn_aes_key_base64"
            )
        return cdn_aes_key_from_base64(self.cdn_aes_key_base64)

    # ===== 入站工厂 =====

    @classmethod
    async def create_inbound_from_message_item(
        cls,
        session: AsyncSession,
        item: MessageItem,
        ilink_msg_id: int | None,
    ) -> 'MediaFile':
        """
        从 iLink ``MessageItem`` 创建入站 ``MediaFile`` 记录并 save。

        按 ``item.type`` 分发到对应的 ``<type>_item`` 子对象提取字段：

        - IMAGE：``image_item.media.full_url`` + ``image_item.aeskey``（hex 优先） + ``image_item.media.aes_key``
        - VOICE：``voice_item.media.full_url`` + ``voice_item.media.aes_key``
        - FILE：``file_item.media.full_url`` + ``file_item.media.aes_key`` + ``file_item.file_name``
        - VIDEO：``video_item.media.full_url`` + ``video_item.media.aes_key``

        缺失关键字段时抛 ``MediaInboundError`` 而非保存"半完整"行——让上层立刻感知。

        :raises MediaInboundError: ``item.type`` 不是媒体类型 / 关键字段缺失
        """
        msg_id_repr: str | int = ilink_msg_id if ilink_msg_id is not None else 'unknown'

        if item.type == ILinkMessageItemType.IMAGE:
            cdn_full_url, encrypt_query_param, aes_key_base64, encrypt_type, file_name = (
                _extract_image_item_fields(item, msg_id_repr)
            )
            image_aeskey_hex = (item.image_item.aeskey if item.image_item is not None else None)
        elif item.type == ILinkMessageItemType.VOICE:
            cdn_full_url, encrypt_query_param, aes_key_base64, encrypt_type, file_name = (
                _extract_voice_item_fields(item, msg_id_repr)
            )
            image_aeskey_hex = None
        elif item.type == ILinkMessageItemType.FILE:
            cdn_full_url, encrypt_query_param, aes_key_base64, encrypt_type, file_name = (
                _extract_file_item_fields(item, msg_id_repr)
            )
            image_aeskey_hex = None
        elif item.type == ILinkMessageItemType.VIDEO:
            cdn_full_url, encrypt_query_param, aes_key_base64, encrypt_type, file_name = (
                _extract_video_item_fields(item, msg_id_repr)
            )
            image_aeskey_hex = None
        else:
            raise MediaInboundError(
                ilink_msg_id=msg_id_repr,
                stage='create',
                reason=f"item.type={item.type!r} 不是已知的媒体类型",
            )

        mime_type = _guess_mime_type(file_name, item.type)
        instance = cls(
            direction=MediaFileDirectionEnum.inbound,
            item_type=item.type,
            cdn_full_url=cdn_full_url,
            cdn_encrypt_query_param=encrypt_query_param,
            cdn_aes_key_base64=aes_key_base64,
            cdn_encrypt_type=encrypt_type,
            image_aeskey_hex=image_aeskey_hex,
            file_name=file_name,
            mime_type=mime_type,
            ilink_msg_id=str(ilink_msg_id) if ilink_msg_id is not None else None,
        )
        return await instance.save(session)

    # ===== 入站：下载 + 解密 =====

    async def ensure_decrypted(
        self,
        bridge: 'Bridge',
        session: AsyncSession,
        *,
        commit: bool = True,
    ) -> Path:
        """
        触发懒下载 + AES 解密 + （VOICE 自动 silk→wav）的本地缓存。

        缓存命中（``local_path`` 文件存在）时直接返回。

        :raises MediaInboundError: 字段缺失 / 下载失败 / 解密失败 / silk 转码失败
        :raises ValueError: 调用方向错误（仅入站）
        """
        if self.direction != MediaFileDirectionEnum.inbound:
            raise ValueError(
                f"ensure_decrypted 仅适用于入站方向，当前 direction={self.direction}"
            )

        # 缓存命中检查
        if self.local_path is not None:
            local = Path(self.local_path)
            if await aiofiles.os.path.exists(local):
                return local

        if self.id is None:
            raise ValueError("MediaFile 必须先 save 获取 ID 才能缓存解密文件")

        download_url = self._build_inbound_download_url()
        msg_id_repr: str | int = self.ilink_msg_id or self.id or 'unknown'

        cache_dir = Path(meta_config.MEDIA_CACHE_DIR)
        await aiofiles.os.makedirs(cache_dir, exist_ok=True)

        # 解密产物先落到 .raw 临时文件，VOICE 再做 silk→wav 转换
        raw_path = cache_dir / f"{self.id}_{self.file_name}.raw"
        try:
            await self._download_and_decrypt_stream(bridge, download_url, raw_path)
        except MediaInboundError:
            raise
        except Exception as e:
            raise MediaInboundError(
                ilink_msg_id=msg_id_repr,
                stage='download',
                reason=f"{type(e).__name__}: {e}",
            ) from e

        # 对 VOICE 段做 silk → wav 转码（best-effort：失败时 fallback 用原 silk）
        final_path = await self._maybe_transcode_voice(raw_path, msg_id_repr)

        # 更新元数据
        stat = await aiofiles.os.stat(final_path)
        self.local_path = str(final_path.absolute())
        self.plaintext_size = stat.st_size
        if commit:
            _ = await self.save(session)
        l.info(
            f"入站媒体已就绪: id={self.id}, type={self.item_type.name}, "
            f"size={self.plaintext_size}, path={final_path}"
        )
        return final_path

    def _build_inbound_download_url(self) -> str:
        """
        构造入站下载 URL。

        优先 ``cdn_full_url``；缺失时用 ``cdn_encrypt_query_param`` 拼到默认 CDN base。

        :raises MediaInboundError: 两个字段都缺失
        """
        if self.cdn_full_url is not None:
            return self.cdn_full_url
        if self.cdn_encrypt_query_param is not None:
            from urllib.parse import quote
            cdn_base = meta_config.ILINK_CDN_BASE_URL.rstrip('/')
            return (
                f"{cdn_base}/download?"
                f"encrypted_query_param={quote(self.cdn_encrypt_query_param, safe='')}"
            )
        raise MediaInboundError(
            ilink_msg_id=self.ilink_msg_id or self.id or 'unknown',
            stage='build_url',
            reason="cdn_full_url 和 cdn_encrypt_query_param 都缺失，无法构造下载 URL",
        )

    async def _download_and_decrypt_stream(
        self,
        bridge: 'Bridge',
        download_url: str,
        target_path: Path,
    ) -> None:
        """
        流式下载 CDN 密文 → AES 解密 → 落盘。

        失败时抛原始异常，由调用方包装为 ``MediaInboundError``。
        """
        key = self.aes_key_bytes()
        decryptor = StreamAESDecryptor(key)

        l.debug(f"下载入站媒体: id={self.id}, url={download_url[:80]}...")
        async with bridge._http_request_with_retry('GET', download_url) as resp:
            if resp.status != 200:
                raise MediaInboundError(
                    ilink_msg_id=self.ilink_msg_id or self.id or 'unknown',
                    stage='download',
                    reason=f"HTTP {resp.status}: {download_url[:80]}",
                )
            async with aiofiles.open(target_path, 'wb') as f:
                async for encrypted_chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK_SIZE):
                    plaintext_chunk = decryptor.update(encrypted_chunk)
                    if plaintext_chunk:
                        _ = await f.write(plaintext_chunk)
                tail = decryptor.finalize()
                if tail:
                    _ = await f.write(tail)

    async def _maybe_transcode_voice(
        self,
        raw_path: Path,
        msg_id_repr: str | int,
    ) -> Path:
        """
        对 VOICE 段做 silk → wav 转码（best-effort）。

        失败时 fallback 用原 silk 文件——与官方 ``silkToWav`` 行为一致：
        转码失败不影响主体送达，OneBot 框架可以自行处理 silk 原始字节。

        非 VOICE 段直接返回 ``raw_path``（重命名去掉 ``.raw`` 后缀）。
        """
        if self.item_type != ILinkMessageItemType.VOICE:
            # 非 VOICE：去掉 .raw 后缀，保留原文件名
            final_path = raw_path.with_suffix('')
            await aiofiles.os.rename(raw_path, final_path)
            return final_path

        # VOICE: 尝试 silk → wav
        wav_path = raw_path.parent / f"{raw_path.stem.removesuffix('.raw')}.wav"
        try:
            import pilk
            # pilk.silk_to_wav 默认 24000Hz，与 iLink VOICE 段一致
            # pilk.silk_to_wav 默认 24000Hz，与 iLink VOICE 段（silk-wasm 默认）一致
            pilk.silk_to_wav(str(raw_path), str(wav_path), rate=24000)
            # 转码成功，删除 raw silk
            await aiofiles.os.remove(raw_path)
            self.mime_type = 'audio/wav'
            l.debug(f"silk → wav 转码成功: id={self.id}")
            return wav_path
        except Exception as e:
            # best-effort fallback：保留 silk 原文件
            l.warning(
                f"silk → wav 转码失败（fallback 用原 silk）: msg_id={msg_id_repr}, "
                f"err={type(e).__name__}: {e}"
            )
            # 重命名 raw 文件去掉 .raw 后缀，保留 silk 扩展名
            silk_path = raw_path.with_suffix('')
            if not silk_path.name.endswith('.silk'):
                silk_path = silk_path.with_suffix('.silk')
            await aiofiles.os.rename(raw_path, silk_path)
            self.mime_type = 'audio/silk'
            return silk_path

    # ===== 出站工厂 =====

    @classmethod
    async def create_outbound_from_onebot_segment(
        cls,
        session: AsyncSession,
        segment: OneBotSegment,
        ilink_msg_type: ILinkMessageItemType,
    ) -> 'MediaFile':
        """
        从 OneBot 媒体 segment 创建出站 ``MediaFile`` 记录并 save。

        自动生成本地 AES key（IMAGE 同时生成 hex 和 base64 两种形态）+ filekey。

        :raises MediaOutboundError: segment 缺少 file/url 字段、或 ``ilink_msg_type`` 是 TEXT/NONE
        """
        if ilink_msg_type in (ILinkMessageItemType.TEXT, ILinkMessageItemType.NONE):
            raise MediaOutboundError(
                user_id='unknown',
                segment_type=segment.type,
                stage='create',
                reason=f"ilink_msg_type={ilink_msg_type!r} 不是媒体类型",
            )

        source_url = (
            segment.data.get('file')
            or segment.data.get('url')
            or segment.data.get('base64')
        )
        if source_url is None or not str(source_url).strip():
            raise MediaOutboundError(
                user_id='unknown',
                segment_type=segment.type,
                stage='create',
                reason=f"OneBot {segment.type} 段缺少 file/url/base64 字段: {segment.data}",
            )
        source_url = str(source_url)

        file_name = _resolve_outbound_file_name(segment, source_url, ilink_msg_type)
        mime_type = _guess_mime_type(file_name, ilink_msg_type)

        # 本地生成 AES key + filekey
        key = generate_aes_key()
        cdn_aes_key_b64 = cdn_aes_key_to_base64(key)
        image_aeskey_hex = (
            image_aes_key_to_hex(key) if ilink_msg_type == ILinkMessageItemType.IMAGE else None
        )
        filekey = secrets.token_hex(16)

        instance = cls(
            direction=MediaFileDirectionEnum.outbound,
            item_type=ilink_msg_type,
            onebot_source_url=source_url,
            cdn_aes_key_base64=cdn_aes_key_b64,
            image_aeskey_hex=image_aeskey_hex,
            filekey=filekey,
            file_name=file_name,
            mime_type=mime_type,
            cdn_encrypt_type=1,  # 出站方向固定 1（对齐官方 send.ts）
        )
        return await instance.save(session)

    # ===== 出站：拉取源 + 加密上传 =====

    async def fetch_outbound_source(
        self,
        bridge: 'Bridge',
        session: AsyncSession,
        *,
        commit: bool = True,
    ) -> Path:
        """
        把 ``onebot_source_url`` 指向的数据加载到本地 ``local_path``。

        四种 scheme 分支：
        - ``base64://``：解码内嵌字节直接落盘（受 ``_MAX_BASE64_DECODED_BYTES`` 限制）
        - ``http(s)://``：流式下载到工作目录
        - ``file://`` 与裸路径：直接复用本地文件（不复制）

        ``local_path`` 已存在且文件实际有效时直接复用（断点重试场景）。

        :raises MediaOutboundError: scheme 不支持 / HTTP 下载失败 / base64 解码失败 / 文件不存在
        :raises ValueError: 调用方向错误
        """
        if self.direction != MediaFileDirectionEnum.outbound:
            raise ValueError(
                f"fetch_outbound_source 仅适用于出站方向，当前 direction={self.direction}"
            )
        if self.onebot_source_url is None:
            raise MediaOutboundError(
                user_id='unknown',
                segment_type=self.item_type.name,
                stage='fetch_source',
                reason="出站 MediaFile 缺少 onebot_source_url",
            )
        if self.id is None:
            raise ValueError("MediaFile 必须先 save 获取 ID 才能落盘")

        # 缓存命中
        if self.local_path is not None:
            existing = Path(self.local_path)
            if await aiofiles.os.path.exists(existing):
                return existing

        work_dir = Path(meta_config.MEDIA_CACHE_DIR) / 'outbound'
        await aiofiles.os.makedirs(work_dir, exist_ok=True)
        source = self.onebot_source_url.strip()

        try:
            if source.startswith('base64://'):
                local, size = await self._fetch_from_base64(source[len('base64://'):], work_dir)
            elif source.startswith('data:') and ';base64,' in source:
                local, size = await self._fetch_from_base64(source.split(';base64,', 1)[1], work_dir)
            elif source.startswith('http://') or source.startswith('https://'):
                local, size = await self._fetch_from_http(source, bridge, work_dir)
            elif source.startswith('file://'):
                local, size = await self._fetch_from_local_path(source)
            else:
                path_obj = Path(source)
                if path_obj.exists():
                    local, size = await self._fetch_from_local_path(source)
                else:
                    # 尝试按裸 base64 处理
                    local, size = await self._fetch_from_base64(source, work_dir)
        except MediaOutboundError:
            raise
        except Exception as e:
            raise MediaOutboundError(
                user_id='unknown',
                segment_type=self.item_type.name,
                stage='fetch_source',
                reason=f"{type(e).__name__}: {e}",
            ) from e

        self.local_path = str(local.absolute())
        self.plaintext_size = size
        if commit:
            _ = await self.save(session)
        return local.absolute()

    async def _fetch_from_base64(
        self,
        b64_content: str,
        work_dir: Path,
    ) -> tuple[Path, int]:
        """从 base64 字符串解码到本地文件"""
        if b64_content.startswith('base64://'):
            b64_content = b64_content[len('base64://'):]
        if ';base64,' in b64_content:
            b64_content = b64_content.split(';base64,', 1)[1]
        b64_content = b64_content.strip()

        try:
            data = base64.b64decode(b64_content, validate=False)
        except (ValueError, BinasciiError) as e:
            raise MediaOutboundError(
                user_id='unknown',
                segment_type=self.item_type.name,
                stage='fetch_source',
                reason=f"base64 解码失败: {e}",
            ) from e

        if len(data) > _MAX_BASE64_DECODED_BYTES:
            raise MediaOutboundError(
                user_id='unknown',
                segment_type=self.item_type.name,
                stage='fetch_source',
                reason=(
                    f"base64 解码后超过 {_MAX_BASE64_DECODED_BYTES} 字节上限"
                    f"（实际 {len(data)}）"
                ),
            )

        local = (work_dir / f"{self.id}_{self.file_name}").absolute()
        await aiofiles.os.makedirs(work_dir, exist_ok=True)
        async with aiofiles.open(local, 'wb') as f:
            _ = await f.write(data)
        return local, len(data)

    async def _fetch_from_http(
        self,
        source: str,
        bridge: 'Bridge',
        work_dir: Path,
    ) -> tuple[Path, int]:
        """从 ``http(s)://`` 流式下载到本地"""
        local = (work_dir / f"{self.id}_{self.file_name}").absolute()
        total = 0
        async with bridge._http_request_with_retry('GET', source) as resp:
            if resp.status != 200:
                raise MediaOutboundError(
                    user_id='unknown',
                    segment_type=self.item_type.name,
                    stage='fetch_source',
                    reason=f"HTTP {resp.status}: {source[:80]}",
                )
            async with aiofiles.open(local, 'wb') as f:
                async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK_SIZE):
                    _ = await f.write(chunk)
                    total += len(chunk)
        l.debug(f"出站源已下载: {source[:60]}... → {local} ({total} bytes)")
        return local, total

    async def _fetch_from_local_path(self, source: str) -> tuple[Path, int]:
        """处理 ``file://`` 和裸路径（直接复用，不复制）"""
        if source.startswith('file://'):
            parsed = urlparse(source)
            path_str = unquote(parsed.path)
            # Windows: file:///C:/path → /C:/path，去掉首斜杠
            if path_str.startswith('/') and len(path_str) > 3 and path_str[2] == ':':
                path_str = path_str[1:]
        else:
            path_str = source
        path = Path(path_str)
        if not await aiofiles.os.path.exists(path):
            raise MediaOutboundError(
                user_id='unknown',
                segment_type=self.item_type.name,
                stage='fetch_source',
                reason=f"本地媒体文件不存在: {path}",
            )
        stat = await aiofiles.os.stat(path)
        return path.absolute(), stat.st_size

    async def prepare_and_upload(
        self,
        bridge: 'Bridge',
        session: AsyncSession,
        to_user_id: str,
        *,
        commit: bool = True,
    ) -> None:
        """
        把已落盘的 ``local_path`` 明文流式加密并上传到 iLink CDN。

        步骤：
        1. 读 ``local_path`` 计算 MD5 + 大小 ``plaintext_size``、``ciphertext_size``
        2. 调 ``bridge.get_upload_url(...)`` 获取 ``upload_full_url``
        3. 流式 AES 加密 + PUT 上传，从 CDN 响应 header ``x-encrypted-param``
           取得 download param，写入 ``cdn_encrypt_query_param``
        4. 持久化所有字段

        ``cdn_encrypt_query_param`` 已填时短路返回（幂等）。

        :raises MediaOutboundError: 任何上传 / 加密阶段失败
        """
        if self.direction != MediaFileDirectionEnum.outbound:
            raise ValueError(
                f"prepare_and_upload 仅适用于出站方向，当前 direction={self.direction}"
            )

        # 已上传，幂等
        if self.cdn_encrypt_query_param is not None:
            return

        if self.local_path is None:
            raise MediaOutboundError(
                user_id=to_user_id,
                segment_type=self.item_type.name,
                stage='prepare',
                reason="缺少 local_path，请先调用 fetch_outbound_source",
            )
        if self.filekey is None:
            raise MediaOutboundError(
                user_id=to_user_id,
                segment_type=self.item_type.name,
                stage='prepare',
                reason="缺少 filekey（应在 create_outbound_from_onebot_segment 生成）",
            )

        local = Path(self.local_path)
        # Step 1: 计算 MD5 + 大小
        plaintext_size, md5_hex = await self._compute_md5_and_size(local)
        self.plaintext_size = plaintext_size
        self.plaintext_md5_hex = md5_hex
        self.ciphertext_size = aes_ecb_padded_size(plaintext_size)

        # Step 2: getuploadurl
        upload_media_type = _onebot_to_upload_media_type(self.item_type)
        if self.image_aeskey_hex is not None:
            aeskey_hex_for_upload = self.image_aeskey_hex
        else:
            # 非 IMAGE 也用本地 AES key 的 hex 表达（与官方 cdn-upload.ts 一致）
            aeskey_hex_for_upload = self.aes_key_bytes().hex()

        try:
            upload_response = await bridge.get_upload_url(
                filekey=self.filekey,
                media_type=upload_media_type,
                to_user_id=to_user_id,
                rawsize=plaintext_size,
                rawfilemd5=md5_hex,
                filesize=self.ciphertext_size,
                no_need_thumb=True,
                aeskey=aeskey_hex_for_upload,
            )
        except Exception as e:
            raise MediaOutboundError(
                user_id=to_user_id,
                segment_type=self.item_type.name,
                stage='get_upload_url',
                reason=f"{type(e).__name__}: {e}",
            ) from e

        upload_full_url = (
            upload_response.upload_full_url.strip()
            if upload_response.upload_full_url is not None
            else None
        )
        upload_param = (
            upload_response.upload_param.strip()
            if upload_response.upload_param is not None
            else None
        )

        if upload_full_url:
            target_upload_url = upload_full_url
        elif upload_param:
            from urllib.parse import quote
            cdn_base = meta_config.ILINK_CDN_BASE_URL.rstrip('/')
            # 对齐 Tencent/openclaw-weixin: buildCdnUploadUrl
            target_upload_url = (
                f"{cdn_base}/upload?"
                f"encrypted_query_param={quote(upload_param, safe='')}"
                f"&filekey={quote(self.filekey, safe='')}"
            )
        else:
            raise MediaOutboundError(
                user_id=to_user_id,
                segment_type=self.item_type.name,
                stage='get_upload_url',
                reason=f"iLink 未返回 upload_full_url 或 upload_param: {upload_response}",
            )

        # Step 3: 加密 + POST
        try:
            download_param = await self._stream_encrypt_and_post(
                bridge=bridge,
                upload_url=target_upload_url,
                local_path=local,
            )
        except MediaOutboundError:
            raise
        except Exception as e:
            raise MediaOutboundError(
                user_id=to_user_id,
                segment_type=self.item_type.name,
                stage='upload',
                reason=f"{type(e).__name__}: {e}",
            ) from e

        self.cdn_encrypt_query_param = download_param
        if commit:
            _ = await self.save(session)
        l.info(
            f"出站媒体已上传: id={self.id}, type={self.item_type.name}, "
            f"size={plaintext_size}, download_param={download_param[:32]}..."
        )

    @staticmethod
    async def _compute_md5_and_size(local: Path) -> tuple[int, str]:
        """读本地文件计算原文件 MD5（hex）+ 大小（字节）"""
        md5 = hashlib.md5(usedforsecurity=False)
        total = 0
        async with aiofiles.open(local, 'rb') as f:
            while True:
                chunk = await f.read(_DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                md5.update(chunk)
                total += len(chunk)
        return total, md5.hexdigest()

    async def _stream_encrypt_and_post(
        self,
        bridge: 'Bridge',
        upload_url: str,
        local_path: Path,
    ) -> str:
        """
        本地文件 → AES 加密 → POST 上传到 iLink CDN。

        协议：上传请求方法是 POST（对齐官方 cdn-upload.ts 与 CDN Media 协议规范），
        必须携带明确的 Content-Length，响应 200 + header ``x-encrypted-param`` 包含后续
        ``sendmessage`` 用的 download param。

        :return: CDN 响应 header ``x-encrypted-param`` 的值
        :raises MediaOutboundError: 缺 header / 非 2xx 响应
        """
        key = self.aes_key_bytes()
        encryptor = StreamAESEncryptor(key)

        async with aiofiles.open(local_path, 'rb') as f:
            plaintext = await f.read()

        ciphertext = encryptor.update(plaintext) + encryptor.finalize()

        headers = {
            'Content-Type': 'application/octet-stream',
            'Content-Length': str(len(ciphertext)),
        }
        async with bridge.http_session.post(
            upload_url,
            data=ciphertext,
            headers=headers,
        ) as resp:
            if not (200 <= resp.status < 300):
                body = await resp.text()
                raise MediaOutboundError(
                    user_id='unknown',
                    segment_type=self.item_type.name,
                    stage='upload',
                    reason=f"CDN POST 返回 {resp.status}: {body[:200]}",
                )
            download_param = resp.headers.get('x-encrypted-param')
            if not download_param:
                raise MediaOutboundError(
                    user_id='unknown',
                    segment_type=self.item_type.name,
                    stage='upload',
                    reason="CDN 响应缺少 x-encrypted-param header",
                )
            return download_param

    # ===== 通用：清理本地缓存 =====

    async def cleanup_local(
        self,
        session: AsyncSession,
        *,
        commit: bool = True,
    ) -> None:
        """
        删除本地缓存文件并清除 ``local_path``（DB 记录保留）。

        定期清理超时缓存时使用。CDN URL 和密钥保留供将来重新下载/重传。
        """
        if self.local_path is not None:
            local = Path(self.local_path)
            if await aiofiles.os.path.exists(local):
                await aiofiles.os.remove(local)
                l.debug(f"已删除媒体缓存: {local}")
        self.local_path = None
        if commit:
            _ = await self.save(session)


# ===== 模块级辅助函数（不属于充血方法） =====

def _onebot_to_upload_media_type(item_type: ILinkMessageItemType) -> int:
    """
    ``ILinkMessageItemType`` → ``ILinkUploadMediaType`` 的整数值映射。

    协议怪癖：这两个枚举的取值不同（``MessageItemType`` IMAGE=2 / VIDEO=5 / FILE=4 / VOICE=3，
    而 ``UploadMediaType`` IMAGE=1 / VIDEO=2 / FILE=3 / VOICE=4）。

    :raises MediaOutboundError: item_type 是 TEXT / NONE
    """
    mapping: dict[ILinkMessageItemType, ILinkUploadMediaType] = {
        ILinkMessageItemType.IMAGE: ILinkUploadMediaType.IMAGE,
        ILinkMessageItemType.VIDEO: ILinkUploadMediaType.VIDEO,
        ILinkMessageItemType.FILE: ILinkUploadMediaType.FILE,
        ILinkMessageItemType.VOICE: ILinkUploadMediaType.VOICE,
    }
    if item_type not in mapping:
        raise MediaOutboundError(
            user_id='unknown',
            segment_type=item_type.name,
            stage='prepare',
            reason=f"item_type={item_type!r} 不是上传支持的媒体类型",
        )
    return int(mapping[item_type])


def _extract_image_item_fields(
    item: MessageItem,
    msg_id_repr: str | int,
) -> tuple[str | None, str | None, str | None, int | None, str]:
    """
    从 IMAGE ``MessageItem`` 提取 ``cdn_full_url`` / ``cdn_encrypt_query_param``
    / ``cdn_aes_key_base64`` / ``cdn_encrypt_type`` / ``file_name``。
    """
    if item.image_item is None:
        raise MediaInboundError(
            ilink_msg_id=msg_id_repr,
            stage='extract',
            reason="IMAGE item 缺少 image_item 子对象",
        )
    media = item.image_item.media
    return (
        media.full_url if media else None,
        media.encrypt_query_param if media else None,
        media.aes_key if media else None,
        media.encrypt_type if media else None,
        f"{item.msg_id or 'image'}.jpg",
    )


def _extract_voice_item_fields(
    item: MessageItem,
    msg_id_repr: str | int,
) -> tuple[str | None, str | None, str | None, int | None, str]:
    """从 VOICE ``MessageItem`` 提取 CDN 字段"""
    if item.voice_item is None:
        raise MediaInboundError(
            ilink_msg_id=msg_id_repr,
            stage='extract',
            reason="VOICE item 缺少 voice_item 子对象",
        )
    media = item.voice_item.media
    return (
        media.full_url if media else None,
        media.encrypt_query_param if media else None,
        media.aes_key if media else None,
        media.encrypt_type if media else None,
        f"{item.msg_id or 'voice'}.silk",
    )


def _extract_file_item_fields(
    item: MessageItem,
    msg_id_repr: str | int,
) -> tuple[str | None, str | None, str | None, int | None, str]:
    """从 FILE ``MessageItem`` 提取 CDN 字段"""
    if item.file_item is None:
        raise MediaInboundError(
            ilink_msg_id=msg_id_repr,
            stage='extract',
            reason="FILE item 缺少 file_item 子对象",
        )
    media = item.file_item.media
    file_name = item.file_item.file_name or f"{item.msg_id or 'file'}.bin"
    return (
        media.full_url if media else None,
        media.encrypt_query_param if media else None,
        media.aes_key if media else None,
        media.encrypt_type if media else None,
        file_name,
    )


def _extract_video_item_fields(
    item: MessageItem,
    msg_id_repr: str | int,
) -> tuple[str | None, str | None, str | None, int | None, str]:
    """从 VIDEO ``MessageItem`` 提取 CDN 字段"""
    if item.video_item is None:
        raise MediaInboundError(
            ilink_msg_id=msg_id_repr,
            stage='extract',
            reason="VIDEO item 缺少 video_item 子对象",
        )
    media = item.video_item.media
    return (
        media.full_url if media else None,
        media.encrypt_query_param if media else None,
        media.aes_key if media else None,
        media.encrypt_type if media else None,
        f"{item.msg_id or 'video'}.mp4",
    )


def _guess_mime_type(file_name: str, item_type: ILinkMessageItemType) -> str | None:
    """优先从文件名推断 MIME，失败时按段类型兜底"""
    import mimetypes
    mime, _ = mimetypes.guess_type(file_name)
    if mime is not None:
        return mime
    fallback: dict[ILinkMessageItemType, str] = {
        ILinkMessageItemType.IMAGE: 'image/jpeg',
        ILinkMessageItemType.VOICE: 'audio/silk',
        ILinkMessageItemType.VIDEO: 'video/mp4',
        ILinkMessageItemType.FILE: 'application/octet-stream',
    }
    return fallback.get(item_type)


def _resolve_outbound_file_name(
    segment: OneBotSegment,
    source_url: str,
    item_type: ILinkMessageItemType,
) -> str:
    """
    决定出站媒体的文件名。

    优先级：OneBot ``data['name']`` → URL 末段 → 兜底 ``onebot_outbound_<hex>.<ext>``。
    """
    explicit = segment.data.get('name')
    if explicit and explicit.strip():
        return explicit.strip()

    if source_url.startswith('http://') or source_url.startswith('https://'):
        last = urlparse(source_url).path.rsplit('/', 1)[-1]
        last = unquote(last).strip()
        if last and '.' in last:
            return last
    elif source_url.startswith('file://'):
        parsed = urlparse(source_url)
        last = Path(unquote(parsed.path)).name
        if last:
            return last
    elif not source_url.startswith('base64://'):
        last = Path(source_url).name
        if last:
            return last

    # 兜底
    default_ext: dict[ILinkMessageItemType, str] = {
        ILinkMessageItemType.IMAGE: 'jpg',
        ILinkMessageItemType.VOICE: 'silk',
        ILinkMessageItemType.VIDEO: 'mp4',
        ILinkMessageItemType.FILE: 'bin',
    }
    ext = default_ext.get(item_type, 'bin')
    return f"onebot_outbound_{secrets.token_hex(6)}.{ext}"
