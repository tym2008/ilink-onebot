"""
iLink ↔ OneBot v11 消息转换（领域逻辑）

承担两个协议格式互译的纯函数，被 ``Bridge`` 委托。

设计原则：

- **无静默失败**：转换层遇到无法处理的情况一律 raise（``MediaInboundError``、
  ``MediaOutboundError``、``OneBotMappingMissingError``），让 polling / action_handler
  层决定降级 / 重试 / 上抛策略
- **信息密度优先**：每个异常携带完整上下文（ilink_msg_id / user_id / segment_type / 失败阶段）
- **单段消息原则**：iLink 单条消息只支持单一媒体类型，多媒体段输入须拆分多次 sendmessage

入站路径：``WeixinMessage`` → ``OneBotPrivateMessageEvent``
出站路径：``list[OneBotSegment]`` → 1 次或多次 ``Bridge.send_text/send_media``
"""
import time
from typing import TYPE_CHECKING

from loguru import logger as l
from sqlmodel.ext.asyncio.session import AsyncSession

from meta_config import meta_config
from onebots.models import (
    OneBotPrivateMessageEvent,
    OneBotPrivateSubTypeEnum,
    OneBotSegment,
    OneBotSender,
)
from sqlmodels.bridge.enums import ILinkMessageItemType
from sqlmodels.bridge.exceptions import OneBotMappingMissingError
from sqlmodels.bridge.id_registry import OneBotMessageIdRegistry
from sqlmodels.bridge.ilink_models import (
    MessageItem,
    RefMessage,
    WeixinMessage,
)
from sqlmodels.media_file import MediaFile

if TYPE_CHECKING:
    from sqlmodels.bridge.bridge import Bridge


# ===== iLink item type ↔ OneBot segment type 双向映射 =====

# OneBot v11 段 ``image/record/file/video`` 对应 iLink ``MessageItem.type``。
# 注意 OneBot 用 ``record`` 表示语音段（非 ``voice``，是 v11 历史命名）。
_ILINK_TYPE_TO_ONEBOT_SEGMENT_TYPE: dict[ILinkMessageItemType, str] = {
    ILinkMessageItemType.IMAGE: 'image',
    ILinkMessageItemType.VOICE: 'record',
    ILinkMessageItemType.FILE: 'file',
    ILinkMessageItemType.VIDEO: 'video',
}

_ONEBOT_SEGMENT_TYPE_TO_ILINK_TYPE: dict[str, ILinkMessageItemType] = {
    'image': ILinkMessageItemType.IMAGE,
    'record': ILinkMessageItemType.VOICE,
    'voice': ILinkMessageItemType.VOICE,  # 兼容部分框架的别名
    'file': ILinkMessageItemType.FILE,
    'video': ILinkMessageItemType.VIDEO,
}


# ===== 入站：WeixinMessage → OneBot v11 =====

