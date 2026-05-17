"""
OneBotMessageMapping — iLink ↔ OneBot 消息 ID 双向映射的持久化表

每条已分配映射的 iLink msg_id ↔ OneBot message_id 都落入此表。配合
``OneBotMessageIdRegistry`` 的内存缓存层使用：

- 注册时：内存写入 + DB INSERT（事务内）
- 反查时：先查内存（O(1)），未命中再查 DB（unique index 点查 O(log N)），命中后回填内存
- 重启后：内存清空但 DB 保留，所有历史 reply 反查仍然可用

设计取舍：
- ``onebot_message_id`` 用整数 PK 而非自增 — 由内存计数器分配，保证单调；DB 端只是
  落盘。这样内存的 ``next_message_id()`` 不需要每次都 hit DB
- 双 unique index（ilink_msg_id 一项、onebot_message_id 一项）支持任一方向反查
- 不存 ``direction`` —— 入站和出站消息都有同等地位，没必要区分
- TTL 清理由独立的 ``Bridge`` 周期任务（未来）处理：扫描 ``created_at < now - retention``
  的行批量删除。映射表是 append-only + scheduled cleanup 的典型场景

DTO / Mixin：当前没有 DTO（不暴露 REST），只有 table 本体。
"""
from typing import Annotated

from sqlmodel import Field

from sqlmodel_ext import SQLModelBase, Str64, TableBaseMixin


# ===== Base 与业务模型 =====

class OneBotMessageMappingBase(SQLModelBase):
    """
    OneBotMessageMapping 共享字段基类。

    设为独立 Base 让未来如果需要 DTO（例如管理端列出最近映射）继承统一。
    """

    ilink_msg_id: Annotated[Str64, Field(unique=True, index=True)]
    """
    iLink 端的消息 ID（来自 ``WeixinMessage.message_id``，存为字符串）。

    unique + index：保证按 ilink_msg_id 反查时点查 O(log N)，且重复 register 时
    UPSERT 行为可预测（业务保证 register 是幂等调用）。
    """

    onebot_message_id: Annotated[int, Field(unique=True, index=True)]
    """
    OneBot 端的消息 ID（由 ``OneBotMessageIdRegistry`` 单调递增分配）。

    unique + index：保证按 onebot_message_id 反查时点查 O(log N)。
    虽然 register 的语义保证唯一，加 unique 约束作为 DB 层兜底。
    """


class OneBotMessageMapping(OneBotMessageMappingBase, TableBaseMixin, table=True):
    """
    iLink ↔ OneBot 消息 ID 持久化映射。

    纯数据表，所有 CRUD 能力继承自 ``TableBaseMixin``：
    - 查询：``OneBotMessageMapping.get(session, col(OneBotMessageMapping.ilink_msg_id) == ...)``
    - 插入：``OneBotMessageMapping(ilink_msg_id=..., onebot_message_id=...).save(session)``

    业务方法的协调由 ``OneBotMessageIdRegistry`` 负责（内存 + DB 两层），
    本类只关心 DB 层职责。
    """
