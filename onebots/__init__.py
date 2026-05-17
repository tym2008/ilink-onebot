"""
OneBot v11 协议层（薄传输层）

职责边界：
- **本层只负责**：协议数据结构定义、WebSocket 服务端、动作分发、心跳发射
- **业务逻辑全部委托给 Bridge**：消息转换、iLink API 调用、状态管理

类比：OneBot v11 端点（onebots/）调用 Bridge 充血方法（sqlmodels/bridge/）就像
fastapis/ 端点调用领域模型的方法。

包级导出仅包含**不依赖 sqlmodels 的协议数据 + 服务接口**：
- `models`：纯数据类型（不引入领域依赖）
- `ws_server`：通用 WS 服务端（领域无关）

`action_handler` 和 `heartbeat` 这两个**业务联动**模块依赖 `sqlmodels.bridge`，
按完整路径直接导入以避免 `onebots ↔ sqlmodels.bridge` 循环依赖。
"""
from onebots.models import (
    OneBotAction,
    OneBotActionResponse,
    OneBotActionStatusEnum,
    OneBotEventBase,
    OneBotHeartbeatStatus,
    OneBotLifecycleSubTypeEnum,
    OneBotMessageEventBase,
    OneBotMessageTypeEnum,
    OneBotMetaEvent,
    OneBotMetaEventTypeEnum,
    OneBotPostTypeEnum,
    OneBotPrivateMessageEvent,
    OneBotPrivateSubTypeEnum,
    OneBotRetCodeEnum,
    OneBotSegment,
    OneBotSender,
    OneBotStatus,
)
from onebots.ws_server import OneBotWSServer
