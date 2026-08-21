"""proxy: убрать поле comment

Revision ID: a7d4f2c81b39
Revises: e5b93d7a1c48
Create Date: 2026-08-20 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d4f2c81b39'
down_revision: Union[str, Sequence[str], None] = 'e5b93d7a1c48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Заметка человека про прокси: у кого куплен, до какого числа оплачен, какие
    # порталы берёт. Пул теперь из одного адреса одного провайдера, который доходит
    # до всех трёх порталов, — вести такие заметки негде и незачем.
    op.drop_column('proxy', 'comment')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('proxy', sa.Column('comment', sa.String(length=255), nullable=True))
