"""OutboxPublisher: неопубликованное уезжает, опубликованное не уезжает дважды.

Сценарии из ТЗ §11: unpublished Outbox публикуется, опубликованные сообщения корректно
помечаются.

Живой RabbitMQ здесь не нужен и не нужен намеренно: publish передаётся в publish_batch
аргументом, поэтому проверяется именно решение — что взять, что отметить и что делать с
половинчатым успехом, — а не работа kombu. Взамен есть тест на форму сообщения
(test_message_shape_is_the_contract): он охраняет ровно тот JSON, который увидит
подписчик.

Главный здесь test_failed_publish_keeps_the_prefix_marked. Он про свойство, которое легко
потерять рефакторингом: порция, упавшая на середине, обязана оставить помеченным свой
успешный префикс. Пробрось publish_batch исключение наверх — session_scope откатил бы
транзакцию вместе с отметками, и всё уже отправленное уехало бы повторно.

## Почему модуль требует остановленного publisher'а

Тесты здесь заводят настоящие, закоммиченные строки в integration_outbox_event и
утверждают, что порция взяла ровно их. Запущенный рядом core-v2-outbox-publisher опрашивает
ту же таблицу раз в секунду и разбирает эти строки первым — тест падал бы через раз, в
зависимости от того, кто успел.

Мириться с этим нельзя: флаки-тест хуже падающего, потому что его начинают перезапускать
вместо того, чтобы читать. Поэтому модуль сам проверяет, не работает ли publisher, и
честно пропускается с внятной причиной, а не падает загадочно.
"""
from __future__ import annotations

import datetime as dt
import json
import time

import pytest
from sqlalchemy import delete, select

from app import config
from app.database import session_scope
from app.integration_events import to_integration_events
from app.integration_publisher import Batch, message_of, publish_batch
from app.models import Case, Court, CourtLevel, IntegrationOutboxEvent
from app.outbox import changes_to_events
from app.parsers import ParsedCase, ParsedEvent, ParsedSide
from app.repositories import IntegrationOutboxRepository, OutboxEventRepository
from app.services import sync_case

pytestmark = pytest.mark.db

UID = "77MS0002-01-2026-077777-77"
COURT_CODE = "77MS0002"


@pytest.fixture(scope="module", autouse=True)
def no_live_publisher():
    """Пропустить модуль, если рядом работает настоящий publisher.

    Проверяем не догадкой, а опытом: кладём в таблицу пробную строку и смотрим, не
    опубликует ли её кто-то за нас. Один раз на модуль, ценой полутора секунд.

    Пробе нужно существующее дело: case_id — внешний ключ. Берём любое из базы; если дел
    нет вовсе, проверять нечего — publisher'у тоже было бы нечего публиковать.
    """
    with session_scope() as session:
        case_id = session.scalar(select(Case.id).limit(1))
        if case_id is None:
            return
        probe = IntegrationOutboxEvent(
            event_type="event_new", case_id=case_id, entity_id=None
        )
        session.add(probe)
        session.flush()
        probe_id = probe.id

    time.sleep(max(1.5, config.PUBLISHER_POLL_SECONDS * 1.5))

    with session_scope() as session:
        row = session.get(IntegrationOutboxEvent, probe_id)
        taken = row is not None and row.published_at is not None
        if row is not None:
            session.delete(row)

    if taken:
        pytest.skip(
            "рядом работает core-v2-outbox-publisher: он разбирает те же строки, и "
            "тесты порций стали бы флаки. Остановите его: "
            "docker compose stop core-v2-outbox-publisher"
        )


