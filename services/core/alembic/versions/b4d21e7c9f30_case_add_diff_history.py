"""case add diff_history

Revision ID: b4d21e7c9f30
Revises: 3a0b7c365a15
Create Date: 2026-08-03 13:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b4d21e7c9f30'
down_revision: Union[str, Sequence[str], None] = '3a0b7c365a15'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # История парсингов дела: по записи на каждый вызов парсинга.
    # server_default '[]' — чтобы у уже существующих дел поле было пустым массивом,
    # а не NULL (иначе append_parse_entry пришлось бы отдельно обрабатывать NULL).
    op.add_column(
        'case',
        sa.Column(
            'diff_history',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('case', 'diff_history')
