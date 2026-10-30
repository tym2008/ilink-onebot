"""
OneBot v11 协议数据模型

继承树设计：

    ExtraIgnoreModelBase (sqlmodel-ext)
        │
        ├── OneBotEventBase                ← 所有事件共享 time/self_id/post_type
        │       ├── OneBotMessageEventBase     (+ message_type/message_id/user_id/message/raw_message/sender)
        │       │       └── OneBotPrivateMessageEvent  (+ sub_type='friend')
        │       │       └── OneBotGroupMessageEvent   (+ group_id, anonymous, sub_type)  ← 暂未启用（iLink 不支持群）
        │       │
        │       └── OneBotMetaEvent           (meta_event_type=heartbeat/lifecycle, + interval/status/sub_type)
        │
        ├── OneBotSegment                  ← 消息段（type + data dict）
        ├── OneBotSender                   ← 消息发送者元信息
        ├── OneBotStatus                   ← 心跳和 get_status 共享的状态对象
        │
        ├── OneBotAction                   ← 入站动作请求
        └── OneBotActionResponse           ← 出站动作响应

设计原则：
- 所有时间字段用 int (unix seconds)，按 OneBot v11 规范
- ID 字段用 int (OneBot v11 历史上是 QQ 号 int64)
- 消息段 `data` 用 `dict[str, str]`：v11 规范允许 string/number/bool，但所有客户端都接受字符串值
- 禁止裸 Pydantic，全部继承 sqlmodel-ext 的 SQLModelBase / ExtraIgnoreModelBase
"""
from enum import StrEnum
from typing import Any

from sqlmodel_ext import ExtraIgnoreModelBase, SQLModelBase


# ===== 枚举 =====

class OneBotPostTypeEnum(StrEnum):
    """OneBot v11 事件的 post_type"""
    message = 'message'
    notice = 'notice'
    request = 'request'
    meta_event = 'meta_event'


class OneBotMessageTypeEnum(StrEnum):
    """消息事件的 message_type"""
    private = 'private'
    group = 'group'


class OneBotPrivateSubTypeEnum(StrEnum):
    """私聊消息的 sub_type"""
    friend = 'friend'
    group = 'group'
    other = 'other'


class OneBotMetaEventTypeEnum(StrEnum):
    """meta_event 的子类型"""
    lifecycle = 'lifecycle'
    heartbeat = 'heartbeat'


class OneBotLifecycleSubTypeEnum(StrEnum):
    """lifecycle meta_event 的 sub_type"""
    enable = 'enable'
    disable = 'disable'
    connect = 'connect'


class OneBotActionStatusEnum(StrEnum):
    """OneBot 动作响应的 status 字段"""
    ok = 'ok'
    async_ = 'async'  # 'async' 是 Python 保留字
    failed = 'failed'


class OneBotRetCodeEnum:
    """
    OneBot v11 动作响应的 retcode 常量。

    用普通类而非 IntEnum，因为实现可以扩展自定义错误码，不应限制为闭枚举。
    """
    OK: int = 0
    """成功"""

    ASYNC_ACCEPTED: int = 1
    """异步操作已接受"""

    BAD_REQUEST: int = 1400
    """请求格式错误"""

    UNAUTHORIZED: int = 1401
    """缺少 access_token"""

    FORBIDDEN: int = 1403
    """access_token 错误"""

    NOT_FOUND: int = 1404
    """不支持的 action 或资源不存在"""


# ===== 消息段 =====

class OneBotSegment(SQLModelBase):
    """
    OneBot v11 消息段（array 格式）。

    示例：
    - `{"type": "text", "data": {"text": "hello"}}`
    - `{"type": "image", "data": {"file": "http://.../image.jpg"}}`
    - `{"type": "reply", "data": {"id": "12345"}}`

    `data` 用 `dict[str, str]` 而非 `dict[str, Any]`：v11 规范允许多种值类型，
    但所有主流实现都接受字符串值；统一字符串简化序列化和类型检查。
    """
    type: str
    """段类型（text/image/record/video/file/reply/at/share/face/...）"""

    data: dict[str, Any]
    """段数据字段（结构因 type 而异，支持字符串、数字等类型）"""


# ===== 发送者信息 =====

class OneBotSender(SQLModelBase):
    """
    消息事件的发送者信息（OneBot v11 sender 对象）。

    私聊消息只填写基础字段；群消息会额外含 card/role/level/title 等。
    本桥接只支持私聊，仅暴露私聊场景需要的字段。
    """
    user_id: int
    """发送者的 OneBot user_id（int64）"""

    nickname: str
    """发送者昵称（来自微信用户的 nickname，不可空避免框架显示 'null'）"""

    sex: str = 'unknown'
    """性别（male/female/unknown），iLink 不提供时统一 unknown"""

    age: int = 0
    """年龄，iLink 不提供时统一 0"""


# ===== 事件基类 =====

class OneBotEventBase(ExtraIgnoreModelBase):
    """
    所有 OneBot v11 事件的共享字段。

    OneBot v11 规范：每个事件都包含 time/self_id/post_type。
    继承 ExtraIgnoreModelBase 以便未来扩展字段时不破坏现有 framework 解析。
    """
    time: int
    """事件发生的 unix 时间戳（秒）"""

    self_id: int
    """收到事件的 bot 的 OneBot self_id（int64）"""

    post_type: OneBotPostTypeEnum
    """事件大类（message/notice/request/meta_event）"""


# ===== 消息事件 =====

