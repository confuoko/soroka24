"""document: drop (case,type,date) unique, date not null, updated_at

Revision ID: a5c2d80f61be
Revises: e3b8f2c17d94
Create Date: 2026-08-04 17:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a5c2d80f61be'
down_revision: Union[str, Sequence[str], None] = 'e3b8f2c17d94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Документы до этой ревизии не сохранялись (парсер не читал вкладку), таблица пуста —
    # все изменения безопасны и backfill не нужен.
    #
    # 1. Тройка (дело, вид, дата) больше НЕ уникальна. Портал легально отдаёт несколько
    #    одинаковых строк за одну дату — у дела 77MS0002-01-2026-001597-10 это 21
    #    «Приложение» за 17.07.2026. Различить их в разметке нечем (ни id, ни номера),
    #    но терять нельзя, поэтому в identity входит номер повторения строки на странице
    #    (см. document_uid). Настоящий страж уникальности — ix_document_uid.
    op.drop_constraint(
        "document_case_id_document_type_document_date_key", "document", type_="unique"
    )
    # 2. Дата входит в identity, из которой считается uid: без неё uid не вычислить, и код
    #    это предполагает (парсер отбрасывает строки без даты, document_uid вызывает
    #    date.isoformat()). На портале дата заполнена во всех 160 виденных строках.
    op.alter_column("document", "document_date", existing_type=sa.Date(), nullable=False)
    # 3. Метка времени обновления — как у event / place_history / court_session.
    op.add_column(
        "document",
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("document", "updated_at")
    op.alter_column("document", "document_date", existing_type=sa.Date(), nullable=True)
    op.create_unique_constraint(
        "document_case_id_document_type_document_date_key",
        "document",
        ["case_id", "document_type", "document_date"],
    )
