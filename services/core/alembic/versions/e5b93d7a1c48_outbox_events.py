"""outbox_event: поток доменных изменений вместо case.diff_history

Revision ID: e5b93d7a1c48
Revises: c4e8b7a21f60
Create Date: 2026-08-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e5b93d7a1c48'
down_revision: Union[str, Sequence[str], None] = 'c4e8b7a21f60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Значения перечисления в БД — имена членов OutboxEventType (так их пишет SQLAlchemy).
OUTBOX_EVENT_TYPES = (
    'CASE_FIELD_CHANGED',
    'EVENT_NEW', 'EVENT_UPDATED', 'EVENT_REMOVED',
    'PLACE_NEW', 'PLACE_UPDATED', 'PLACE_REMOVED',
    'SESSION_NEW', 'SESSION_UPDATED', 'SESSION_REMOVED',
    'DOCUMENT_NEW', 'DOCUMENT_REMOVED',
    'JUDGE_ADDED', 'JUDGE_REMOVED',
    'SIDE_ADDED', 'SIDE_REMOVED',
)


def upgrade() -> None:
    """Upgrade schema."""
    # Строка на каждое обнаруженное изменение по делу. Пишется в одной транзакции с самим
    # изменением карточки — событие не может потеряться и не может появиться без изменения.
    op.create_table(
        "outbox_event",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("case_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(*OUTBOX_EVENT_TYPES, name="outboxeventtype"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["case.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_outbox_event_case_id"), "outbox_event", ["case_id"], unique=False)
    op.create_index(op.f("ix_outbox_event_event_type"), "outbox_event", ["event_type"], unique=False)
    op.create_index(op.f("ix_outbox_event_created_at"), "outbox_event", ["created_at"], unique=False)
    # Под основной запрос клиента: события дела, случившиеся после момента подписки.
    op.create_index(
        "ix_outbox_event_case_created", "outbox_event", ["case_id", "created_at"], unique=False
    )

    # Старая история парсингов больше не нужна: журнал обходов заменён потоком событий,
    # а ключи S3-снапшотов — вместе с самой механикой снапшотов. Содержимое не переносим:
    # это был журнал работы парсера, а не доменные изменения.
    op.drop_column("case", "diff_history")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "case",
        sa.Column(
            "diff_history",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.drop_index("ix_outbox_event_case_created", table_name="outbox_event")
    op.drop_index(op.f("ix_outbox_event_created_at"), table_name="outbox_event")
    op.drop_index(op.f("ix_outbox_event_event_type"), table_name="outbox_event")
    op.drop_index(op.f("ix_outbox_event_case_id"), table_name="outbox_event")
    op.drop_table("outbox_event")
    sa.Enum(name="outboxeventtype").drop(op.get_bind())