@pytest.fixture
def pending():
    """Дело с ДВУМЯ неопубликованными сообщениями и уборка за собой.

    Коммитим по-настоящему: publish_batch открывает свой session_scope, то есть другое
    соединение, и незакоммиченных строк не увидит вовсе.

    Чтобы порция не зачерпнула чужого, тест сначала осушает таблицу — в дев-базе могли
    остаться сообщения от предыдущих прогонов, и утверждения про «ровно два» иначе
    ломались бы от постороннего.
    """
    with session_scope() as session:
        session.execute(
            delete(IntegrationOutboxEvent).where(
                IntegrationOutboxEvent.published_at.is_(None)
            )
        )

    with session_scope() as session:
        court = session.scalar(select(Court).where(Court.code == COURT_CODE))
        if court is None:
            court = Court(
                code=COURT_CODE,
                name="Судебный участок № 2",
                level=CourtLevel.MIRSUD,
                region="Город Москва",
                timezone="Europe/Moscow",
                base_url="http://mos-sud.ru/ms/2",
            )
            session.add(court)
            session.flush()

        parsed = ParsedCase(
            status="Рассмотрение",
            judge_names=["Иванов И.И."],
            sides=[ParsedSide(role="Истец", full_name="Петров П.П.")],
            events=[
                ParsedEvent(
                    event_date=dt.datetime(2026, 8, 10, 10, 0),
                    state_description="Регистрация",
                )
            ],
        )
        # Первый обход — baseline, сообщений не даёт.
        case_id = sync_case(session, UID, parsed, court, "77-7777/2026").case.id

    with session_scope() as session:
        court = session.scalar(select(Court).where(Court.code == COURT_CODE))
        grown = ParsedCase(
            status="Рассмотрено",  # изменение поля -> case_field_changed
            judge_names=["Иванов И.И."],
            sides=[ParsedSide(role="Истец", full_name="Петров П.П.")],
            events=[
                ParsedEvent(
                    event_date=dt.datetime(2026, 8, 10, 10, 0),
                    state_description="Регистрация",
                ),
                ParsedEvent(  # новая строка -> event_new
                    event_date=dt.datetime(2026, 8, 15, 11, 0),
                    state_description="Судебное заседание",
                ),
            ],
        )
        changes = sync_case(session, UID, grown, court, "77-7777/2026")
        domain_events = changes_to_events(changes)
        OutboxEventRepository(session).emit(changes.case, domain_events)
        IntegrationOutboxRepository(session).emit(
            to_integration_events(changes.case.id, domain_events)
        )

    try:
        yield case_id
    finally:
        with session_scope() as session:
            session.execute(delete(Case).where(Case.id == case_id))


def ours(session, case_id: int) -> list[IntegrationOutboxEvent]:
    return list(
        session.scalars(
            select(IntegrationOutboxEvent)
            .where(IntegrationOutboxEvent.case_id == case_id)
            .order_by(IntegrationOutboxEvent.id)
        )
    )


# ------------------------------------------------------------- обычная работа
def test_pending_messages_are_published_and_marked(pending) -> None:
    """Неопубликованное уезжает и получает published_at."""
    sent: list[dict] = []

    batch = publish_batch(sent.append)

    assert batch.published == 2
    assert batch.error is None
    assert [message["type"] for message in sent] == ["case_field_changed", "event_new"]

    with session_scope() as session:
        rows = ours(session, pending)
        assert all(row.published_at is not None for row in rows)


def test_published_messages_are_not_published_again(pending) -> None:
    """Второй проход не отправляет то же повторно.

    Ровно то, ради чего published_at и существует: publisher опрашивает таблицу раз в
    секунду, и без отметки он рассылал бы всю историю изменений каждую секунду.
    """
    first: list[dict] = []
    publish_batch(first.append)

    second: list[dict] = []
    batch = publish_batch(second.append)

    assert len(first) == 2
    assert second == []
    assert batch.published == 0
    assert batch.taken == 0


def test_empty_table_publishes_nothing(pending) -> None:
    """Пустая очередь — пустая порция, без обращений к брокеру."""
    publish_batch(lambda _message: None)

    calls: list[dict] = []
    batch = publish_batch(calls.append)

    assert batch == Batch(published=0, taken=0, error=None)
    assert calls == []


def test_messages_go_in_id_order(pending) -> None:
    """Порядок публикации — порядок появления.

    По id, а не по occurred_at: у всех сообщений одного обхода момент одинаковый (он
    берётся из начала транзакции), и порядок по нему был бы неопределён. А порядок
    значим — «дело изменилось» до «появилось новое событие» читается иначе, чем наоборот.
    """
    sent: list[dict] = []
    publish_batch(sent.append)

    assert [message["id"] for message in sent] == sorted(m["id"] for m in sent)


# ------------------------------------------------------- частичный успех и отказы
def test_failed_publish_keeps_the_prefix_marked(pending) -> None:
    """ГЛАВНОЕ. Порция, упавшая на середине, помечает ровно свой успешный префикс.

    Пробрось publish_batch исключение наверх — session_scope откатил бы транзакцию вместе
    с отметками, и уже отправленное уехало бы повторно. Ошибка возвращается в Batch именно
    для этого.
    """
    sent: list[dict] = []

    def flaky(message: dict) -> None:
        if len(sent) == 1:
            raise RuntimeError("брокер отвалился")
        sent.append(message)

    batch = publish_batch(flaky)

    assert batch.published == 1
    assert isinstance(batch.error, RuntimeError)

    with session_scope() as session:
        rows = ours(session, pending)
        # Первое отмечено, второе — нет: оно и правда не уехало.
        assert rows[0].published_at is not None
        assert rows[1].published_at is None


