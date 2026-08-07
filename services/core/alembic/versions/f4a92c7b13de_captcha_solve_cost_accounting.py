"""captcha_solve: учёт стоимости разгаданных капч

Revision ID: f4a92c7b13de
Revises: e5b71c9a4d28
Create Date: 2026-08-06 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a92c7b13de'
down_revision: Union[str, Sequence[str], None] = 'e5b71c9a4d28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Стоимость каждой разгадки приходит от сервиса вместе с ответом и больше нигде не
    # хранится: тарифа у него нет, цена плавает от нагрузки. Поэтому пишем её строкой
    # на капчу — иначе посчитать, сколько стоило дело, будет уже нечем.
    op.create_table(
        "captcha_solve",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_task_id", sa.BigInteger(), nullable=False),
        sa.Column("search_task_id", sa.BigInteger(), nullable=True),
        sa.Column("case_id", sa.BigInteger(), nullable=True),
        sa.Column("court_id", sa.BigInteger(), nullable=True),
        sa.Column("host", sa.String(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        # NULL = цена неизвестна (не дождались ответа), а не ноль.
        sa.Column("cost", sa.Numeric(precision=10, scale=5), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("solve_count", sa.Integer(), nullable=True),
        sa.Column("attempt_no", sa.Integer(), nullable=True),
        sa.Column("celery_retry", sa.Integer(), nullable=True),
        sa.Column("captcha_bucket", sa.String(), nullable=True),
        sa.Column("captcha_key", sa.String(), nullable=True),
        sa.Column("requested_at", sa.DateTime(), nullable=True),
        sa.Column("solved_at", sa.DateTime(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # SET NULL на обоих ключах: удаление дела или задачи не должно стирать расход —
        # деньги-то потрачены.
        sa.ForeignKeyConstraint(["search_task_id"], ["search_task.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["case_id"], ["case.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["court_id"], ["court.id"]),
        # id задачи у сервиса уникален — повторная запись того же решения (ретрай
        # воркера, двойной вызов колбэка) не удвоит расход в отчёте.
        sa.UniqueConstraint(
            "provider", "provider_task_id", name="uq_captcha_solve_provider_task"
        ),
    )
    # Индексы под отчёты: по делу, по задаче и по периоду.
    op.create_index(op.f("ix_captcha_solve_case_id"), "captcha_solve", ["case_id"], unique=False)
    op.create_index(
        op.f("ix_captcha_solve_search_task_id"), "captcha_solve", ["search_task_id"], unique=False
    )
    op.create_index(op.f("ix_captcha_solve_court_id"), "captcha_solve", ["court_id"], unique=False)
    op.create_index(
        op.f("ix_captcha_solve_created_at"), "captcha_solve", ["created_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_captcha_solve_created_at"), table_name="captcha_solve")
    op.drop_index(op.f("ix_captcha_solve_court_id"), table_name="captcha_solve")
    op.drop_index(op.f("ix_captcha_solve_search_task_id"), table_name="captcha_solve")
    op.drop_index(op.f("ix_captcha_solve_case_id"), table_name="captcha_solve")
    op.drop_table("captcha_solve")
