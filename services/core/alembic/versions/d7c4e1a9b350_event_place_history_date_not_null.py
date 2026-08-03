"""event, place_history: date not null

Revision ID: d7c4e1a9b350
Revises: b4d21e7c9f30
Create Date: 2026-08-03 16:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7c4e1a9b350'
down_revision: Union[str, Sequence[str], None] = 'b4d21e7c9f30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Дата входит в identity, из которой считается uid (event_uid /
    # place_history_uid): без неё uid не вычислить, и код это уже предполагает —
    # парсер отбрасывает строки без даты, а *_uid вызывает date.isoformat().
    # Приводим схему к фактическому контракту.
    #
    # NULL'ов быть не должно: ни один путь в коде их не пишет. Если миграция
    # упадёт на этом шаге, значит строки завелись вручную — посмотреть их:
    #   SELECT id, case_id, state_description FROM event WHERE event_date IS NULL;
    #   SELECT id, case_id, place_description FROM place_history WHERE place_date IS NULL;
    # Такие строки нерабочие (их uid невозможно пересчитать со страницы), их можно удалить.
    op.alter_column("event", "event_date", existing_type=sa.Date(), nullable=False)
    op.alter_column(
        "place_history", "place_date", existing_type=sa.Date(), nullable=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        "place_history", "place_date", existing_type=sa.Date(), nullable=True
    )
    op.alter_column("event", "event_date", existing_type=sa.Date(), nullable=True)