def test_the_rest_goes_out_on_the_next_round(pending) -> None:
    """Недоотправленный остаток уезжает следующей порцией, и ровно один раз."""
    attempt: list[dict] = []

    def once_flaky(message: dict) -> None:
        if len(attempt) == 1:
            raise RuntimeError("брокер отвалился")
        attempt.append(message)

    publish_batch(once_flaky)

    rest: list[dict] = []
    batch = publish_batch(rest.append)

    assert batch.published == 1
    assert len(rest) == 1
    assert rest[0]["type"] == "event_new"

    with session_scope() as session:
        assert all(row.published_at is not None for row in ours(session, pending))


def test_nothing_is_lost_when_the_first_publish_fails(pending) -> None:
    """Упало на первом же сообщении — ничего не отмечено, всё уедет позже.

    Потеря здесь была бы худшим исходом из возможных: обход изменение нашёл, в таблице
    оно помечено отправленным, а до подписчика не дошло — и уже никогда не дойдёт.
    """
    def broken(_message: dict) -> None:
        raise RuntimeError("брокер недоступен")

    batch = publish_batch(broken)

    assert batch.published == 0
    assert batch.taken == 2

    with session_scope() as session:
        assert all(row.published_at is None for row in ours(session, pending))


# ------------------------------------------------------------------ порции
def test_full_batch_reports_there_is_more(pending) -> None:
    """Полная порция означает «в таблице есть ещё» — publisher не будет ждать."""
    sent: list[dict] = []
    batch = publish_batch(sent.append, limit=1)

    assert batch.taken == 1
    # limit совпал с размером порции из настроек? Тогда had_more должен сказать «есть ещё».
    assert batch.had_more is (1 >= config.PUBLISHER_BATCH_SIZE)

    remaining = publish_batch(sent.append)
    assert remaining.published == 1


# --------------------------------------------------------- форма сообщения
def test_message_shape_is_the_contract(pending) -> None:
    """Сообщение содержит ровно оговорённые поля и ничего кроме.

    Этот тест — единственное, что стоит между «добавил колонку в таблицу» и «сломал
    разбор у клиента». Лишнее поле само по себе безвредно, но отсутствие теста означало
    бы, что формат может уехать незаметно.
    """
    sent: list[dict] = []
    publish_batch(sent.append)

    assert set(sent[0]) == {
        "id",
        "type",
        "version",
        "case_id",
        "entity_id",
        "occurred_at",
    }


def test_message_is_json_serialisable(pending) -> None:
    """Сообщение уходит строкой JSON — значит ни datetime, ни UUID внутри быть не может.

    Проверяем настоящим json.dumps: именно на нём споткнулся бы забытый occurred_at.
    """
    sent: list[dict] = []
    publish_batch(sent.append)

    for message in sent:
        restored = json.loads(json.dumps(message, ensure_ascii=False))
        assert restored == message


def test_occurred_at_carries_an_offset(pending) -> None:
    """У момента есть смещение: без него подписчик не отличит Москву от UTC."""
    sent: list[dict] = []
    publish_batch(sent.append)

    moment = dt.datetime.fromisoformat(sent[0]["occurred_at"])
    assert moment.tzinfo is not None


def test_entity_id_survives_the_trip(pending) -> None:
    """id сущности доезжает до сообщения: без него клиент не скажет, ЧТО появилось."""
    sent: list[dict] = []
    publish_batch(sent.append)

    by_type = {message["type"]: message for message in sent}
    assert by_type["event_new"]["entity_id"] is not None
    # А у изменения поля дела его и не должно быть: поменялась сама карточка.
    assert by_type["case_field_changed"]["entity_id"] is None


def test_message_of_matches_the_row(pending) -> None:
    """message_of не выдумывает: каждое поле сообщения взято из своей колонки."""
    with session_scope() as session:
        row = ours(session, pending)[0]
        message = message_of(row)

        assert message["id"] == row.id
        assert message["type"] == row.event_type
        assert message["version"] == row.version
        assert message["case_id"] == row.case_id
        assert message["entity_id"] == row.entity_id
        assert message["occurred_at"] == row.occurred_at.isoformat()
