"""support_multi_account

Revision ID: 20261030_144001
Revises: 20260517_200000
Create Date: 2026-10-30 14:40:01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '20261030_144001'
down_revision: Union[str, None] = '20260517_200000'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('bridge', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'account_name',
                sqlmodel.sql.sqltypes.AutoString(length=64),
                nullable=False,
                server_default='default',
            )
        )
        batch_op.add_column(
            sa.Column(
                'self_id',
                sa.Integer(),
                nullable=False,
                server_default='10000',
            )
        )
        batch_op.create_index(batch_op.f('ix_bridge_account_name'), ['account_name'], unique=True)
        batch_op.create_index(batch_op.f('ix_bridge_self_id'), ['self_id'], unique=True)

    with op.batch_alter_table('contexttoken', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'bridge_id',
                sa.Integer(),
                nullable=False,
                server_default='1',
            )
        )
        batch_op.drop_index(batch_op.f('ix_contexttoken_user_name'))
        batch_op.create_index(batch_op.f('ix_contexttoken_user_name'), ['user_name'], unique=False)
        batch_op.create_index(batch_op.f('ix_contexttoken_bridge_id'), ['bridge_id'], unique=False)
        batch_op.create_unique_constraint('uq_contexttoken_bridge_user', ['bridge_id', 'user_name'])


def downgrade() -> None:
    with op.batch_alter_table('contexttoken', schema=None) as batch_op:
        batch_op.drop_constraint('uq_contexttoken_bridge_user', type_='unique')
        batch_op.drop_index(batch_op.f('ix_contexttoken_bridge_id'))
        batch_op.drop_index(batch_op.f('ix_contexttoken_user_name'))
        batch_op.create_index(batch_op.f('ix_contexttoken_user_name'), ['user_name'], unique=1)
        batch_op.drop_column('bridge_id')

    with op.batch_alter_table('bridge', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bridge_self_id'))
        batch_op.drop_index(batch_op.f('ix_bridge_account_name'))
        batch_op.drop_column('self_id')
        batch_op.drop_column('account_name')
