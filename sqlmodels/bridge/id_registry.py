"""
OneBotMessageIdRegistry — OneBot 消息/用户 ID 映射协调器（内存 + DB 两层）

充血协调对象，聚合三个相关职责：

1. **OneBot message_id 分配**：单调递增计数器，每条新消息生成唯一 int。重启时
   通过 ``SELECT MAX(onebot_message_id)`` 从 ``OneBotMessageMapping`` 恢复，
   保证跨重启单调
2. **iLink ↔ OneBot 消息 ID 双向映射**：内存命中走内存（O(1)），未命中查 DB
   （unique index 点查 O(log N)）并回填内存。写入时同步写 DB 持久化
3. **WeChat user_name → OneBot user_id 确定性哈希**：纯内存（SHA-256 截断本身
   就是 deterministic，无需持久化——重启后同一 user_name 仍映射到相同 user_id）

设计为充血对象的理由：

- 把"内存 cache"与"DB 持久化"两层职责协调起来，对外暴露统一的 register/resolve API
- Bridge 持有一个 registry 实例作为 ClassVar 单例，业务方法委托给它
- ``OneBotMessageMapping`` 表只负责 DB 层 CRUD，registry 负责"先查内存、再查 DB、
  回填内存"的协调
"""
import hashlib
from typing import ClassVar

from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from sqlmodel_ext import SQLModelBase

from sqlmodels.onebot_message_mapping import OneBotMessageMapping


