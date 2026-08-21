"""часовые пояса: timestamptz везде, пояс суда, время у событий

Revision ID: d2f6a91c74be
Revises: b8e1f4a06d27
Create Date: 2026-08-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2f6a91c74be'
down_revision: Union[str, Sequence[str], None] = 'b8e1f4a06d27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Колонки-МОМЕНТЫ: всё, что писалось питоном через utcnow() или сервером через now().
# Значения там честный UTC, поэтому перевод в timestamptz — это только смена типа с
# пометкой «считать написанное UTC», данные не двигаются.
#
# Календарных дат (receipt_date, place_date, document_date, published_at…) здесь нет и
# быть не должно: у даты нет времени, и приписывать ей полночь значит выдумывать момент.
UTC_COLUMNS = [
    ("case", "created_at"), ("case", "updated_at"),
    ("case", "last_checked_at"), ("case", "last_changed_at"),
    ("case_url", "last_success_at"), ("case_url", "created_at"), ("case_url", "updated_at"),
    ("case_link", "created_at"),
    ("event", "created_at"), ("event", "updated_at"),
    ("place_history", "created_at"), ("place_history", "updated_at"),
    ("document", "created_at"), ("document", "updated_at"),
    ("court_session", "created_at"), ("court_session", "updated_at"),
    ("proxy", "last_used_at"), ("proxy", "created_at"), ("proxy", "updated_at"),
    ("search_task", "last_attempt_at"), ("search_task", "created_at"), ("search_task", "updated_at"),
    ("captcha_solve", "requested_at"), ("captcha_solve", "solved_at"), ("captcha_solve", "created_at"),
    ("outbox_event", "created_at"),
]


def _retype(table: str, column: str, to_tz: bool) -> None:
    """Сменить тип колонки между timestamp и timestamptz, не сдвигая значение.

    Обе стороны трактуют naive-значение как UTC: туда — «это было UTC», обратно —
    «покажи в UTC». Момент при этом не меняется.
    """
    if to_tz:
        using = f"{column} AT TIME ZONE 'UTC'"
        new_type = "TIMESTAMP WITH TIME ZONE"
    else:
        using = f"{column} AT TIME ZONE 'UTC'"
        new_type = "TIMESTAMP WITHOUT TIME ZONE"
    op.execute(f'ALTER TABLE "{table}" ALTER COLUMN {column} TYPE {new_type} USING {using}')


def _local_to_utc(table: str, column: str) -> None:
    """Перевести колонку из МЕСТНОГО времени суда в timestamptz.

    Значение писалось как есть со страницы суда, то есть местным временем, а какое оно —
    известно только через суд карточки. ALTER ... USING к другой таблице обратиться не
    может, поэтому идём через временную колонку и UPDATE с JOIN.
    """
    op.execute(f'ALTER TABLE "{table}" ADD COLUMN _tz_tmp TIMESTAMP WITH TIME ZONE')
    op.execute(
        f'UPDATE "{table}" t SET _tz_tmp = (t.{column}::timestamp AT TIME ZONE co.timezone) '
        f'FROM "case" c JOIN court co ON co.id = c.court_id WHERE c.id = t.case_id'
    )
    # Строк без суда быть не может (case_id NOT NULL, court_id NOT NULL), но если такая
    # найдётся — падаем на NOT NULL ниже, а не сохраняем молча NULL.
    op.execute(f'ALTER TABLE "{table}" DROP COLUMN {column}')
    op.execute(f'ALTER TABLE "{table}" RENAME COLUMN _tz_tmp TO {column}')
    op.execute(f'ALTER TABLE "{table}" ALTER COLUMN {column} SET NOT NULL')


def _utc_to_local(table: str, column: str, as_date: bool) -> None:
    """Обратное преобразование: timestamptz -> местное время суда (naive) или дата."""
    target = "DATE" if as_date else "TIMESTAMP WITHOUT TIME ZONE"
    op.execute(f'ALTER TABLE "{table}" ADD COLUMN _tz_tmp {target}')
    op.execute(
        f'UPDATE "{table}" t SET _tz_tmp = (t.{column} AT TIME ZONE co.timezone)'
        f'{"::date" if as_date else ""} '
        f'FROM "case" c JOIN court co ON co.id = c.court_id WHERE c.id = t.case_id'
    )
    op.execute(f'ALTER TABLE "{table}" DROP COLUMN {column}')
    op.execute(f'ALTER TABLE "{table}" RENAME COLUMN _tz_tmp TO {column}')
    op.execute(f'ALTER TABLE "{table}" ALTER COLUMN {column} SET NOT NULL')


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Пояс суда. Сначала nullable — заполнить его нечем, пока код не проставит значения.
    op.add_column('court', sa.Column('timezone', sa.String(), nullable=True))

    # Заполняем прямо здесь, а не отдельным скриптом: колонка NOT NULL, и оставлять базу
    # в промежуточном состоянии между миграцией и запуском скрипта нельзя.
    from app.timezones import timezone_for

    connection = op.get_bind()
    courts = connection.execute(sa.text("SELECT id, code, region FROM court")).fetchall()
    for court_id, code, region in courts:
        connection.execute(
            sa.text("UPDATE court SET timezone = :tz WHERE id = :id"),
            {"tz": timezone_for(region, code), "id": court_id},
        )
    op.alter_column('court', 'timezone', nullable=False)

    # 2. Серверные метки: смена типа без сдвига значений.
    for table, column in UTC_COLUMNS:
        _retype(table, column, to_tz=True)

    # 3. Время с портала. Оно местное для суда, поэтому переводится по его поясу.
    #    event.event_date был DATE — у событий без времени получится местная полночь.
    _local_to_utc("event", "event_date")
    _local_to_utc("court_session", "session_date")


def downgrade() -> None:
    """Downgrade schema."""
    # Время у событий при откате теряется: колонка снова становится DATE. Пояс суда тоже
    # уходит — вернуть его потом можно повторным прогоном миграции, карта живёт в коде.
    _utc_to_local("court_session", "session_date", as_date=False)
    _utc_to_local("event", "event_date", as_date=True)

    for table, column in reversed(UTC_COLUMNS):
        _retype(table, column, to_tz=False)

    op.drop_column('court', 'timezone')
