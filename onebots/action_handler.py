"""
OneBot v11 动作分发器（薄分发层）

把 OneBot 框架发来的 action 请求路由到对应的 handler 函数。
每个 handler 委托给 Bridge 充血方法，本模块不写业务逻辑。

支持的动作（与 iLink 协议能力匹配）：
- `send_private_msg` / `send_msg`：调用 Bridge.send_text/send_media（具体由 converters 决定）
- `get_login_info`：返回 ServerConfig.bot_display_name + Bridge.onebot_self_id
- `get_status` / `get_version_info` / `can_send_image` / `can_send_record`：元信息查询

不支持的动作（iLink 无对应能力）：
- `send_group_msg` / `set_group_*`：iLink 不支持群聊
- `delete_msg`：iLink 无撤回 API
- `get_friend_list` / `get_stranger_info`：iLink 无联系人 API

不支持的动作统一返回 `retcode=1404`（NOT_FOUND）。
"""
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from loguru import logger as l
from sqlalchemy.ext.asyncio import async_sessionmaker
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
from sqlmodels.bridge import Bridge
from sqlmodels.bridge.enums import BridgeStateEnum
from sqlmodels.server_config import ServerConfig

if TYPE_CHECKING:
    pass


# 单个动作的处理函数签名：接收 (bridge, session, config, action) 返回 OneBotActionResponse
# 之所以传入 bridge/session/config 而非内部查询：
# - 调用方在外层已经获取了这些对象，避免重复查询
# - 便于测试时注入 mock
ActionHandler = Callable[
    [Bridge, AsyncSession, ServerConfig, OneBotAction],
    Awaitable[OneBotActionResponse],
]


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
    """
    nickname = config.bot_display_name
    if not nickname and bridge.wechat_nickname is not None:
        nickname = bridge.wechat_nickname
    return OneBotActionResponse.ok(
        data={
            'user_id': meta_config.ONEBOT_SELF_ID,
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

    OneBot v11 框架可能传入两种格式：
    - 数组：`[{type, data}, ...]`（OneBot v11 array 格式）
    - 字符串：纯文本，等价于 `[{type='text', data={text: ...}}]`

    本桥接暂不解析 CQ 码字符串（如 `[CQ:image,file=...]`）—— v11 array 格式
    是现代实现的主流，框架侧通常都用 array。
    """
    if isinstance(raw, str):
        return [OneBotSegment(type='text', data={'text': raw})]
    if isinstance(raw, list):
        return [OneBotSegment.model_validate(item) for item in raw]
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

    `message_type='private'` → 转发到 handle_send_private_msg。
    `message_type='group'` 或缺失 → 1404（本桥接不支持群聊）。
    """
    message_type = action.params.get('message_type', 'private')
    if message_type != 'private':
        return OneBotActionResponse.failed(
            retcode=OneBotRetCodeEnum.NOT_FOUND,
            echo=action.echo,
            data={'error': f"message_type={message_type} 不支持（iLink 无群聊）"},
        )
    return await handle_send_private_msg(bridge, session, config, action)


# ===== 注册表 =====

ACTION_HANDLERS: dict[str, ActionHandler] = {
    'send_private_msg': handle_send_private_msg,
    'send_msg': handle_send_msg,
    'get_login_info': handle_get_login_info,
    'get_status': handle_get_status,
    'get_version_info': handle_get_version_info,
    'can_send_image': handle_can_send_image,
    'can_send_record': handle_can_send_record,
}
"""
action 名称到 handler 函数的映射。

注册式分发让新增动作支持只需添加 handler + 注册一项，无需修改 dispatch 函数。
未注册的动作统一返回 retcode=1404。
"""


# ===== 分发入口 =====

async def dispatch_action(
    bridge: Bridge,
    session_factory: async_sessionmaker[AsyncSession],
    config: ServerConfig,
    action: OneBotAction,
) -> OneBotActionResponse:
    """
    分发 OneBot v11 动作到对应的 handler，返回响应。

    每个动作打开独立的 AsyncSession，处理完即释放，避免长生命周期 session 占用连接。
    Bridge 和 ServerConfig 单例由外层注入。

    :param bridge: Bridge 单例（在外层启动时加载，注入避免重复查询）
    :param session_factory: 数据库会话工厂
    :param config: ServerConfig 单例（同上）
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
        try:
            return await handler(bridge, session, config, action)
        except Exception as e:
            l.exception(f"OneBot 动作 {action.action} 执行失败: {e}")
            return OneBotActionResponse.failed(
                retcode=OneBotRetCodeEnum.BAD_REQUEST,
                echo=action.echo,
                data={'error': str(e)},
            )