async def convert_inbound_message(
    bridge: 'Bridge',
    session: AsyncSession,
    msg: WeixinMessage,
    id_registry: OneBotMessageIdRegistry,
    self_id: int | None = None,
) -> OneBotPrivateMessageEvent:
    """
    把单条 ``WeixinMessage`` 转换为 OneBot v11 私聊事件。

    调用前提（``Bridge.handle_inbound_message`` 已保证）：

    - ``msg.from_user_id`` 非空
    - ``msg.message_state`` 不是 ``GENERATING``（已被上层过滤）

    副作用：
    - 通过 ``id_registry`` 注册 iLink message_id ↔ OneBot message_id 映射
    - 通过 ``id_registry`` 缓存 from_user_id → OneBot user_id 哈希

    :raises MediaInboundError: 媒体段下载 / 解密失败（由 ``_convert_media_segment_inbound`` 抛出）
    :raises ValueError: ``msg.from_user_id`` 为空（违反调用前提）
    """
    if msg.from_user_id is None:
        raise ValueError(
            f"convert_inbound_message called with msg.from_user_id=None "
            f"(message_id={msg.message_id})"
        )

    segments = await _convert_segments_inbound(bridge, session, msg, id_registry)

    # 分配 OneBot message_id 并注册映射（仅当 iLink 给了 message_id 时）
    onebot_message_id = await id_registry.next_message_id(session)
    if msg.message_id is not None:
        await id_registry.register_message_id(
            session, str(msg.message_id), onebot_message_id,
        )

    onebot_user_id = id_registry.resolve_user_id(msg.from_user_id)
    create_time_seconds = (msg.create_time_ms or int(time.time() * 1000)) // 1000
    target_self_id = self_id if self_id is not None else bridge.self_id

    return OneBotPrivateMessageEvent(
        time=create_time_seconds,
        self_id=target_self_id,
        sub_type=OneBotPrivateSubTypeEnum.friend,
        message_id=onebot_message_id,
        user_id=onebot_user_id,
        message=segments,
        raw_message=_build_raw_message(segments),
        sender=OneBotSender(
            user_id=onebot_user_id,
            nickname=msg.from_user_id,  # iLink 不返回 nickname，用 user_id 占位
        ),
    )


async def _convert_segments_inbound(
    bridge: 'Bridge',
    session: AsyncSession,
    msg: WeixinMessage,
    id_registry: OneBotMessageIdRegistry,
) -> list[OneBotSegment]:
    """
    把 ``WeixinMessage.item_list`` 转换为 OneBot v11 段数组。

    每个 ``MessageItem`` 按 ``type`` 分发；含 ``ref_msg`` 的 item 额外发出一个
    ``reply`` 段（若 ID 映射查不到则丢弃 reply 段——这是合理降级，因为 reply
    只是辅助信息，主体段仍能送达；其他类型失败一律 raise 不降级）。

    :raises MediaInboundError: 媒体段失败（来自 ``_convert_media_segment_inbound``）
    :raises ValueError: 出现不可识别的 ``MessageItem.type`` 值
    """
    segments: list[OneBotSegment] = []
    item_list = msg.item_list or []

    for item in item_list:
        # 引用段（最多 1 个，放在主体段之前以符合 OneBot 框架习惯）
        if item.ref_msg is not None and item.ref_msg.message_item is not None:
            try:
                reply_seg = await _resolve_reply_segment(
                    session, item.ref_msg.message_item, id_registry,
                )
                segments.append(reply_seg)
            except OneBotMappingMissingError as e:
                # 协议预期内的部分降级（消息保留期外 / 重启前未注册），日志记录便于排查。
                # 主体段不受影响，转换继续。
                l.info(f"reply 段映射查不到，跳过该段: {e}")

        # 主体段
        if item.type == ILinkMessageItemType.TEXT:
            segments.append(_build_text_segment(item))
        elif item.type in _ILINK_TYPE_TO_ONEBOT_SEGMENT_TYPE:
            media_segment = await _convert_media_segment_inbound(
                bridge, session, item, ilink_msg_id=msg.message_id,
            )
            segments.append(media_segment)
        elif item.type == ILinkMessageItemType.NONE:
            # 空 item（如纯 ref_msg 携带 wrapper 没有自身内容），跳过主体段——
            # 仅当 ref_msg 已添加为 reply 段时这是合理的，否则上层应警告
            if item.ref_msg is None:
                raise ValueError(
                    f"MessageItem with type=NONE and no ref_msg (message_id={msg.message_id})"
                )
        else:
            # 类型枚举完备，理论不可达；防御性 raise 而非静默
            raise ValueError(
                f"Unknown MessageItem.type={item.type!r} in message_id={msg.message_id}"
            )

    return segments


