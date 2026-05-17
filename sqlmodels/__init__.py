"""
sqlmodels — 领域核心层

领域驱动设计（DDD）+ 充血模型：
- Bridge: iLink 协议会话状态充血模型（单例）
- ServerConfig: 系统级运行时可调参数（单例）
- ContextToken: 每用户 context_token 持久化表（独立表）
- MediaFile: 媒体文件生命周期管理充血模型（入站 + 出站）
- OneBotMessageMapping: iLink ↔ OneBot 消息 ID 持久化映射
- OneBotMessageIdRegistry: 运行时消息/用户 ID 映射协调器（内存 + DB lazy lookup）

所有持久化模型继承 sqlmodel-ext 的 SQLModelBase / TableBaseMixin。
"""
from sqlmodels.bridge import Bridge, ContextToken
from sqlmodels.media_file import MediaFile
from sqlmodels.onebot_message_mapping import OneBotMessageMapping
from sqlmodels.server_config import ServerConfig
