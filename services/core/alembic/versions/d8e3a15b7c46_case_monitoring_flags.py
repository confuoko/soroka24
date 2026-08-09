"""case add monitoring_enabled, last_checked_at, last_changed_at

Revision ID: d8e3a15b7c46
Revises: c2d95f31e7a8
Create Date: 2026-08-09 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8e3a15b7c46'
down_revision: Union[str, Sequence[str], None] = 'c2d95f31e7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Дело стоит на периодическом обходе. server_default false — чтобы у уже
    # существующих дел флаг был выставлен, а не NULL.
    op.add_column(
        'case',
        sa.Column(
            'monitoring_enabled',
            sa.Boolean(),
            server_default=sa.text('false'),
            nullable=False,
        ),
    )
    # Когда карточку последний раз ходили проверять (в т.ч. холостой обход).
    op.add_column('case', sa.Column('last_checked_at', sa.DateTime(), nullable=True))
    # Когда на портале последний раз что-то реально изменилось.
    op.add_column('case', sa.Column('last_changed_at', sa.DateTime(), nullable=True))

    # Планировщик выбирает дела условием
    #   monitoring_enabled AND (last_checked_at IS NULL OR last_checked_at < ...)
    # и сортирует по last_checked_at — оба поля под индексом.
    op.create_index('ix_case_monitoring_enabled', 'case', ['monitoring_enabled'])
    op.create_index('ix_case_last_checked_at', 'case', ['last_checked_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_case_last_checked_at', table_name='case')
    op.drop_index('ix_case_monitoring_enabled', table_name='case')
    op.drop_column('case', 'last_changed_at')
    op.drop_column('case', 'last_checked_at')
    op.drop_column('case', 'monitoring_enabled')
