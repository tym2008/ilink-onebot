"""initial schema (拍平所有早期演化为单一 baseline)

包含全部 5 张表的最终 schema：

- ``bridge``：iLink 协议会话状态单例
- ``serverconfig``：管理员可调运行时配置单例
- ``contexttoken``：每用户 context_token 持久化
- ``mediafile``：媒体文件充血记录（CDN 嵌套字段映射）
- ``onebotmessagemapping``：OneBot ↔ iLink message_id 映射

Revision ID: 20260517_200000
Revises: (none, baseline)
Create Date: 2026-05-17 20:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = '20260517_200000'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建全部 5 张表（顺序：无依赖的先建）"""

    # ===== bridge =====
    op.create_table(
        'bridge',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'state',
            sqlmodel.sql.sqltypes.AutoString(length=32),
            nullable=False,
        ),
        sa.Column(
            'bot_token',
            sqlmodel.sql.sqltypes.AutoString(length=256),
            nullable=True,
        ),
        sa.Column(
            'wechat_id',
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=True,
        ),
        sa.Column(
            'wechat_nickname',
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=True,
        ),
        sa.Column(
            'get_updates_buf',
            sqlmodel.sql.sqltypes.AutoString(length=512),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint('id'),
    )

    # ===== serverconfig =====
    op.create_table(
        'serverconfig',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'bot_display_name',
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
        ),
        sa.Column('poll_interval_seconds', sa.Float(), nullable=False),
        sa.Column('poll_error_retry_seconds', sa.Float(), nullable=False),
        sa.Column('poll_error_max_delay_seconds', sa.Float(), nullable=False),
        sa.Column('max_consecutive_poll_errors', sa.Integer(), nullable=False),
        sa.Column('heartbeat_interval_seconds', sa.Integer(), nullable=False),
        sa.Column('auto_logout_on_session_expired', sa.Boolean(), nullable=False),
        sa.Column('message_id_map_retention_seconds', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    # ===== contexttoken =====
    op.create_table(
        'contexttoken',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'user_name',
            sqlmodel.sql.sqltypes.AutoString(length=256),
            nullable=False,
        ),
        sa.Column(
            'token',
            sqlmodel.sql.sqltypes.AutoString(length=512),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('contexttoken', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_contexttoken_user_name'),
            ['user_name'],
            unique=True,
        )

    # ===== mediafile（新协议嵌套字段映射） =====
    op.create_table(
        'mediafile',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'direction',
            sqlmodel.sql.sqltypes.AutoString(length=16),
            nullable=False,
        ),
        sa.Column('item_type', sa.Integer(), nullable=False),
        sa.Column(
            'cdn_full_url',
            sqlmodel.sql.sqltypes.AutoString(length=2048),
            nullable=True,
        ),
        sa.Column(
            'cdn_encrypt_query_param',
            sqlmodel.sql.sqltypes.AutoString(length=2048),
            nullable=True,
        ),
        sa.Column(
            'cdn_aes_key_base64',
            sqlmodel.sql.sqltypes.AutoString(length=256),
            nullable=True,
        ),
        sa.Column('cdn_encrypt_type', sa.Integer(), nullable=True),
        sa.Column(
            'image_aeskey_hex',
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=True,
        ),
        sa.Column(
            'filekey',
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=True,
        ),
        sa.Column(
            'onebot_source_url',
            sqlmodel.sql.sqltypes.AutoString(length=2048),
            nullable=True,
        ),
        sa.Column(
            'plaintext_md5_hex',
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=True,
        ),
        sa.Column('plaintext_size', sa.Integer(), nullable=True),
        sa.Column('ciphertext_size', sa.Integer(), nullable=True),
        sa.Column(
            'file_name',
            sqlmodel.sql.sqltypes.AutoString(length=256),
            nullable=False,
        ),
        sa.Column(
            'mime_type',
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=True,
        ),
        sa.Column(
            'local_path',
            sqlmodel.sql.sqltypes.AutoString(length=512),
            nullable=True,
        ),
        sa.Column(
            'ilink_msg_id',
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint('id'),
    )

    # ===== onebotmessagemapping =====
    op.create_table(
        'onebotmessagemapping',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'ilink_msg_id',
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
        ),
        sa.Column('onebot_message_id', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('onebotmessagemapping', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_onebotmessagemapping_ilink_msg_id'),
            ['ilink_msg_id'],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f('ix_onebotmessagemapping_onebot_message_id'),
            ['onebot_message_id'],
            unique=True,
        )


def downgrade() -> None:
    """drop 全部表（仅供完整性，baseline 一般不回滚）"""
    with op.batch_alter_table('onebotmessagemapping', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_onebotmessagemapping_onebot_message_id'))
        batch_op.drop_index(batch_op.f('ix_onebotmessagemapping_ilink_msg_id'))
    op.drop_table('onebotmessagemapping')

    op.drop_table('mediafile')

    with op.batch_alter_table('contexttoken', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_contexttoken_user_name'))
    op.drop_table('contexttoken')

    op.drop_table('serverconfig')
    op.drop_table('bridge')
