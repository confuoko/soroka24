"""case: registration/first instance/effective dates, superior number; side: role

Revision ID: b7e4a3f19c02
Revises: a5c2d80f61be
Create Date: 2026-08-04 18:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e4a3f19c02'
down_revision: Union[str, Sequence[str], None] = 'a5c2d80f61be'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Все колонки nullable: набор меток на карточке различается по типам дел, и почти
    # каждое поле у части дел отсутствует. Данные не теряются, backfill не нужен —
    # значения приедут при следующем разборе (в т.ч. перепарсингом из снапшотов).
    #
    # «Дата поступления» (гражданское) и «Дата регистрации» (КоАП) — РАЗНЫЕ метки разных
    # шаблонов, раньше склеивались в receipt_date. Теперь у каждой своя колонка; на
    # странице они взаимоисключающие, так что заполнена всегда ровно одна.
    op.add_column("case", sa.Column("registration_date", sa.Date(), nullable=True))
    op.add_column("case", sa.Column("first_instance_date", sa.Date(), nullable=True))
    # Решение храним строкой как есть («Удовлетворено, 21.05.2026») — как и status.
    # Дату из него не выделяем: она совпадает с first_instance_date во всех виденных делах.
    op.add_column("case", sa.Column("first_instance_decision", sa.String(), nullable=True))
    op.add_column("case", sa.Column("decision_effective_date", sa.Date(), nullable=True))
    op.add_column("case", sa.Column("superior_case_number", sa.String(), nullable=True))
    # Роль стороны с портала: «Взыскатель», «Должник», «Подсудимый», «Привлекаемое лицо»…
    # Раньше терялась: SideType знает только Истец/Ответчик/Другое, и всё остальное
    # схлопывалось в «Другое». SideType остаётся грубой классификацией для фильтров.
    op.add_column("side", sa.Column("role", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("side", "role")
    op.drop_column("case", "superior_case_number")
    op.drop_column("case", "decision_effective_date")
    op.drop_column("case", "first_instance_decision")
    op.drop_column("case", "first_instance_date")
    op.drop_column("case", "registration_date")