def _build_text_segment(item: MessageItem) -> OneBotSegment:
    """
    TEXT item → OneBot text 段。

    协议规定 TEXT item 必有 ``text_item.text``，运行时若缺失视为协议异常 raise。
    """
    if item.text_item is None or item.text_item.text is None:
        raise ValueError(
            f"TEXT MessageItem missing text_item.text (msg_id={item.msg_id})"
        )
    return OneBotSegment(type='text', data={'text': item.text_item.text})


async def _resolve_reply_segment(
    session: AsyncSession,
    ref_message_item: MessageItem,
    id_registry: OneBotMessageIdRegistry,
) -> OneBotSegment:
    """
    iLink ``ref_msg.message_item`` → OneBot reply 段。

    严格 raise 模式：映射查不到时抛 ``OneBotMappingMissingError`` 让调用方明确知道，
    由调用方决定是否做"协议预期内的降级"。本函数不吞错。

    :raises ValueError: ref_msg.message_item 协议违规（缺 msg_id）
    :raises OneBotMappingMissingError: iLink msg_id 在本地映射表中不存在
    """
    if ref_message_item.msg_id is None:
        raise ValueError("ref_msg.message_item missing msg_id (iLink protocol violation)")

    onebot_message_id = await id_registry.resolve_onebot_message_id(
        session, ref_message_item.msg_id,
    )
    if onebot_message_id is None:
        raise OneBotMappingMissingError(
            ilink_msg_id=ref_message_item.msg_id,
            direction='ilink_to_onebot',
        )
    return OneBotSegment(type='reply', data={'id': str(onebot_message_id)})


async def _convert_media_segment_inbound(
    bridge: 'Bridge',
    session: AsyncSession,
    item: MessageItem,
    *,
    ilink_msg_id: int | None,
) -> OneBotSegment:
    """
    媒体 item → OneBot 媒体段（``image`` / ``record`` / ``file`` / ``video``）。

    委托 ``MediaFile.create_inbound_from_message_item()`` + 触发懒下载解密，
    然后构造 OneBot 段。

    任何阶段失败都 raise ``MediaInboundError`` 携带 ilink_msg_id + 失败阶段。
    转换层不吞错——上层根据异常决定整条消息的处理（如 polling 层 catch + log 丢弃单条消息）。

    :raises MediaInboundError: 由 ``MediaFile.create_inbound_from_message_item`` /
        ``MediaFile.ensure_decrypted`` 抛出
    :raises ValueError: item type 不是已知媒体类型（违反调用前提）
    """
    if item.type not in _ILINK_TYPE_TO_ONEBOT_SEGMENT_TYPE:
        raise ValueError(
            f"_convert_media_segment_inbound called with non-media type={item.type!r}"
        )

    media_file = await MediaFile.create_inbound_from_message_item(
        session=session,
        item=item,
        ilink_msg_id=ilink_msg_id,
    )
    # 触发懒下载 + 解密（失败时抛 MediaInboundError，由调用方上抛）
    await media_file.ensure_decrypted(bridge, session)

    onebot_segment_type = _ILINK_TYPE_TO_ONEBOT_SEGMENT_TYPE[item.type]
    serve_url = f"{_media_public_base_url()}{media_file.url_suffix}"

    seg_data: dict[str, str] = {'file': serve_url, 'url': serve_url}
    if media_file.file_name is not None:
        seg_data['name'] = media_file.file_name
    return OneBotSegment(type=onebot_segment_type, data=seg_data)


def _build_raw_message(segments: list[OneBotSegment]) -> str:
    """
    构造 ``raw_message`` 字段（OneBot 框架做关键词匹配用）。

    简单方案：拼接所有 text 段；媒体段填中文占位符（与社区约定一致）；
    reply 段不进入 raw_message（框架通常通过 reply 段单独取上下文）。
    """
    parts: list[str] = []
    for seg in segments:
        if seg.type == 'text':
            parts.append(seg.data.get('text', ''))
        elif seg.type == 'image':
            parts.append('[图片]')
        elif seg.type == 'record':
            parts.append('[语音]')
        elif seg.type == 'file':
            parts.append('[文件]')
        elif seg.type == 'video':
            parts.append('[视频]')
        # reply / 其他段不进 raw_message
    return ''.join(parts)


