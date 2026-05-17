"""
ServerConfig — 系统级运行时可调配置（充血模型单例）

职责划分：
- **meta_config (.env)**：部署时确定的基础设施（网络端口、磁盘路径、外部 URL）
- **ServerConfig (本类)**：系统级**运行时可调**参数（管理员通过 CLI 修改）
- **Bridge**：iLink 协议**会话状态**（协议层自动维护，管理员不应手动编辑）

ServerConfig 存储的是"管理员希望系统如何行为"的决策参数——轮询多快、出错重试几次、
心跳多长一次等。这些参数可以在运行时通过 CLI 或未来的管理端点修改，无需重启。

单例模式：ID 固定为 1，通过 `ServerConfig.get_instance(session)` 访问。
"""
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from sqlmodel_ext import (
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    SQLModelBase,
    Str64,
    TableBaseMixin,
)

# ServerConfig 单例行的固定主键（同一数据库只有一条记录）
_SERVER_CONFIG_SINGLETON_ID: int = 1


class ServerConfigBase(SQLModelBase):
    """
    ServerConfig 共享字段基类。

    所有字段都是"管理员可调的行为参数"，不是基础设施（那些在 meta_config）
    也不是协议会话状态（那些在 Bridge）。
    """

    # ===== Bot 身份 =====

    bot_display_name: Str64 = 'iLink OneBot Bridge'
    """
    Bot 对外显示名称。

    OneBot v11 框架通过 `get_login_info` 动作查询 bot 的 nickname，本字段就是返回值。
    登录前 Bridge.wechat_nickname 为 None 时作为占位使用；登录后仍作为
    get_login_info 的第一选择，允许管理员覆盖真实微信昵称（如显示"家庭助理"而非真名）。
    """

    # ===== 长轮询行为 =====

    poll_interval_seconds: PositiveFloat = 0.5
    """
    两次成功长轮询之间的延迟（秒）。

    长轮询本身会阻塞最多 35 秒直到有新消息或超时，这里的间隔仅在**轮询立即返回**
    （服务端无消息且游标已过期）时生效，避免空转高频调用。
    """

    poll_error_retry_seconds: PositiveFloat = 5.0
    """
    长轮询遇到网络错误或 iLink 业务错误后的初始重试延迟（秒）。

    每次连续失败后按指数退避乘以 2，直到达到 poll_error_max_delay_seconds。
    """

    poll_error_max_delay_seconds: PositiveFloat = 60.0
    """轮询错误退避的最大延迟（秒）"""

    max_consecutive_poll_errors: PositiveInt = 10
    """
    允许的最大连续轮询错误次数。

    超过此数后停止轮询循环并将 Bridge 状态标记为异常，等待管理员介入。
    """

    # ===== OneBot 心跳 =====

    heartbeat_interval_seconds: PositiveInt = 15
    """
    OneBot v11 心跳 meta_event 的发射间隔（秒）。

    OneBot 框架依赖心跳判断桥接是否存活，间隔过长会触发连接超时重连。
    """

    # ===== 会话过期行为 =====

    auto_logout_on_session_expired: bool = True
    """
    iLink 返回 errcode=-14 会话过期时，是否自动清空 Bridge.bot_token 并转入 expired 状态。

    关闭时会保留过期的 token 便于调试（仍会停止轮询）。
    """

    # ===== 消息 ID 映射保留 =====

    message_id_map_retention_seconds: NonNegativeInt = 86400
    """
    OneBot message_id ↔ iLink msg_id 映射的内存保留时长（秒）。

    超过此时长的映射会被定期清理任务从内存移除，避免长时间运行后内存无限膨胀。
    主要用途是 OneBot reply 段的反查，大多数用户只回复最近几小时内的消息。
    """


class ServerConfig(ServerConfigBase, TableBaseMixin, table=True):
    """
    系统级运行时可调配置单例。

    单例模式：ID 固定为 `_SERVER_CONFIG_SINGLETON_ID`，通过 `get_instance(session)` 访问。
    首次访问时自动创建默认实例并 commit。
    """

    @classmethod
    async def get_instance(cls, session: AsyncSession) -> 'ServerConfig':
        """
        获取 ServerConfig 单例实例。

        若不存在则创建默认实例并 commit。后续调用命中已创建的行。
        """
        config = await cls.get(session, col(cls.id) == _SERVER_CONFIG_SINGLETON_ID)
        if config is None:
            config = cls(id=_SERVER_CONFIG_SINGLETON_ID)
            config = await config.save(session)
        return config
