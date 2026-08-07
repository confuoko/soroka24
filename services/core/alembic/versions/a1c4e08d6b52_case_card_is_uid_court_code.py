"""case: карточка = тройка (уид, суд, номер дела); номер стал обязательным

Revision ID: a1c4e08d6b52
Revises: b3f7c21a9e04
Create Date: 2026-08-07 12:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c4e08d6b52'
down_revision: Union[str, Sequence[str], None] = 'b3f7c21a9e04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    connection = op.get_bind()

    # По одному УИД в одном суде бывает несколько дел с разными номерами (приказное
    # производство, его отмена, затем исковое) — раньше они затирали друг друга, потому что
    # карточка искалась по паре «УИД + суд». Теперь номер дела входит в ключ, а значит
    # обязан быть у каждой карточки.

    # 1. У гражданских карточек номер лежал под меткой «Номер заявления» и попадал в
    #    application_number, а code оставался пустым — переносим.
    op.execute(
        'UPDATE "case" SET code = application_number '
        "WHERE code IS NULL AND application_number IS NOT NULL"
    )

    # 2. Что не перенеслось — руками: выбрать номер за человека мы не можем, а карточка
    #    без номера в новой схеме существовать не должна.
    orphans = connection.execute(
        sa.text('SELECT id, uid FROM "case" WHERE code IS NULL ORDER BY id LIMIT 20')
    ).fetchall()
    if orphans:
        listed = ", ".join(f"{row[0]} ({row[1]})" for row in orphans)
        total = connection.execute(
            sa.text('SELECT count(*) FROM "case" WHERE code IS NULL')
        ).scalar()
        raise RuntimeError(
            f"У {total} карточек нет номера дела, а он стал частью ключа. Первые: {listed}. "
            f"Проставьте номер (или удалите карточки) и повторите."
        )

    op.alter_column("case", "code", existing_type=sa.String(), nullable=False)

    # 3. Ключ карточки — тройка.
    op.drop_constraint("uq_case_uid_court", "case", type_="unique")
    op.create_unique_constraint(
        "uq_case_uid_court_code", "case", ["uid", "court_id", "code"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Обратный переход упадёт, если в одном суде по одному УИД накопилось несколько
    # карточек: пара «УИД + суд» их уже не различает, и выбрать выжившую автоматически
    # нельзя. Это осознанно — пусть лучше упадёт, чем потеряет данные.
    op.drop_constraint("uq_case_uid_court_code", "case", type_="unique")
    op.create_unique_constraint("uq_case_uid_court", "case", ["uid", "court_id"])
    op.alter_column("case", "code", existing_type=sa.String(), nullable=True)
