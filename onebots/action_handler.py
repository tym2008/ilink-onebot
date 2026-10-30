"""
OneBot v11 动作分发器（薄分发层）

把 OneBot 框架发来的 action 请求路由到对应的 handler 函数。
每个 handler 委托给 Bridge 充血方法，本模块不写业务逻辑。

支持的动作（与 iLink 协议能力匹配及机器人框架初始化兼容）：
- `send_private_msg` / `send_msg`：调用 Bridge.send_text/send_media（具体由 converters 决定）
- `get_login_info`：返回 ServerConfig.bot_display_name + Bridge.onebot_self_id
- `get_status` / `get_version_info` / `can_send_image` / `can_send_record`：元信息查询
- `get_friend_list` / `get_stranger_info`：从 ContextToken 返回已知微信联系人
- `get_group_list` / `get_group_member_list`：返回空列表（个人号无群聊）
- `_set_model_show` / `get_guild_service_profile` / `get_online_clients` / `get_cookies` / `get_csrf_token`：框架兼容占位接口

不支持的动作（iLink 无对应能力）：
- `send_group_msg` / `set_group_*`：iLink 不支持群聊
- `delete_msg`：iLink 无撤回 API

不支持的动作统一返回 `retcode=1404`（NOT_FOUND）。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from loguru import logger as l
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from meta_config import meta_config
from onebots.constants import (
    ONEBOT_APP_NAME,
    ONEBOT_APP_VERSION,
    ONEBOT_PROTOCOL_VERSION,
)
from onebots.models import (
    OneBotAction,
    OneBotActionResponse,
    OneBotRetCodeEnum,
    OneBotSegment,
)
from sqlmodels.bridge.context_token import ContextToken
from sqlmodels.bridge.enums import BridgeStateEnum
from sqlmodels.server_config import ServerConfig

if TYPE_CHECKING:
    from sqlmodels.bridge.bridge import Bridge


# 单个动作的处理函数签名：接收 (bridge, session, config, action) 返回 OneBotActionResponse
# 之所以传入 bridge/session/config 而非内部查询：
# - 调用方在外层已经获取了这些对象，避免重复查询
# - 便于测试时注入 mock
ActionHandler = Callable[
    ['Bridge', AsyncSession, ServerConfig, OneBotAction],
    Awaitable[OneBotActionResponse],
]


from onebots.bootstrap_queue import pop_pending_bootstrap_self_id, push_pending_bootstrap_self_id


# ===== 单个动作处理函数 =====
#
# 命名规则：handle_<action_name>，与 OneBot v11 action 名称对应


async def handle_get_login_info(
    bridge: Bridge,
    session: AsyncSession,  # noqa: ARG001 - 接口统一，本函数不需要 session
    config: ServerConfig,
    action: OneBotAction,  # noqa: ARG001 - 无参数动作
) -> OneBotActionResponse:
    """
    返回 bot 的登录信息：`{"user_id": <self_id>, "nickname": <display_name>}`。

    nickname 优先使用 ServerConfig.bot_display_name（管理员可配置），
    回退到 Bridge.wechat_nickname（登录后由 iLink 返回的真实昵称）。
    user_id 使用当前 Bridge 的 self_id（支持多账号各自上报）。
    """
    nickname = config.bot_display_name
    if not nickname and bridge.wechat_nickname is not None:
        nickname = bridge.wechat_nickname
    return OneBotActionResponse.ok(
        data={
            'user_id': bridge.self_id,
            'nickname': nickname,
        },
        echo=action.echo,
    )


async def handle_get_status(
    bridge: Bridge,
    session: AsyncSession,  # noqa: ARG001
    config: ServerConfig,  # noqa: ARG001
    action: OneBotAction,
) -> OneBotActionResponse:
    """
    返回 bot 的运行状态：online + good 标志。

    online = Bridge 处于 running 状态（已扫码登录，正在长轮询）。
    good = 与 online 当前同义（未来可以根据轮询错误率区分）。
    """
    online = bridge.state == BridgeStateEnum.running
    return OneBotActionResponse.ok(
        data={'online': online, 'good': online},
        echo=action.echo,
    )


async def handle_get_version_info(
    bridge: Bridge,  # noqa: ARG001
    session: AsyncSession,  # noqa: ARG001
    config: ServerConfig,  # noqa: ARG001
    action: OneBotAction,
) -> OneBotActionResponse:
    """
    返回 OneBot 实现的版本信息。

    OneBot 框架据此识别后端类型，可能据此选择兼容性策略。
    """
    return OneBotActionResponse.ok(
        data={
            'app_name': ONEBOT_APP_NAME,
            'app_version': ONEBOT_APP_VERSION,
            'protocol_version': ONEBOT_PROTOCOL_VERSION,
        },
        echo=action.echo,
    )


async def handle_can_send_image(
    bridge: Bridge,  # noqa: ARG001
    session: AsyncSession,  # noqa: ARG001
    config: ServerConfig,  # noqa: ARG001
    action: OneBotAction,
) -> OneBotActionResponse:
    """返回 bot 是否能发送图片（本桥接通过 iLink IMAGE(2) 始终支持）"""
    return OneBotActionResponse.ok(data={'yes': True}, echo=action.echo)


async def handle_can_send_record(
    bridge: Bridge,  # noqa: ARG001
    session: AsyncSession,  # noqa: ARG001
    config: ServerConfig,  # noqa: ARG001
    action: OneBotAction,
) -> OneBotActionResponse:
    """返回 bot 是否能发送语音（本桥接通过 iLink VOICE(3) 始终支持）"""
    return OneBotActionResponse.ok(data={'yes': True}, echo=action.echo)


def _parse_message_param(raw: object) -> list[OneBotSegment]:
    """
    把 OneBot v11 action.params['message'] 解析为段数组。

    支持：
    - 纯字符串：自动包装为 `text` 段
    - 标准 segment 数组：`[{type, data}, ...]`
    - 扁平 segment 数组（部分框架传入如 `[{type: 'text', text: '...'}]`）
    - 数组中混杂的字符串项：自动包装为 `text` 段
    """
    if isinstance(raw, str):
        return [OneBotSegment(type='text', data={'text': raw})]
    if isinstance(raw, list):
        parsed: list[OneBotSegment] = []
        for item in raw:
            if isinstance(item, str):
                parsed.append(OneBotSegment(type='text', data={'text': item}))
            elif isinstance(item, dict):
                if 'data' not in item or not isinstance(item.get('data'), dict):
                    data_dict: dict[str, Any] = {
                        k: v for k, v in item.items() if k != 'type'
                    }
                    parsed.append(
                        OneBotSegment(
                            type=str(item.get('type', 'text')),
                            data=data_dict,
                        )
                    )
                else:
                    parsed.append(OneBotSegment.model_validate(item))
            elif isinstance(item, OneBotSegment):
                parsed.append(item)
        return parsed
    raise ValueError(f"无法解析 message 参数: {type(raw).__name__}")


async def handle_send_private_msg(
    bridge: Bridge,
    session: AsyncSession,
    config: ServerConfig,  # noqa: ARG001
    action: OneBotAction,
) -> OneBotActionResponse:
    """
    发送私聊消息：解析 params → 委托 Bridge 转换并调用 iLink sendmessage。

    OneBot v11 params 结构：`{user_id: int, message: list | str, auto_escape?: bool}`
    """
    raw_user_id = action.params.get('user_id')
    raw_message = action.params.get('message')
    if raw_user_id is None or raw_message is None:
        return OneBotActionResponse.failed(
            retcode=OneBotRetCodeEnum.BAD_REQUEST, echo=action.echo,
        )
    onebot_user_id = int(raw_user_id)
    segments = _parse_message_param(raw_message)

    # 出站发送：失败时 ``handle_outbound_send_message`` 抛异常（``ILinkAPIError`` /
    # ``MediaOutboundError`` / ``ILinkContextTokenMissingError`` / ``ValueError``），
    # 由 ``dispatch_action`` 的 except 兜底转换为 retcode=1400 响应——本函数无须自己
    # 做 None 检查。iLink ``sendmessage`` 响应不返回 message_id，所以返回值始终是
    # None；不抛异常就视为发送成功。
    await bridge.handle_outbound_send_message(
        session=session,
        onebot_user_id=onebot_user_id,
        segments=segments,
    )

    # 分配 OneBot message_id 作为成功响应；iLink 端没有 message_id 可注册映射，
    # 所以本次不写 ``OneBotMessageMapping``（出站消息无法被 reply 引用是协议限制，
    # 与官方实现一致——iLink ``RefMessage`` 只引用 bot 收到的消息）
    registry = bridge.get_id_registry()
    onebot_message_id = await registry.next_message_id(session)
    return OneBotActionResponse.ok(
        data={'message_id': onebot_message_id},
        echo=action.echo,
    )


async def handle_send_msg(
    bridge: Bridge,
    session: AsyncSession,
    config: ServerConfig,
    action: OneBotAction,
) -> OneBotActionResponse:
    """
    统一发送消息（OneBot v11 兼容动作）。

    - `message_type='group'` 或包含 `group_id` 且无 `user_id`：返回 1404（iLink 不支持群聊）
    - `message_type='private'` 或包含 `user_id`：转发到 handle_send_private_msg
    """
    group_id = action.params.get('group_id')
    user_id = action.params.get('user_id')
    message_type = action.params.get('message_type')

    if (group_id is not None and user_id is None) or message_type == 'group':
        return OneBotActionResponse.failed(
            retcode=OneBotRetCodeEnum.NOT_FOUND,
            echo=action.echo,
            data={'error': "iLink 不支持群聊消息（仅支持私聊）"},
        )
    return await handle_send_private_msg(bridge, session, config, action)


async def handle_get_friend_list(
    bridge: Bridge,
    session: AsyncSession,
    config: ServerConfig,  # noqa: ARG001
    action: OneBotAction,
) -> OneBotActionResponse:
    """
    获取好友列表（OneBot v11 标准动作）。

    从 ContextToken 表中查询已与当前 Bot 建立对话的联系人，转换为 OneBot 好友项列表。
    若无记录返回空列表，避免框架（如 TRSS-Yunzai）在初始化时抛异常中断。
    """
    registry = bridge.get_id_registry()
    tokens = await ContextToken.get(
        session,
        col(ContextToken.bridge_id) == bridge.id,
        fetch_mode='all',
    )
    if not tokens:
        # 兼容单账号时期历史数据或全量查询
        tokens = await ContextToken.get(session, fetch_mode='all')

    friends: list[dict[str, Any]] = []
    if tokens is not None:
        for row in tokens:
            uid = registry.resolve_user_id(row.user_name)
            friends.append({
                'user_id': uid,
                'nickname': row.user_name,
                'remark': row.user_name,
            })
    return OneBotActionResponse.ok(data=friends, echo=action.echo)


async def handle_get_stranger_info(
    bridge: Bridge,
    session: AsyncSession,
    config: ServerConfig,  # noqa: ARG001
    action: OneBotAction,
) -> OneBotActionResponse:
    """
    获取陌生人信息（OneBot v11 标准动作）。

    若能在 ContextToken/缓存中反查到 user_name 则填入，否则返回占位信息。
    """
    raw_user_id = action.params.get('user_id')
    if raw_user_id is None:
        return OneBotActionResponse.failed(
            retcode=OneBotRetCodeEnum.BAD_REQUEST,
            echo=action.echo,
            data={'error': "缺少 user_id 参数"},
        )
    user_id = int(raw_user_id)
    user_name = await bridge.resolve_user_id_from_onebot(session, user_id)
    nickname = user_name if user_name is not None else str(user_id)
    return OneBotActionResponse.ok(
        data={
            'user_id': user_id,
            'nickname': nickname,
            'sex': 'unknown',
            'age': 0,
        },
        echo=action.echo,
    )


async def handle_get_group_list(
    bridge: Bridge,  # noqa: ARG001
    session: AsyncSession,  # noqa: ARG001
    config: ServerConfig,  # noqa: ARG001
    action: OneBotAction,
) -> OneBotActionResponse:
    """获取群列表（iLink 个人号无群聊，返回空列表）"""
    return OneBotActionResponse.ok(data=[], echo=action.echo)


async def handle_get_group_member_list(
    bridge: Bridge,  # noqa: ARG001
    session: AsyncSession,  # noqa: ARG001
    config: ServerConfig,  # noqa: ARG001
    action: OneBotAction,
) -> OneBotActionResponse:
    """获取群成员列表（iLink 个人号无群聊，返回空列表）"""
    return OneBotActionResponse.ok(data=[], echo=action.echo)


async def handle_set_model_show(
    bridge: Bridge,  # noqa: ARG001
    session: AsyncSession,  # noqa: ARG001
    config: ServerConfig,  # noqa: ARG001
    action: OneBotAction,
) -> OneBotActionResponse:
    """兼容 Yunzai/TRSS _set_model_show 占位接口"""
    return OneBotActionResponse.ok(data=None, echo=action.echo)


async def handle_get_guild_service_profile(
    bridge: Bridge,  # noqa: ARG001
    session: AsyncSession,  # noqa: ARG001
    config: ServerConfig,  # noqa: ARG001
    action: OneBotAction,
) -> OneBotActionResponse:
    """兼容频道服务资料查询（返回空字典）"""
    return OneBotActionResponse.ok(data={}, echo=action.echo)


async def handle_get_guild_list(
    bridge: Bridge,  # noqa: ARG001
    session: AsyncSession,  # noqa: ARG001
    config: ServerConfig,  # noqa: ARG001
    action: OneBotAction,
) -> OneBotActionResponse:
    """获取频道列表（iLink 个人号无频道，返回空列表）"""
    return OneBotActionResponse.ok(data=[], echo=action.echo)


async def handle_get_online_clients(
    bridge: Bridge,  # noqa: ARG001
    session: AsyncSession,  # noqa: ARG001
    config: ServerConfig,  # noqa: ARG001
    action: OneBotAction,
) -> OneBotActionResponse:
    """返回在线客户端列表（占位返回空客户端列表，与 SnowLuma 对齐）"""
    return OneBotActionResponse.ok(data={'clients': []}, echo=action.echo)


async def handle_get_cookies(
    bridge: Bridge,  # noqa: ARG001
    session: AsyncSession,  # noqa: ARG001
    config: ServerConfig,  # noqa: ARG001
    action: OneBotAction,
) -> OneBotActionResponse:
    """返回 cookies（iLink 非 QQ 协议，返回空 cookies）"""
    return OneBotActionResponse.ok(data={'cookies': ''}, echo=action.echo)


async def handle_get_csrf_token(
    bridge: Bridge,  # noqa: ARG001
    session: AsyncSession,  # noqa: ARG001
    config: ServerConfig,  # noqa: ARG001
    action: OneBotAction,
) -> OneBotActionResponse:
    """返回 CSRF token（iLink 非 QQ 协议，返回 0）"""
    return OneBotActionResponse.ok(data={'token': 0}, echo=action.echo)


# ===== 注册表 =====

ACTION_HANDLERS: dict[str, ActionHandler] = {
    'send_private_msg': handle_send_private_msg,
    'send_msg': handle_send_msg,
    'get_login_info': handle_get_login_info,
    'get_status': handle_get_status,
    'get_version_info': handle_get_version_info,
    'can_send_image': handle_can_send_image,
    'can_send_record': handle_can_send_record,
    'get_friend_list': handle_get_friend_list,
    'get_stranger_info': handle_get_stranger_info,
    'get_group_list': handle_get_group_list,
    'get_group_member_list': handle_get_group_member_list,
    '_set_model_show': handle_set_model_show,
    'get_guild_service_profile': handle_get_guild_service_profile,
    'get_guild_list': handle_get_guild_list,
    'get_online_clients': handle_get_online_clients,
    'get_cookies': handle_get_cookies,
    'get_csrf_token': handle_get_csrf_token,
}
"""
action 名称到 handler 函数的映射。

