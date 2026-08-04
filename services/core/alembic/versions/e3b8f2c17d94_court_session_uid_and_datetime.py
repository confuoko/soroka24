"""court_session: uid, timestamps, session_date -> datetime

Revision ID: e3b8f2c17d94
Revises: d7c4e1a9b350
Create Date: 2026-08-04 17:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3b8f2c17d94'
down_revision: Union[str, Sequence[str], None] = 'd7c4e1a9b350'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Заседания до этой ревизии не сохранялись вообще (парсер не читал вкладку), поэтому
    # таблица пуста и все изменения безопасны: ни backfill, ни USING не нужны.
    #
    # 1. Портал отдаёт «Дата и время» одной колонкой («30.07.2026 16:50»), и время входит
    #    в identity заседания (см. court_session_uid) — значит дата+время, а не дата.
    op.alter_column(
        "court_session",
        "session_date",
        existing_type=sa.Date(),
        type_=sa.DateTime(),
        existing_nullable=False,
    )
    # 2. uid — как у event и place_history: стабильный внешний идентификатор, по которому
    #    повторный парсинг узнаёт уже сохранённое заседание. UNIQUE обязателен: он и
    #    защищает от повторной вставки той же строки.
    op.add_column("court_session", sa.Column("uid", sa.Uuid(), nullable=False))
    op.create_index("ix_court_session_uid", "court_session", ["uid"], unique=True)
    # 3. Метки времени — как у остальных дочерних сущностей дела.
    op.add_column(
        "court_session",
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.add_column(
        "court_session",
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("court_session", "updated_at")
    op.drop_column("court_session", "created_at")
    op.drop_index("ix_court_session_uid", table_name="court_session")
    op.drop_column("court_session", "uid")
    # Обратно в Date время отбрасывается — Postgres приводит timestamp к date сам.
    op.alter_column(
        "court_session",
        "session_date",
        existing_type=sa.DateTime(),
        type_=sa.Date(),
        existing_nullable=False,
    )