class OneBotMessageIdRegistry(SQLModelBase):
    """
    OneBot 消息/用户 ID 映射的运行时充血对象。

    内部状态：

    - ``_message_id_counter``：内存中下一个待分配的 OneBot message_id。第一次访问时
      lazy 从 DB ``MAX(onebot_message_id)`` 初始化，之后纯内存自增
    - ``_ilink_to_onebot`` / ``_onebot_to_ilink``：消息 ID 双向内存 cache。未命中
      会查 ``OneBotMessageMapping`` 并回填
    - ``_user_name_to_id`` / ``_user_id_to_name``：user 哈希缓存（纯内存，不需要 DB）
    - ``_counter_initialized``：是否已从 DB 恢复计数器

    单线程异步模型下无需加锁。
    """

    _message_id_counter: int = 0
    _counter_initialized: bool = False
    _ilink_to_onebot: dict[str, int] = {}
    _onebot_to_ilink: dict[int, str] = {}
    _user_name_to_id: dict[str, int] = {}
    _user_id_to_name: dict[int, str] = {}

    # 内存缓存的上限，超过后按 FIFO 淘汰（防止长期运行内存膨胀）
    # 选 10000：覆盖典型用户最近几天的活跃消息，超出部分仍可通过 DB 查询
    _MAX_CACHE_ENTRIES: ClassVar[int] = 10000

    def model_post_init(self, __context: object) -> None:
        """每次实例化时重置状态，避免 Pydantic 类级别默认值共享"""
        super().model_post_init(__context)
        self._message_id_counter = 0
        self._counter_initialized = False
        self._ilink_to_onebot = {}
        self._onebot_to_ilink = {}
        self._user_name_to_id = {}
        self._user_id_to_name = {}

    # ===== OneBot message_id 分配（持久化计数器） =====

    async def next_message_id(self, session: AsyncSession) -> int:
        """
        分配下一个 OneBot message_id（单调递增，跨重启保持）。

        首次调用时从 ``MAX(OneBotMessageMapping.onebot_message_id)`` 恢复计数器，
        之后纯内存递增。这样 register 写 DB 时，内存与 DB 计数器保持一致。

        :param session: 数据库会话（仅首次调用时实际查询 DB）
        :return: 新分配的 OneBot message_id
        """
        if not self._counter_initialized:
            # 用 TableBaseMixin.get 走 ORM 路径，order_by + fetch_mode='first' 拿到最新的一条
            # 比起裸 select(...).order_by(...).limit(1) + session.exec()，与项目风格统一
            latest = await OneBotMessageMapping.get(
                session,
                order_by=[col(OneBotMessageMapping.onebot_message_id).desc()],
                fetch_mode='first',
            )
            self._message_id_counter = latest.onebot_message_id if latest is not None else 0
            self._counter_initialized = True

        self._message_id_counter += 1
        return self._message_id_counter

    # ===== iLink ↔ OneBot 消息 ID 双向映射（持久化 + 内存 cache） =====

    async def register_message_id(
        self,
        session: AsyncSession,
        ilink_msg_id: str,
        onebot_message_id: int,
        *,
        commit: bool = True,
    ) -> None:
        """
        记录一对 iLink msg_id ↔ OneBot message_id 双向映射（同步写 DB + 内存）。

        幂等：内存已存在或 DB unique 冲突时静默忽略——同一对 ID 重复 register 不该
        发生但不应崩溃。

        :param session: 数据库会话
        :param ilink_msg_id: iLink 端的消息 ID
        :param onebot_message_id: OneBot 端的消息 ID
        :param commit: 是否提交 DB 变更
        """
        # 内存已注册：幂等返回
        if self._ilink_to_onebot.get(ilink_msg_id) == onebot_message_id:
            return

        # DB 落盘
        existing = await OneBotMessageMapping.get(
            session,
            col(OneBotMessageMapping.ilink_msg_id) == ilink_msg_id,
        )
        if existing is None:
            mapping = OneBotMessageMapping(
                ilink_msg_id=ilink_msg_id,
                onebot_message_id=onebot_message_id,
            )
            _ = await mapping.save(session, commit=commit)

        # 写内存 cache
        self._ilink_to_onebot[ilink_msg_id] = onebot_message_id
        self._onebot_to_ilink[onebot_message_id] = ilink_msg_id
        self._evict_if_oversized()

    async def resolve_onebot_message_id(
        self,
        session: AsyncSession,
        ilink_msg_id: str,
    ) -> int | None:
        """
        通过 iLink msg_id 查找对应的 OneBot message_id。

        先查内存 cache，未命中则查 ``OneBotMessageMapping`` 表（unique index 点查）
        并回填内存。

        :return: 对应的 OneBot message_id，未找到返回 None
        """
        cached = self._ilink_to_onebot.get(ilink_msg_id)
        if cached is not None:
            return cached

        row = await OneBotMessageMapping.get(
            session,
            col(OneBotMessageMapping.ilink_msg_id) == ilink_msg_id,
        )
        if row is None:
            return None

        self._ilink_to_onebot[ilink_msg_id] = row.onebot_message_id
        self._onebot_to_ilink[row.onebot_message_id] = ilink_msg_id
        self._evict_if_oversized()
        return row.onebot_message_id

    async def resolve_ilink_message_id(
        self,
        session: AsyncSession,
        onebot_message_id: int,
    ) -> str | None:
        """
        通过 OneBot message_id 反查 iLink msg_id。

        先查内存 cache，未命中则查 ``OneBotMessageMapping`` 并回填内存。

        :return: 对应的 iLink msg_id，未找到返回 None
        """
        cached = self._onebot_to_ilink.get(onebot_message_id)
        if cached is not None:
            return cached

        row = await OneBotMessageMapping.get(
            session,
            col(OneBotMessageMapping.onebot_message_id) == onebot_message_id,
        )
        if row is None:
            return None

        self._ilink_to_onebot[row.ilink_msg_id] = onebot_message_id
        self._onebot_to_ilink[onebot_message_id] = row.ilink_msg_id
        self._evict_if_oversized()
        return row.ilink_msg_id

    def _evict_if_oversized(self) -> None:
        """
        当任一内存 cache 超过 ``_MAX_CACHE_ENTRIES`` 时，按插入顺序淘汰最早的项。

        Python 3.7+ 的 dict 保留插入顺序，``next(iter(d))`` 给出最早的 key。
        DB 仍保留所有映射，淘汰的项仍可通过 ``resolve_*`` 的 DB 兜底获取。
        """
        if len(self._ilink_to_onebot) <= self._MAX_CACHE_ENTRIES:
            return
        # 同时淘汰正反两个字典中的对应项
        overflow = len(self._ilink_to_onebot) - self._MAX_CACHE_ENTRIES
        for _ in range(overflow):
            oldest_ilink = next(iter(self._ilink_to_onebot))
            oldest_onebot = self._ilink_to_onebot.pop(oldest_ilink)
            self._onebot_to_ilink.pop(oldest_onebot, None)

    # ===== WeChat user_name ↔ OneBot user_id 双向映射（纯内存） =====

    def resolve_user_id(self, user_name: str) -> int:
        """
        将 iLink user_name 确定性映射为 OneBot int64 user_id。

        使用 SHA-256 截断为 52 位正整数（最大值 0xFFFFFFFFFFFFF = 4503599627370495，
        严格小于 JavaScript 的 Number.MAX_SAFE_INTEGER = 2^53 - 1 = 9007199254740991，
        彻底杜绝在 Node.js / Yunzai 等 JS 框架反序列化 JSON 时产生浮点舍入精度截断）。
        同一 user_name 跨重启始终映射到相同 user_id。

        首次查询会计算哈希并同时写入正反两个缓存字典，同时将历史 60 位（15 hex）哈希及其在
        JavaScript float64 舍入后的别名一并写入反向索引，保证新老 ID 与舍入值均能瞬时反查命中。

        :param user_name: iLink 的 user_name 字符串
        :return: 52 位正整数，作为 OneBot 的 user_id
        """
        cached = self._user_name_to_id.get(user_name)
        if cached is not None:
            return cached

        digest = hashlib.sha256(user_name.encode('utf-8')).hexdigest()
        # 取前 13 个 hex 字符 = 52 位，保证为严格安全的 JS 整数
        user_id = int(digest[:13], 16)
        self._user_name_to_id[user_name] = user_id
        self._user_id_to_name[user_id] = user_name

        # 兼容历史版本 60 位哈希及 JS 端 JSON.parse(double float64) 舍入产生的别名
        legacy_60 = int(digest[:15], 16)
        self._user_id_to_name[legacy_60] = user_name
        self._user_id_to_name[int(float(legacy_60))] = user_name

        return user_id

    def lookup_user_name_cached(self, onebot_user_id: int) -> str | None:
        """
        反查 OneBot user_id 对应的 iLink user_name（仅查内存缓存）。

        缓存命中返回 user_name；未命中返回 None，调用方应回退到 DB 扫描
        （遍历 ContextToken 表并对每行 ``resolve_user_id``），从而填充缓存。

        支持直接命中（52 位新 ID、历史 60 位 ID 及其 float 舍入别名）以及
        对任意被 JS 浮点截断的超长整数的 double 精度等价兜底匹配。
        """
        # 1. 优先直接匹配缓存（O(1)）
        cached = self._user_id_to_name.get(onebot_user_id)
        if cached is not None:
            return cached

        # 2. 兜底容错：比对 double float 精度等价性（处理历史超出 53 位的各种精度截断值）
        try:
            target_float = float(onebot_user_id)
            for uid, name in list(self._user_id_to_name.items()):
                if float(uid) == target_float:
                    # 回填直接索引以加速后续查询
                    self._user_id_to_name[onebot_user_id] = name
                    return name
        except (ValueError, OverflowError):
            pass

        return None
