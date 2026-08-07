"""события/документы/заседания/местонахождения: uid считается от карточки, а не от УИД дела

Revision ID: c2d95f31e7a8
Revises: a1c4e08d6b52
Create Date: 2026-08-07 14:00:00.000000

"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2d95f31e7a8'
down_revision: Union[str, Sequence[str], None] = 'a1c4e08d6b52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Namespace'ы — копии из app/repositories/*.py. Дублируются намеренно: миграция обязана
# считать ровно то, что считал код на момент её написания, и не должна ехать вслед за
# будущими правками приложения.
EVENT_NS = uuid.UUID("af75dcd7-7083-4294-8e05-d5f643e533c3")
PLACE_NS = uuid.UUID("6b1f3c02-9a4d-5e77-b8c1-2f0a7d43e915")
SESSION_NS = uuid.UUID("9c4e7a10-2f83-5b6d-a1c7-4e0d9f5b3a26")
DOCUMENT_NS = uuid.UUID("2f7b91c4-6d3e-5a08-9c1f-7b45e0a2d836")


def _cards(connection) -> dict[int, tuple[str, str]]:
    """{id карточки: (УИД дела, ключ карточки)}.

    Ключ карточки — «УИД | код суда | номер дела», ровно как Case.card_key.
    """
    rows = connection.execute(
        sa.text(
            'SELECT c.id, c.uid, ct.code, c.code '
            'FROM "case" c JOIN court ct ON ct.id = c.court_id'
        )
    ).fetchall()
    return {row[0]: (row[1], f"{row[1]}|{row[2]}|{row[3]}") for row in rows}


def _renumber(connection, cards, table, namespace, columns, key_parts) -> int:
    """Пересчитать uid всех строк таблицы по новому ключу. Возвращает число строк.

    columns — поля identity, которые читаем; key_parts — как собрать из них хвост ключа.
    """
    selected = ", ".join(["id", "case_id", *columns])
    rows = connection.execute(
        sa.text(f"SELECT {selected} FROM {table} ORDER BY case_id, id")
    ).fetchall()

    updated = 0
    for row in rows:
        card = cards.get(row[1])
        if card is None:
            # Дела нет (строка-сирота) — пересчитывать не от чего, оставляем как есть.
            continue
        key = "|".join([card[1], *key_parts(row)])
        connection.execute(
            sa.text(f"UPDATE {table} SET uid = :uid WHERE id = :id"),
            {"uid": uuid.uuid5(namespace, key), "id": row[0]},
        )
        updated += 1
    return updated


def upgrade() -> None:
    """Upgrade schema."""
    connection = op.get_bind()

    # uid событий, документов, заседаний и местонахождений считался от УИД ДЕЛА. Пока
    # карточка была одна на УИД, это работало; теперь по одному УИД карточек бывает
    # несколько (разные суды, разные производства), а UNIQUE-индекс на uid глобальный —
    # одинаковые строки соседних карточек сталкивались бы, и вторая карточка не
    # сохранялась бы вовсе. Ключом стала карточка: «УИД | код суда | номер дела».
    #
    # Пересчитываем на месте, а не оставляем «поехать» само: иначе следующий обход увидел
    # бы все строки как удалённые и завёл заново — массовый ложный дифф в истории дела.
    cards = _cards(connection)

    events = _renumber(
        connection, cards, "event", EVENT_NS,
        ["event_date", "state_description"],
        lambda r: [r[2].isoformat(), r[3]],
    )
    places = _renumber(
        connection, cards, "place_history", PLACE_NS,
        ["place_date", "place_description"],
        lambda r: [r[2].isoformat(), r[3]],
    )
    sessions = _renumber(
        connection, cards, "court_session", SESSION_NS,
        ["session_date", "stage"],
        lambda r: [r[2].isoformat(), r[3]],
    )

    # У документов в ключ входит номер повторения — сколько строк с той же парой
    # (дата, вид) встретилось ВЫШЕ на странице. В БД он не хранится, поэтому
    # восстанавливаем его порядком строк: они вставлялись в порядке страницы, id растёт.
    document_rows = connection.execute(
        sa.text(
            "SELECT id, case_id, document_date, document_type "
            "FROM document ORDER BY case_id, id"
        )
    ).fetchall()
    seen: dict[tuple[int, str, str], int] = {}
    documents = 0
    for row in document_rows:
        card = cards.get(row[1])
        if card is None:
            continue
        group = (row[1], row[2].isoformat(), row[3])
        occurrence = seen.get(group, 0)
        seen[group] = occurrence + 1
        key = "|".join([card[1], row[2].isoformat(), row[3], str(occurrence)])
        connection.execute(
            sa.text("UPDATE document SET uid = :uid WHERE id = :id"),
            {"uid": uuid.uuid5(DOCUMENT_NS, key), "id": row[0]},
        )
        documents += 1

    print(
        f"uid пересчитаны: событий {events}, местонахождений {places}, "
        f"заседаний {sessions}, документов {documents}"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Обратный пересчёт — от УИД дела. Упадёт на UNIQUE-индексе, если к этому моменту по
    # одному УИД накопилось несколько карточек с одинаковыми строками: старая схема их
    # различить не умела, и это ровно та причина, по которой ключ и меняли.
    connection = op.get_bind()
    cards = {cid: (case_uid, case_uid) for cid, (case_uid, _key) in _cards(connection).items()}

    _renumber(
        connection, cards, "event", EVENT_NS,
        ["event_date", "state_description"],
        lambda r: [r[2].isoformat(), r[3]],
    )
    _renumber(
        connection, cards, "place_history", PLACE_NS,
        ["place_date", "place_description"],
        lambda r: [r[2].isoformat(), r[3]],
    )
    _renumber(
        connection, cards, "court_session", SESSION_NS,
        ["session_date", "stage"],
        lambda r: [r[2].isoformat(), r[3]],
    )

    document_rows = connection.execute(
        sa.text(
            "SELECT id, case_id, document_date, document_type "
            "FROM document ORDER BY case_id, id"
        )
    ).fetchall()
    seen: dict[tuple[int, str, str], int] = {}
    for row in document_rows:
        card = cards.get(row[1])
        if card is None:
            continue
        group = (row[1], row[2].isoformat(), row[3])
        occurrence = seen.get(group, 0)
        seen[group] = occurrence + 1
        key = "|".join([card[1], row[2].isoformat(), row[3], str(occurrence)])
        connection.execute(
            sa.text("UPDATE document SET uid = :uid WHERE id = :id"),
            {"uid": uuid.uuid5(DOCUMENT_NS, key), "id": row[0]},
        )
