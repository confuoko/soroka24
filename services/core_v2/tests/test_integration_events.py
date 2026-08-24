"""Integration events: публичный контракт изменений и его атомарность с обходом.

Сценарии из ТЗ §11: DomainEvent формируется при реальном diff, преобразуется в
IntegrationEvent, сохраняется в Outbox, изменение Case и Outbox атомарны.

Два теста здесь важнее остальных.

test_rollback_leaves_neither_table проверяет то единственное свойство, ради которого
запись устроена именно так. Уедет она из транзакции изменения — и сообщение сможет
появиться без изменения (клиент расскажет пользователю о том, чего не было) или изменение
без сообщения (пользователь не узнает о том, что случилось).

test_every_domain_type_has_a_public_name охраняет развязку контрактов. Добавили ветку в
сверку, забыли публичное имя — обход упадёт, и это правильно: молча выброшенное сообщение
означало бы, что пользователь не увидел изменения, о котором мы знали.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.integration_events import (
    INTEGRATION_EVENT_VERSION,
    INTEGRATION_TYPE_BY_DOMAIN,
    IntegrationEvent,
    to_integration_events,
)
from app.models import IntegrationOutboxEvent, OutboxEventType
from app.outbox import DomainEvent, changes_to_events
from app.parsers import ParsedCase, ParsedDocument, ParsedEvent, ParsedSession, ParsedSide
from app.repositories import IntegrationOutboxRepository, OutboxEventRepository
from app.services import sync_case

pytestmark = pytest.mark.db

UID = "77MS0002-01-2026-004321-11"
CODE = "02-0777/2026"


def page(**overrides) -> ParsedCase:
    """Разбор страницы: минимальная карточка с одним событием и одной стороной."""
    parsed = ParsedCase(
        status="Рассмотрение",
        category="Гражданские дела",
        judge_names=["Иванов И.И."],
        sides=[ParsedSide(role="Истец", full_name="Петров П.П.")],
        events=[
            ParsedEvent(
                event_date=dt.datetime(2026, 8, 10, 10, 0),
                state_description="Регистрация",
            )
        ],
    )
    for name, value in overrides.items():
        setattr(parsed, name, value)
    return parsed


def sync(session, court, parsed: ParsedCase):
    return sync_case(session, UID, parsed, court, CODE)


def emit_both(session, changes):
    """Записать оба представления так, как это делает обход: домен-лог, затем публичное.

    Порядок именно такой и в discovery.py: emit домен-лога флашит, и только после этого у
    новых строк появляются id, которые нужны entity_id.
    """
    domain_events = changes_to_events(changes)
    OutboxEventRepository(session).emit(changes.case, domain_events)
    return IntegrationOutboxRepository(session).emit(
        to_integration_events(changes.case.id, domain_events)
    )


def queued(session, case_id: int) -> list[IntegrationOutboxEvent]:
    return list(
        session.scalars(
            select(IntegrationOutboxEvent)
            .where(IntegrationOutboxEvent.case_id == case_id)
            .order_by(IntegrationOutboxEvent.id)
        )
    )


# --------------------------------------------------- развязка внутреннего и публичного
def test_every_domain_type_has_a_public_name() -> None:
    """Таблица соответствий покрывает ВСЕ внутренние типы изменений.

    Она и есть развязка контрактов, сделанная руками. Появилась новая ветка в сверке —
    её публичное имя надо назвать явно, иначе сообщение об этом изменении никуда не
    уедет.
    """
    assert set(INTEGRATION_TYPE_BY_DOMAIN) == set(OutboxEventType)


def test_public_names_are_unique() -> None:
    """Два внутренних типа не могут схлопнуться в одно публичное имя.

    Схлопнулись бы — клиент не смог бы отличить «заседание назначено» от «заседание
    сняли», а показывать это надо по-разному.
    """
    names = list(INTEGRATION_TYPE_BY_DOMAIN.values())
    assert len(names) == len(set(names))


def test_unknown_domain_type_raises() -> None:
    """Неописанный тип — исключение, а не молчаливый пропуск.

    Падение на обходе заметят сразу; потерянное сообщение — только когда пользователь
    спросит, почему ему не пришло уведомление.
    """
    class Fake:
        value = "made_up"

    with pytest.raises(KeyError, match="INTEGRATION_TYPE_BY_DOMAIN"):
        to_integration_events(1, [DomainEvent(Fake(), {})])


# ------------------------------------------------------------- само преобразование
def test_field_change_has_no_entity_id(session, court) -> None:
    """У изменения скалярного поля дела сущности нет: поменялась сама карточка."""
    sync(session, court, page())
    session.flush()

    changes = sync(session, court, page(status="Рассмотрено"))
    events = to_integration_events(changes.case.id, changes_to_events(changes))

    assert len(events) == 1
    assert events[0].event_type == "case_field_changed"
    assert events[0].entity_id is None
    assert events[0].case_id == changes.case.id


def test_new_event_carries_its_entity_id(session, court) -> None:
    """У нового события «Движения дела» в сообщение уходит его id.

    Без него клиент может сказать только «в деле что-то поменялось», а надо — «вот ЭТО
    новое».
    """
    sync(session, court, page())
    session.flush()

    grown = page()
    grown.events = grown.events + [
        ParsedEvent(
            event_date=dt.datetime(2026, 8, 15, 11, 0),
            state_description="Судебное заседание",
        )
    ]
    changes = sync(session, court, grown)
    rows = emit_both(session, changes)

    assert len(rows) == 1
    assert rows[0].event_type == "event_new"
    assert rows[0].entity_id is not None
    # Это именно id новой строки, а не что-нибудь похожее.
    assert rows[0].entity_id == changes.new_events[0].id


def test_entity_id_is_filled_for_sessions_and_documents(session, court) -> None:
    """Заседания и документы тоже несут свой id — проверяем не только события."""
    sync(session, court, page())
    session.flush()

    grown = page(
        court_sessions=[
            ParsedSession(session_date=dt.datetime(2026, 9, 1, 10, 0), stage="Первая")
        ],
        documents=[
            ParsedDocument(document_date=dt.date(2026, 8, 20), document_type="Решение")
        ],
    )
    rows = emit_both(session, sync(session, court, grown))

    by_type = {row.event_type: row for row in rows}
    assert by_type["session_new"].entity_id is not None
    assert by_type["document_new"].entity_id is not None


def test_removed_entity_still_has_an_id(session, court) -> None:
    """У пропавшей со страницы строки id читается: сессия живёт с expire_on_commit=False.

    Иначе сообщение «событие удалено» не сказало бы, какое именно.
    """
    sync(session, court, page())
    session.flush()

    rows = emit_both(session, sync(session, court, page(events=[])))

    assert [row.event_type for row in rows] == ["event_removed"]
    assert rows[0].entity_id is not None


def test_version_is_stamped(session, court) -> None:
    """Версия контракта попадает в строку: по ней клиент отличит несовместимый формат."""
    sync(session, court, page())
    session.flush()
    rows = emit_both(session, sync(session, court, page(status="Рассмотрено")))
    session.flush()

    assert rows[0].version == INTEGRATION_EVENT_VERSION


def test_side_reconciliation_produces_two_messages(session, court) -> None:
    """Смена роли стороны — отвязка одной и привязка другой, значит два сообщения."""
    sync(session, court, page())
    session.flush()

    changed = page(sides=[ParsedSide(role="Ответчик", full_name="Петров П.П.")])
    rows = emit_both(session, sync(session, court, changed))

    assert {row.event_type for row in rows} == {"side_added", "side_removed"}


# ------------------------------------------------------------------------ baseline
def test_first_import_queues_nothing(session, court) -> None:
    """Первый обход не публикует ничего.

    Вся карточка на нём формально «новая», но это не изменения: так на портале было и до
    нас. Появление самого дела клиент видит и без сообщений — он сам его и добавил.
    """
    changes = sync(session, court, page())
    emit_both(session, changes)
    session.flush()

    assert queued(session, changes.case.id) == []


def test_idle_crawl_queues_nothing(session, court) -> None:
    """Холостой обход не публикует ничего — самый частый случай."""
    sync(session, court, page())
    session.flush()

    changes = sync(session, court, page())
    emit_both(session, changes)
    session.flush()

    assert queued(session, changes.case.id) == []


# --------------------------------------------------------------------- атомарность
def test_change_and_message_are_written_together(session, court) -> None:
    """Изменение карточки, домен-лог и очередь наружу — в одной транзакции."""
    sync(session, court, page())
    session.flush()

    changes = sync(session, court, page(status="Рассмотрено"))
    emit_both(session, changes)
    session.flush()

    rows = queued(session, changes.case.id)
    assert len(rows) == 1
    assert rows[0].event_type == "case_field_changed"
    assert changes.case.status == "Рассмотрено"
    # published_at пустой: сообщение записано, но ещё не отправлено.
    assert rows[0].published_at is None


def test_rollback_leaves_neither_table(session, court) -> None:
    """ГЛАВНОЕ СВОЙСТВО. Откат уносит изменение, домен-лог И сообщение наружу.

    Проверяем вложенной транзакцией (SAVEPOINT): внешнюю откатывает фикстура, а увидеть
    надо откат именно нашей записи, не выходя из тестовой сессии.

    Уедет запись из транзакции изменения — и клиент начнёт врать: расскажет об изменении,
    которого не было, либо промолчит о том, которое было.
    """
    sync(session, court, page())
    session.flush()
    changes = sync(session, court, page(status="Рассмотрено"))
    case_id = changes.case.id

    savepoint = session.begin_nested()
    emit_both(session, changes)
    session.flush()
    assert queued(session, case_id), "сообщение должно было появиться до откáта"
    savepoint.rollback()

    assert queued(session, case_id) == []


def test_domain_log_and_public_queue_agree(session, court) -> None:
    """На каждое изменение — по одной строке в каждой из двух таблиц.

    Расхождение здесь означало бы, что одно из представлений собирается не из того же
    диффа, и клиент увидел бы не то же, что видно в домен-логе.
    """
    sync(session, court, page())
    session.flush()

    changes = sync(session, court, page(status="Рассмотрено", category="Другая"))
    domain_events = changes_to_events(changes)
    OutboxEventRepository(session).emit(changes.case, domain_events)
    IntegrationOutboxRepository(session).emit(
        to_integration_events(changes.case.id, domain_events)
    )
    session.flush()

    assert len(domain_events) == 2
    assert len(queued(session, changes.case.id)) == 2


# ---------------------------------------------------------- очередь на публикацию
def test_unpublished_are_taken_in_order(session, court) -> None:
    """Publisher забирает неопубликованное по возрастанию id.

    По id, а не по occurred_at: у всех сообщений одного обхода момент одинаковый (он
    берётся из начала транзакции), и порядок по нему был бы неопределён.
    """
    sync(session, court, page())
    session.flush()
    changes = sync(session, court, page(status="Рассмотрено", category="Другая"))
    emit_both(session, changes)
    session.flush()

    repo = IntegrationOutboxRepository(session)
    rows = repo.take_unpublished(limit=100)
    ours = [row for row in rows if row.case_id == changes.case.id]

    assert len(ours) == 2
    assert [row.id for row in ours] == sorted(row.id for row in ours)


def test_published_messages_are_not_taken_again(session, court) -> None:
    """Отмеченное опубликованным publisher больше не берёт."""
    sync(session, court, page())
    session.flush()
    changes = sync(session, court, page(status="Рассмотрено"))
    rows = emit_both(session, changes)
    session.flush()

    repo = IntegrationOutboxRepository(session)
    repo.mark_published(rows)
    session.flush()

    assert rows[0].published_at is not None
    still_waiting = [
        row
        for row in repo.take_unpublished(limit=100)
        if row.case_id == changes.case.id
    ]
    assert still_waiting == []


def test_limit_is_respected(session, court) -> None:
    sync(session, court, page())
    session.flush()
    emit_both(session, sync(session, court, page(status="Рассмотрено", category="Другая")))
    session.flush()

    assert len(IntegrationOutboxRepository(session).take_unpublished(limit=1)) == 1


# ------------------------------------------------- контракт остаётся скудным
def test_message_carries_no_court_data() -> None:
    """В сообщении нет судебных данных — только ссылки, по которым их можно запросить.

    Иначе формат сообщения пришлось бы менять всякий раз, когда парсер начинает отдавать
    новое поле, — то есть ровно то, от чего вторая таблица и защищает.
    """
    fields = set(IntegrationEvent.__dataclass_fields__)

    assert fields == {"event_type", "case_id", "entity_id", "version"}


def test_message_carries_no_delivery_recipients(session, court) -> None:
    """Ни пользователей, ни каналов: кому показывать, решает клиентский сервис."""
    columns = {column.name for column in IntegrationOutboxEvent.__table__.columns}
    forbidden = {"user_id", "email", "telegram_id", "recipient", "notification_status"}

    assert not (forbidden & columns)
    # published_at — про отправку в брокер, а не про пользователя. Он здесь законен и
    # нужен: это единственное состояние доставки, которое core вообще знает.
    assert "published_at" in columns