class OneBotMessageEventBase(OneBotEventBase):
    """
    消息事件的共享字段（私聊和群聊共有）。

    post_type 固定为 'message'，message_type 区分 private/group。
    """
    post_type: OneBotPostTypeEnum = OneBotPostTypeEnum.message

    message_type: OneBotMessageTypeEnum
    """消息类型（private/group）"""

    message_id: int
    """消息 ID（由 Bridge 的 OneBotMessageIdRegistry 分配）"""

    user_id: int
    """发送者的 OneBot user_id"""

    message: list[OneBotSegment]
    """消息段数组（OneBot v11 array 格式）"""

    raw_message: str
    """
    消息的纯文本表示（人类可读）。

    供 OneBot 框架的简单关键词匹配使用，复杂消息（图片/视频等）
    通常仅含 `[图片]`、`[视频]` 之类的占位符。
    """

    font: int = 0
    """字体（OneBot v11 历史字段，保留以兼容）"""

    sender: OneBotSender
    """发送者元信息"""


class OneBotPrivateMessageEvent(OneBotMessageEventBase):
    """
    私聊消息事件（本桥接的唯一消息类型——iLink 不支持群聊）。

    OneBot v11 规范的 sub_type 在私聊场景固定为 'friend'。
    """
    message_type: OneBotMessageTypeEnum = OneBotMessageTypeEnum.private

    sub_type: OneBotPrivateSubTypeEnum = OneBotPrivateSubTypeEnum.friend
    """私聊子类型（friend/group/other），本桥接固定 friend"""


# ===== Meta 事件 =====

class OneBotStatus(SQLModelBase):
    """
    OneBot 状态对象（心跳事件和 get_status 动作共享）。

    OneBot v11 规范要求至少包含 online/good 两个布尔字段。
    """
    online: bool
    """bot 是否在线（对本桥接 = Bridge.state == running）"""

    good: bool
    """bot 是否运行良好（无未恢复的错误状态）"""


class OneBotHeartbeatStatus(OneBotStatus):
    """心跳事件的 status 字段（继承 OneBotStatus，未来可能扩展指标）"""


class OneBotMetaEvent(OneBotEventBase):
    """
    Meta 事件（lifecycle / heartbeat）。

    心跳事件比 lifecycle 事件多出 interval + status 字段，
    用 Optional 字段统一表达：lifecycle 时这些字段为 None。
    """
    post_type: OneBotPostTypeEnum = OneBotPostTypeEnum.meta_event

    meta_event_type: OneBotMetaEventTypeEnum
    """meta_event 子类型（lifecycle/heartbeat）"""

    sub_type: OneBotLifecycleSubTypeEnum | None = None
    """lifecycle 事件的子类型（enable/disable/connect）"""

    interval: int | None = None
    """心跳间隔（毫秒），仅 heartbeat 事件填写"""

    status: OneBotHeartbeatStatus | None = None
    """状态对象，仅 heartbeat 事件填写"""


# ===== 动作请求/响应 =====

class OneBotAction(ExtraIgnoreModelBase):
    """
    OneBot v11 动作请求（OneBot 框架 → 桥接）。

    继承 ExtraIgnoreModelBase 以便接受未来 v11 扩展动作的额外参数。

    示例：
    ```json
    {"action": "send_private_msg", "params": {"user_id": 123, "message": "hi"}, "echo": "1"}
    ```
    """
    action: str
    """动作名称（send_private_msg / get_login_info / get_status 等）"""

    params: dict[str, Any]
    """动作参数（不同 action 字段差异巨大，用 dict[str, Any] 承接）"""

    self_id: int | None = None
    """多账号场景下 OneBot 框架指定的目标 bot 虚拟 QQ 号（可选）"""

    echo: Any = None
    """
    回声字段（响应原样返回，用于异步请求的匹配）。

    OneBot v11 官方规范 ``botuniverse/onebot-11/communication/ws.md``：

        echo 字段是可选的，类似于 JSON RPC 的 id 字段，用于唯一标识一次请求，
        **可以是任何类型的数据**。OneBot 将会在调用结果中原样返回。

    主流框架 echo 形态：string（传统计数器）/ int（自增 seq）/ object（含 ``seq``
    / ``timestamp`` 等元数据，如 NoneBot2 v2 适配器）。本字段 ``Any`` 类型注解是
    协议规范要求的"任意类型"（CLAUDE.md "禁用 Any 除非真的无法确定" 的协议例外
    —— echo 的具体类型由对端框架决定，本端无法在编译期约束）。
    """


class OneBotActionResponse(SQLModelBase):
    """
    OneBot v11 动作响应（桥接 → OneBot 框架）。

    `status` 是字符串枚举，`retcode` 是整数错误码，二者冗余但 v11 规范要求都填。
    """
    status: OneBotActionStatusEnum
    """状态字符串（ok/async/failed）"""

    retcode: int
    """整数错误码（0=成功，非 0 见 OneBotRetCodeEnum）"""

    data: Any = None
    """动作返回数据（不同 action 结构差异巨大，支持 dict、list、None 等）"""

    echo: Any = None
    """回声字段（原样返回请求中的 echo，类型同 ``OneBotAction.echo`` 是任意 JSON 类型）"""

    @classmethod
    def ok(cls, data: Any = None, echo: Any = None) -> 'OneBotActionResponse':
        """构造成功响应的便捷工厂方法"""
        return cls(
            status=OneBotActionStatusEnum.ok,
            retcode=OneBotRetCodeEnum.OK,
            data=data,
            echo=echo,
        )

    @classmethod
    def failed(
        cls,
        retcode: int,
        echo: Any = None,
        data: Any = None,
    ) -> 'OneBotActionResponse':
        """构造失败响应的便捷工厂方法"""
        return cls(
            status=OneBotActionStatusEnum.failed,
            retcode=retcode,
            data=data,
            echo=echo,
        )
