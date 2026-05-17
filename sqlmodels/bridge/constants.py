"""Bridge 模块私有常量

iLink 协议相关常量严格映射 Tencent/openclaw-weixin@2.4.3 的 ``src/api/`` + ``src/auth/login-qr.ts``。
"""

ILINK_DEFAULT_BASE_URL: str = 'https://ilinkai.weixin.qq.com'
"""iLink API 默认基础 URL（QR 请求固定走此 URL；运行期可被 baseurl 重定向）"""

ILINK_APP_ID: str = 'bot'
"""``iLink-App-Id`` 请求头值（来自 openclaw-weixin package.json.ilink_appid）"""

ILINK_APP_CLIENT_VERSION_MAJOR: int = 0
"""客户端版本 major。本桥接 0.1.0 → encoded uint32 = 256"""

ILINK_APP_CLIENT_VERSION_MINOR: int = 1
"""客户端版本 minor"""

ILINK_APP_CLIENT_VERSION_PATCH: int = 0
"""客户端版本 patch"""

ILINK_APP_CLIENT_VERSION: int = (
    (ILINK_APP_CLIENT_VERSION_MAJOR << 16)
    | (ILINK_APP_CLIENT_VERSION_MINOR << 8)
    | ILINK_APP_CLIENT_VERSION_PATCH
)
"""
``iLink-App-ClientVersion`` header 值（实际发送时 ``str(...)`` 转十进制字符串）。

协议规定为 uint32 编码 ``(major<<16)|(minor<<8)|patch``，让服务端能根据版本号
做兼容性策略（最小可用版本 / 已知 bug 客户端等）。
"""

ILINK_CHANNEL_VERSION: str = '0.1.0'
"""``base_info.channel_version`` 字段值（自报客户端 semver 版本）"""

ILINK_BOT_AGENT: str = 'ilink-onebot/0.1.0'
"""``base_info.bot_agent`` 字段值（自报上游 bot/app 身份，类 HTTP User-Agent）"""

ILINK_BOT_TYPE: str = '3'
"""``get_bot_qrcode`` 的 ``bot_type`` query 参数值（个人号 bot 固定 3）"""

ILINK_POLL_TIMEOUT_SECONDS: int = 35
"""``getupdates`` 长轮询超时（秒），与 iLink 服务端 hold 时长对齐"""

ILINK_ERR_SESSION_EXPIRED: int = -14
"""会话过期错误码（出现在 ``errcode`` 字段，需要重新扫码）"""

# ===== iLink API 端点（相对 base_url；不带前导斜杠以兼容 URL constructor） =====

ILINK_ENDPOINT_GET_BOT_QRCODE: str = 'ilink/bot/get_bot_qrcode'
"""POST，QR 流程起点"""

ILINK_ENDPOINT_GET_QRCODE_STATUS: str = 'ilink/bot/get_qrcode_status'
"""GET，QR 扫码状态轮询"""

ILINK_ENDPOINT_GETUPDATES: str = 'ilink/bot/getupdates'
"""POST，长轮询取新消息"""

ILINK_ENDPOINT_SENDMESSAGE: str = 'ilink/bot/sendmessage'
"""POST，发送消息"""

ILINK_ENDPOINT_GETUPLOADURL: str = 'ilink/bot/getuploadurl'
"""POST，请求媒体上传 URL"""

ILINK_ENDPOINT_GETCONFIG: str = 'ilink/bot/getconfig'
"""POST，获取 typing_ticket 等配置"""

ILINK_ENDPOINT_SENDTYPING: str = 'ilink/bot/sendtyping'
"""POST，发送 typing 状态"""

ILINK_ENDPOINT_NOTIFYSTART: str = 'ilink/bot/msg/notifystart'
"""POST，客户端上线通知"""

ILINK_ENDPOINT_NOTIFYSTOP: str = 'ilink/bot/msg/notifystop'
"""POST，客户端下线通知"""

# ===== 默认端口 =====

DEFAULT_ONEBOT_WS_PORT: int = 6700
"""OneBot v11 Reverse WS 默认端口"""

DEFAULT_MEDIA_SERVE_PORT: int = 5701
"""媒体 HTTP 服务默认端口"""

DEFAULT_ONEBOT_SELF_ID: int = 10000
"""默认 OneBot self_id（虚拟 QQ 号，避免与真实账号冲突的取值）"""

ONEBOT_TEXT_CHUNK_MAX_LENGTH: int = 4000
"""消息文本段最大长度（OneBot v11 兼容性限制）"""

# ===== 扫码登录 / 自动重登 =====

QRCODE_POLL_INTERVAL_SECONDS: float = 2.0
"""
扫码状态轮询间隔（秒）。

``get_qrcode_status`` 实际是长轮询接口（服务端 hold 最多 35s），但客户端按固定
间隔重试，不依赖打满 35s。2 秒是延迟与压力的折衷。
"""

QRCODE_TOTAL_TIMEOUT_SECONDS: float = 480.0
"""扫码总超时（秒），腾讯官方实现默认 480s（8 分钟，覆盖 QR 刷新 + 配对码输入）"""

QRCODE_LONG_POLL_TIMEOUT_MS: int = 35_000
"""``get_qrcode_status`` 客户端单次请求超时（毫秒，与服务端 hold 时长对齐）"""