def _media_public_base_url() -> str:
    """
    返回媒体服务对外可访问的基础 URL。

    优先 ``meta_config.MEDIA_SERVE_PUBLIC_BASE_URL``（NAT / 反代场景配置）；
    未配置时由 host/port 生成（``0.0.0.0`` → ``127.0.0.1``）。
    """
    if meta_config.MEDIA_SERVE_PUBLIC_BASE_URL is not None:
        return meta_config.MEDIA_SERVE_PUBLIC_BASE_URL.rstrip('/')
    host = '127.0.0.1' if meta_config.MEDIA_SERVE_HOST == '0.0.0.0' else meta_config.MEDIA_SERVE_HOST
    return f"http://{host}:{meta_config.MEDIA_SERVE_PORT}"


# ===== 出站：OneBot v11 → WeixinMessage =====

async def dispatch_outbound_segments(
    bridge: 'Bridge',
    session: AsyncSession,
    target_user_id: str,
    segments: list[OneBotSegment],
) -> str | None:
    """
    把 OneBot 段数组发给指定 iLink user_id。

    iLink 协议约束：单条 ``sendmessage`` 只能携带一种媒体类型的 ``item_list``，
    多媒体段输入必须拆分多次发送。本函数按以下规则拆分：

    - **合并 text 段**：所有 ``text`` 段的文字拼成一个字符串作为 caption
    - **拆分媒体段**：每个媒体段触发一次 ``send_media``；第一个媒体段携带合并后的
      text 作为附带文本（如果协议支持的话——iLink 当前媒体段 caption 实现有限）
    - **纯文本场景**：无媒体段时，单次 ``send_text``
    - **reply 段**：支持出站。根据 OneBot message_id 反查映射构造 ``MessageItem.ref_msg`` 尝试发送；
      若未查到映射或 iLink 发送失败，则自动回退丢弃 reply 段直接发送主体消息

    :param target_user_id: 已通过 OneBot user_id 反查得到的 iLink user_id
    :return: 最后一次发送的 iLink message_id（当前 iLink sendmessage 响应不返回，
        始终为 None；OneBot 框架通常只需要"成功标志"而非真实 ID）

    :raises MediaOutboundError: 任何媒体段发送失败
    :raises ILinkContextTokenMissingError: 目标用户无 context_token
    :raises NotImplementedError: 未支持段类型（face / at / share 等扩展段不支持）
    """
    # 分类段
    text_parts: list[str] = []
    media_segments: list[OneBotSegment] = []
    reply_segments: list[OneBotSegment] = []
    unknown_segments: list[OneBotSegment] = []

    for seg in segments:
        if seg.type == 'text':
            text_parts.append(seg.data.get('text', ''))
        elif seg.type in _ONEBOT_SEGMENT_TYPE_TO_ILINK_TYPE:
            media_segments.append(seg)
        elif seg.type == 'reply':
            reply_segments.append(seg)
        else:
            unknown_segments.append(seg)

    # 未知段类型：明确抛错而非吞掉（信息密度优先）
    if unknown_segments:
        types = [s.type for s in unknown_segments]
        raise NotImplementedError(
            f"出站消息含未支持段类型: {types}（仅支持 text / image / record / video / file / reply）"
        )

    # 解析出站 reply 段（若存在）
    ref_msg: RefMessage | None = None
    if reply_segments:
        reply_seg = reply_segments[0]
        raw_reply_id = reply_seg.data.get('id')
        if raw_reply_id is not None:
            ilink_msg_id: str | None = None
            try:
                onebot_reply_id = int(raw_reply_id)
                registry = bridge.get_id_registry()
                ilink_msg_id = await registry.resolve_ilink_message_id(session, onebot_reply_id)
            except (ValueError, TypeError):
                ilink_msg_id = str(raw_reply_id)

            if ilink_msg_id is not None:
                ref_msg = RefMessage(
                    message_item=MessageItem(
                        type=ILinkMessageItemType.NONE,
                        msg_id=str(ilink_msg_id),
                    ),
                )
                l.info(
                    f"出站 reply 段成功关联 iLink 消息: onebot_id={raw_reply_id} -> ilink_msg_id={ilink_msg_id}"
                )
            else:
                l.info(
                    f"出站 reply 段未在本地映射中找到对应 iLink 消息 (id={raw_reply_id})，丢弃 reply 段直接发送"
                )

    combined_text = ''.join(text_parts).strip()

    # 纯文本场景
    if not media_segments:
        if not combined_text:
            raise ValueError("出站消息既无文本段又无媒体段，无内容可发送")

        if ref_msg is not None:
            try:
                await bridge.send_text(
                    session=session,
                    user_id=target_user_id,
                    content=combined_text,
                    ref_msg=ref_msg,
                )
                return None
            except Exception as e:
                l.warning(
                    f"iLink 发送带 reply 文本消息失败 ({e})，丢弃 reply 段回退直接发送"
                )

        await bridge.send_text(
            session=session,
            user_id=target_user_id,
            content=combined_text,
            ref_msg=None,
        )
        return None

    # 有媒体段：iLink 单消息单媒体，逐个发送
    # 第一个媒体段尝试附带 text 作为 caption 和可选的 ref_msg；其余媒体段只发媒体不带 text
    for index, seg in enumerate(media_segments):
        caption = combined_text if index == 0 else None
        seg_ref_msg = ref_msg if index == 0 else None
        await _send_outbound_media_segment(
            bridge=bridge,
            session=session,
            target_user_id=target_user_id,
            segment=seg,
            caption=caption,
            ref_msg=seg_ref_msg,
        )

    return None


