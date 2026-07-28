"""event add document_str

Revision ID: 3a0b7c365a15
Revises: 71aa569d4757
Create Date: 2026-07-26 15:08:24.691453

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3a0b7c365a15'
down_revision: Union[str, Sequence[str], None] = '71aa569d4757'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Название документа-основания текстом (в «Истории состояний» обычно нет ссылок).
    op.add_column("event", sa.Column("document_str", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("event", "document_str")
