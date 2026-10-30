"""OneBot v11 协议层私有常量"""

# OneBot v11 实现元信息（get_version_info 动作返回）
ONEBOT_APP_NAME: str = 'ilink-onebot'
"""OneBot get_version_info 返回的 app_name"""

ONEBOT_APP_VERSION: str = '0.1.0'
"""OneBot get_version_info 返回的 app_version（与项目版本一致）"""

ONEBOT_PROTOCOL_VERSION: str = 'v11'
"""OneBot 协议版本标识符"""

# Reverse WS 路径前缀（OneBot 框架的标准约定）
ONEBOT_WS_PATH: str = '/onebot/v11/ws'
"""OneBot v11 Reverse WS 端点路径"""

# Authorization 头前缀
BEARER_PREFIX: str = 'Bearer '
"""Bearer token 头部前缀（含尾部空格）"""

# query string 中的 token 参数名（v11 规范允许 query 或 header）
QUERY_ACCESS_TOKEN_KEY: str = 'access_token'

# Reverse WS 自定义头（v11 规范）
HEADER_SELF_ID: str = 'X-Self-ID'
"""Bridge 推 Reverse WS 时携带的 self_id 头"""

HEADER_CLIENT_ROLE: str = 'X-Client-Role'
"""客户端角色头（API / Event / Universal）"""

CLIENT_ROLE_UNIVERSAL: str = 'Universal'
"""通用 WS 客户端角色（同时收事件和接收动作）"""

DEFAULT_USER_AGENT: str = f'{ONEBOT_APP_NAME}/{ONEBOT_APP_VERSION}'
"""Reverse WS 客户端默认 User-Agent（避免发送 Python 标识）"""