async def _send_outbound_media_segment(
    bridge: 'Bridge',
    session: AsyncSession,
    target_user_id: str,
    segment: OneBotSegment,
    caption: str | None,
    ref_msg: RefMessage | None = None,
) -> None:
    """
    单个 OneBot 媒体段端到端发送：

    1. 解析段元数据（msg_type / source_url / file_name）
    2. ``MediaFile.create_outbound_from_onebot_segment`` 创建出站记录
    3. ``MediaFile.fetch_outbound_source(bridge, session)`` 拉取明文到本地
    4. ``MediaFile.prepare_and_upload(bridge, session, to_user_id)`` 加密 + 上传
    5. ``Bridge.send_media(...)`` 构造 ``MessageItem`` + 发 sendmessage

    任何阶段失败都 raise ``MediaOutboundError`` 携带阶段标识。
    若带 ref_msg 发送失败，自动丢弃 ref_msg 直接发送。
    """
    from sqlmodels.media_file import MediaFile

    ilink_type = _ONEBOT_SEGMENT_TYPE_TO_ILINK_TYPE[segment.type]

    media_file = await MediaFile.create_outbound_from_onebot_segment(
        session=session,
        segment=segment,
        ilink_msg_type=ilink_type,
    )
    await media_file.fetch_outbound_source(bridge, session)
    await media_file.prepare_and_upload(bridge, session, to_user_id=target_user_id)

    if ref_msg is not None:
        try:
            await bridge.send_media(
                session=session,
                user_id=target_user_id,
                media_file=media_file,
                caption=caption,
                ref_msg=ref_msg,
            )
            return
        except Exception as e:
            l.warning(
                f"iLink 发送带 reply 媒体消息失败 ({e})，丢弃 reply 段回退直接发送"
            )

    await bridge.send_media(
        session=session,
        user_id=target_user_id,
        media_file=media_file,
        caption=caption,
        ref_msg=None,
    )
