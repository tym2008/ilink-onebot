"""
基础设施配置（MetaConfig 单例）

从 .env 文件读取环境变量。

配置分层原则：
- **meta_config**：部署时确定的基础设施配置（网络地址、文件路径、超时阈值、
  外部服务 URL 等），启动后不变
- **Bridge 模型字段**：运行时演化的业务状态（会话 token、扫码状态、微信昵称、
  长轮询游标等），存在 SQLite 中跨重启持久化

网络层（host/port/access_token）和文件系统层（缓存目录/TTL）都是基础设施，
放在这里而非 Bridge 表，避免每次查询 Bridge 都拉这些启动时确定的值。

用法：
    from meta_config import meta_config
    print(meta_config.DATABASE_URL)
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class MetaConfig(BaseSettings):
    """基础设施配置（启动时加载，运行时不变）"""
    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_file='.env',
        env_file_encoding='utf-8',
    )

    # ===== 日志 =====

    LOG_LEVEL: str = 'INFO'
    """日志级别（DEBUG/INFO/WARNING/ERROR）"""

    LOG_FILE_PATH: Path = Path('logs/ilink-onebot.log')
    """日志文件路径"""

    LOG_ROTATION_SIZE: str = '2 MB'
    """单个日志文件达到此大小后轮转（保守值，避免磁盘膨胀）"""

    LOG_RETENTION_COUNT: int = 5
    """保留的历史日志文件数量（轮转后超出数量的最旧文件会被删除）"""

    LOG_COMPRESSION: str = 'gz'
    """历史日志文件压缩格式（gz / zip / bz2 / xz / lzma / tar / tar.gz / tar.bz2 / tar.xz）"""

    # ===== 数据库 =====

    DATABASE_URL: str = 'sqlite+aiosqlite:///data/bridge.db'
    """SQLite 数据库连接字符串"""

    # ===== HTTP 客户端超时 =====

    AIOHTTP_TIMEOUT_TOTAL: int = 120
    """aiohttp 总超时（秒）"""

    AIOHTTP_TIMEOUT_CONNECT: int = 10
    """aiohttp 连接超时（秒）"""

    AIOHTTP_TIMEOUT_SOCK_READ: int = 60
    """aiohttp socket 读超时（秒）"""

    # ===== iLink 协议 =====

    ILINK_API_BASE_URL: str = 'https://ilinkai.weixin.qq.com'
    """iLink API 基础 URL（默认为微信官方，调试时可指向 mock 服务）"""

    # ===== OneBot v11 协议层 =====

    ONEBOT_WS_HOST: str = '0.0.0.0'
    """OneBot v11 Reverse WS 监听 host"""

    ONEBOT_WS_PORT: int = 6700
    """OneBot v11 Reverse WS 监听端口"""

    ONEBOT_ACCESS_TOKEN: str | None = None
    """
    OneBot v11 WS 连接认证 token。

    None 则不校验，任何连接都接受。生产环境建议配置以防止未授权连接。
    """

    ONEBOT_SELF_ID: int = 10000
    """
    OneBot v11 self_id（桥接的虚拟 QQ 号）。

    OneBot v11 协议要求 self_id 是 int64（历史原因：最初仅用于 QQ 机器人）。
    微信没有数字 ID 与之对应，因此本桥接使用管理员配置的固定数值作为 self_id，
    让 OneBot 框架能唯一识别本桥接发出的事件。

    在多框架连接同一桥接的场景下，所有框架都会看到相同的 self_id。
    建议选择一个不会与真实 QQ 号冲突的特殊值（如本默认值 10000）。
    """

    ONEBOT_REVERSE_WS_URL: str | None = None
    """
    OneBot v11 反向 WebSocket 客户端模式连接目标 URL（可选）。

    若配置（如 ``ws://nonebot-host:port/onebot/v11/ws``），桥接器主动作为 WS
    客户端连接到该 OneBot 框架的反向 WS 服务端。与正向 WS 服务端模式（``ONEBOT_WS_HOST/PORT``）
    可同时启用：framework 既可连过来也可被本端连过去。

    None 则不启用反向 WS 客户端。断开后自动重连（指数退避）。
    """

    # ===== 媒体 HTTP 服务 =====

    MEDIA_SERVE_HOST: str = '0.0.0.0'
    """媒体 HTTP 服务监听 host"""

    MEDIA_SERVE_PORT: int = 5701
    """媒体 HTTP 服务监听端口"""

    MEDIA_SERVE_PUBLIC_BASE_URL: str | None = None
    """
    媒体服务对外可访问的基础 URL（含 scheme/host/port）。

    若 None 则根据 MEDIA_SERVE_HOST/PORT 自动生成（0.0.0.0 回退到 127.0.0.1）。
    部署在 NAT/反向代理后时必须显式配置，让 OneBot 框架能通过公网地址下载媒体。
    """

    # ===== 媒体缓存 =====

    MEDIA_CACHE_DIR: Path = Path('data/media_cache')
    """本地媒体缓存目录（保存解密后的明文文件）"""

    MEDIA_CACHE_TTL_SECONDS: int = 3600
    """媒体缓存 TTL（秒），定期清理任务删除超时的本地缓存文件（DB 记录保留）"""


meta_config: MetaConfig = MetaConfig()
"""全局 MetaConfig 单例实例"""