注册式分发让新增动作支持只需添加 handler + 注册一项，无需修改 dispatch 函数。
未注册的动作统一返回 retcode=1404。
"""


# ===== 分发入口 =====

async def _resolve_target_bridge(
    session: AsyncSession,
    default_bridge: 'Bridge',
    action: OneBotAction,
) -> 'Bridge':
    """
    解析当前 action 应交由哪个 Bridge 账号执行。

    路由规则（优先级从高到低）：
    1. action.self_id 或 action.params.get('self_id')：框架显式指定目标账号
    2. action.action == 'get_login_info' 且无 self_id：从待匹配 bootstrap 队列中提取
    3. action.params.get('user_id')：根据目标 OneBot user_id 反查属于哪个 Bridge 的上下文
    4. 回退：default_bridge
    """
    from sqlmodels.bridge.bridge import Bridge
    target_self_id = getattr(action, 'self_id', None)
    if target_self_id is None and isinstance(action.params, dict):
        raw_self_id = action.params.get('self_id')
        if raw_self_id is not None:
            try:
                target_self_id = int(raw_self_id)
            except (ValueError, TypeError):
                pass

    if target_self_id is not None:
        b = await Bridge.get_by_self_id(session, target_self_id)
        if b is not None:
            return b

    if action.action == 'get_login_info':
        queued_self_id = pop_pending_bootstrap_self_id()
        if queued_self_id is not None:
            b = await Bridge.get_by_self_id(session, queued_self_id)
            if b is not None:
                return b

    if isinstance(action.params, dict):
        raw_user_id = action.params.get('user_id')
        if raw_user_id is not None:
            active_bridges = await Bridge.get_all_active(session)
            for b in active_bridges:
                matched_ilink_user = await b.resolve_user_id_from_onebot(session, raw_user_id)
                if matched_ilink_user is not None:
                    return b

    return default_bridge


async def dispatch_action(
    bridge: Bridge,
    session_factory: async_sessionmaker[AsyncSession],
    config: ServerConfig,
    action: OneBotAction,
) -> OneBotActionResponse:
    """
    分发 OneBot v11 动作到对应的 handler，返回响应。

    每个动作打开独立的 AsyncSession，处理完即释放，避免长生命周期 session 占用连接。
    根据 action 参数智能路由到具体目标 Bridge。

    :param bridge: 默认 Bridge 实例
    :param session_factory: 数据库会话工厂
    :param config: ServerConfig 单例
    :param action: OneBot 动作请求
    :return: OneBot 动作响应（始终返回，不抛异常）
    """
    handler = ACTION_HANDLERS.get(action.action)
    if handler is None:
        l.warning(f"OneBot 不支持的动作: {action.action}")
        return OneBotActionResponse.failed(
            retcode=OneBotRetCodeEnum.NOT_FOUND,
            echo=action.echo,
        )

    async with session_factory() as session:
        target_bridge = await _resolve_target_bridge(session, bridge, action)
        try:
            return await handler(target_bridge, session, config, action)
        except Exception as e:
            l.exception(
                f"OneBot 动作 {action.action} 执行失败 (self_id={target_bridge.self_id}): {e}"
            )
            return OneBotActionResponse.failed(
                retcode=OneBotRetCodeEnum.BAD_REQUEST,
                echo=action.echo,
                data={'error': str(e)},
            )
