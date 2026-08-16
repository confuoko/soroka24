"""case add accepted_date

Revision ID: a1c6d3f95e27
Revises: f7b21c4e9a83
Create Date: 2026-08-16 13:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c6d3f95e27'
down_revision: Union[str, Sequence[str], None] = 'f7b21c4e9a83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Дата принятия к производству — метка «Дата принятия к производству» на страницах
    # типа D (mirsud.spb.ru), у гражданских дел. Отдельное поле, а не re-use
    # registration_date: та по смыслу про дела КоАП, а от receipt_date эта дата
    # отличается по существу и на живых делах с ней расходится.
    op.add_column('case', sa.Column('accepted_date', sa.Date(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('case', 'accepted_date')
