"""
ContextToken — iLink 每用户 context_token 持久化表

iLink 协议要求：回复某用户时，`sendmessage` 请求必须携带该用户的 `context_token`
（由 `getupdates` 最新返回值获得）。每条消息都可能更新对应用户的 token。

设计选型：独立关系表 vs Bridge 表内 JSON 字段
------------------------------------------------
采用独立表的理由：
1. **增量更新**：每条接收消息只需 UPSERT 一个用户的 token 行，无需像 JSON 方案
   那样反序列化 → 修改 → 序列化 → 全量覆盖写回
2. **单一真相源**：每用户一条行，不会出现 JSON blob 与真实状态不一致
3. **可查询性**：SQLite 的 unique index on user_name 让按用户查找 O(log N)，
   对单表 JOIN（实际是索引点查）开销可接受
4. **可观察性**：可以直接 SQL 查询 "有多少个活跃会话"、"某用户最近一次发消息时间"，
   JSON 方案需要写 JSON1 函数

由于 Bridge 是全局单例，本表行不需要 bridge_id 外键——整表内容都属于那个唯一 Bridge。

CRUD 由 TableBaseMixin 提供；调用方直接使用 `ContextToken.get()` 查询、
`ContextToken(...).save()` 插入或 `row.save()` 更新。本模块不重复封装那些方法。
"""
from typing import Annotated, ClassVar

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from sqlmodel_ext import SQLModelBase, Str256, Str512, TableBaseMixin


# ===== Base 与业务模型 =====

class ContextTokenBase(SQLModelBase):
    """
    ContextToken 共享字段基类。

    包含 table 模型和未来可能的 DTO 类共享的字段。
    """
    bridge_id: int = Field(default=1, index=True)
    """所属 Bridge 账号 ID（默认 1，兼容单账号时期数据）"""

    user_name: Annotated[Str256, Field(index=True)]
    """
    用户的 iLink user_id（``WeixinMessage.from_user_id`` / ``to_user_id``）。

    历史列名保留为 ``user_name``，但语义上存的就是 iLink 的不透明用户标识（一串字符串）。
    与 bridge_id 组成联合唯一索引。
    """

    token: Str512
    """
    最新的 context_token。

    随用户发送的每条新消息更新，回复时作为 sendmessage 请求的必填字段。
    """


class ContextToken(ContextTokenBase, TableBaseMixin, table=True):
    """
    iLink 每用户 context_token 的持久化记录。

    纯数据表，所有 CRUD 能力继承自 TableBaseMixin：
    - 查询：`ContextToken.get(session, col(ContextToken.user_name) == name, col(ContextToken.bridge_id) == bid)`
    - 插入：`ContextToken(bridge_id=bid, user_name=name, token=tok).save(session)`
    - 更新：`row.token = new_tok; await row.save(session)`
    """
    __table_args__: ClassVar[tuple[object, ...]] = (
        UniqueConstraint('bridge_id', 'user_name', name='uq_contexttoken_bridge_user'),
    )
