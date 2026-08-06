"""case: карточка = пара (уид, суд); ссылки вынесены в case_url

Revision ID: e5b71c9a4d28
Revises: d1a6c3e802f7
Create Date: 2026-08-06 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b71c9a4d28'
down_revision: Union[str, Sequence[str], None] = 'd1a6c3e802f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    connection = op.get_bind()

    # Единица учёта теперь — карточка дела в конкретном суде. УИД сквозной и не
    # меняется при переходе по инстанциям, поэтому один и тот же УИД встречается на
    # странице участка мирового судьи и на странице районного суда — это разные
    # карточки с разным содержимым, и раньше они не могли сосуществовать.

    # 1. Суд переезжает из связки many-to-many в обычный внешний ключ.
    op.add_column("case", sa.Column("court_id", sa.BigInteger(), nullable=True))

    # Дело с несколькими судами разложить автоматически нельзя: неизвестно, какой из
    # них «свой» для карточки. Лучше упасть здесь, чем молча потерять привязку.
    conflicting = connection.execute(
        sa.text(
            "SELECT case_id FROM case_court GROUP BY case_id HAVING count(*) > 1 LIMIT 5"
        )
    ).fetchall()
    if conflicting:
        ids = ", ".join(str(row[0]) for row in conflicting)
        raise RuntimeError(
            f"У дел {ids} (и, возможно, других) больше одного суда — миграция не может "
            f"выбрать нужный. Разберите их вручную и повторите."
        )

    op.execute(
        'UPDATE "case" SET court_id = ('
        '    SELECT court_id FROM case_court WHERE case_court.case_id = "case".id'
        ")"
    )

    orphans = connection.execute(
        sa.text('SELECT count(*) FROM "case" WHERE court_id IS NULL')
    ).scalar()
    if orphans:
        raise RuntimeError(
            f"У {orphans} дел не оказалось суда, а карточка без суда невозможна. "
            f"Заведите суд или удалите такие дела и повторите."
        )

    op.alter_column("case", "court_id", existing_type=sa.BigInteger(), nullable=False)
    op.create_foreign_key("case_court_id_fkey", "case", "court", ["court_id"], ["id"])
    op.create_index(op.f("ix_case_court_id"), "case", ["court_id"])
    op.drop_table("case_court")

    # 2. Ссылки переезжают в отдельную таблицу: их у карточки бывает несколько.
    op.create_table(
        "case_url",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("case_id", sa.BigInteger(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["case.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_case_url_case_id"), "case_url", ["case_id"])
    # Уникальность глобальная: один адрес ведёт ровно в одну карточку.
    op.create_index(op.f("ix_case_url_url"), "case_url", ["url"], unique=True)
    op.execute(
        'INSERT INTO case_url (case_id, url) '
        'SELECT id, url FROM "case" WHERE url IS NOT NULL'
    )
    op.drop_column("case", "url")

    # 3. УИД сам по себе больше не уникален — уникальна пара с судом.
    op.drop_index("ix_case_uid", table_name="case")
    op.create_index(op.f("ix_case_uid"), "case", ["uid"])
    op.create_unique_constraint("uq_case_uid_court", "case", ["uid", "court_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("uq_case_uid_court", "case", type_="unique")
    op.drop_index(op.f("ix_case_uid"), table_name="case")
    op.create_index("ix_case_uid", "case", ["uid"], unique=True)

    # Возвращаем колонку url: из нескольких ссылок карточки берём самую раннюю.
    op.add_column("case", sa.Column("url", sa.String(), nullable=True))
    op.execute(
        'UPDATE "case" SET url = ('
        "    SELECT url FROM case_url WHERE case_url.case_id = \"case\".id"
        "    ORDER BY created_at, id LIMIT 1"
        ")"
    )
    op.create_index("ix_case_url", "case", ["url"], unique=True)
    op.drop_index(op.f("ix_case_url_url"), table_name="case_url")
    op.drop_index(op.f("ix_case_url_case_id"), table_name="case_url")
    op.drop_table("case_url")

    op.create_table(
        "case_court",
        sa.Column("case_id", sa.BigInteger(), nullable=False),
        sa.Column("court_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["case.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["court_id"], ["court.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("case_id", "court_id"),
    )
    op.execute('INSERT INTO case_court (case_id, court_id) SELECT id, court_id FROM "case"')
    op.drop_index(op.f("ix_case_court_id"), table_name="case")
    op.drop_constraint("case_court_id_fkey", "case", type_="foreignkey")
    op.drop_column("case", "court_id")
