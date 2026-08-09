"""event add published_at

Revision ID: f7b21c4e9a83
Revises: d8e3a15b7c46
Create Date: 2026-08-09 16:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7b21c4e9a83'
down_revision: Union[str, Sequence[str], None] = 'd8e3a15b7c46'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Дата публикации события на портале («Дата размещения», страницы типа B). В identity
    # события не входит, поэтому uid уже сохранённых событий не меняются.
    op.add_column('event', sa.Column('published_at', sa.Date(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('event', 'published_at')
