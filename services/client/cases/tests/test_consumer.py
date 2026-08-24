"""Разбор сообщений из case_changes: что принимаем, что выбрасываем.

Живой RabbitMQ здесь не нужен: транспорт (соединение, ack, сигналы) живёт в
management-команде, а проверять надо решение — что считать мусором и почему.

Главная мысль всего модуля, и её стоит держать в голове, читая тесты: **выбросить мусор
безопаснее, чем вернуть его в очередь.** `nack(requeue=True)` на непонятном сообщении
вернёт его нам же, мы снова не поймём — и так навсегда, забив очередь одним битым
сообщением и заблокировав все остальные изменения. Поэтому Malformed — это ack.
"""
import json

import pytest
from django.test import override_settings

from cases.consumer import CaseChange, Malformed, Outcome, handle, parse

# handle() теперь раскладывает изменение по подписчикам, то есть трогает базу. Тесты
# разбора при этом остаются про разбор: подписок здесь нет ни у одного, поэтому раскладка
# честно находит ноль подписчиков и ничего не пишет.
pytestmark = pytest.mark.django_db

GOOD = {
    "id": 1502,
    "type": "event_new",
    "version": 1,
    "case_id": 481,
    "entity_id": 712,
    "occurred_at": "2026-08-23T13:20:00+00:00",
}


def body(payload: dict | str) -> bytes:
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


# ------------------------------------------------------------- нормальный путь
def test_valid_message_is_parsed() -> None:
    change = parse(body(GOOD))

    assert isinstance(change, CaseChange)
    assert change.id == 1502
    assert change.type == "event_new"
    assert change.case_id == 481
    assert change.entity_id == 712
    assert change.occurred_at.isoformat() == "2026-08-23T13:20:00+00:00"


def test_valid_message_is_processed() -> None:
    assert handle(body(GOOD)) is Outcome.PROCESSED


def test_missing_entity_id_is_fine() -> None:
    """У case_field_changed сущности нет — это законное сообщение, а не мусор.

    Изменилось скалярное поле самой карточки, и её id уже есть в case_id.
    """
    payload = {**GOOD, "type": "case_field_changed", "entity_id": None}

    change = parse(body(payload))

    assert change.entity_id is None
    assert handle(body(payload)) is Outcome.PROCESSED


def test_unknown_event_type_is_still_accepted() -> None:
    """Незнакомый ТИП изменения не мусор: core публикует все 16, и добавит новые.

    Решать, что с ним делать, — наша забота, но отвергать сообщение из-за незнакомого
    типа значило бы требовать деплоя клиента на каждую правку словаря в core.
    """
    payload = {**GOOD, "type": "совсем_новый_тип"}

    assert handle(body(payload)) is Outcome.PROCESSED


def test_all_core_event_types_are_accepted() -> None:
    """Все 16 типов из core разбираются. Список публикуемого фильтром не сужен."""
    types = [
        "case_field_changed",
        "event_new", "event_updated", "event_removed",
        "place_new", "place_updated", "place_removed",
        "session_new", "session_updated", "session_removed",
        "document_new", "document_removed",
        "judge_added", "judge_removed",
        "side_added", "side_removed",
    ]

    for event_type in types:
        assert handle(body({**GOOD, "type": event_type})) is Outcome.PROCESSED


# ------------------------------------------------------------------- мусор
def test_not_json_is_malformed() -> None:
    assert handle(b"<html>502 Bad Gateway</html>") is Outcome.MALFORMED


def test_json_but_not_an_object_is_malformed() -> None:
    assert handle(body("[1, 2, 3]")) is Outcome.MALFORMED


@pytest.mark.parametrize("field", ["id", "type", "case_id", "version", "occurred_at"])
def test_missing_required_field_is_malformed(field) -> None:
    payload = {key: value for key, value in GOOD.items() if key != field}

    with pytest.raises(Malformed, match="обязательных"):
        parse(body(payload))

    assert handle(body(payload)) is Outcome.MALFORMED


def test_non_numeric_ids_are_malformed() -> None:
    """case_id строкой — не «почти правильно», а мусор.

    Принять такое молча значило бы записать в свою базу что-то, с чем потом разбираться,
    когда источник уже забыт.
    """
    assert handle(body({**GOOD, "case_id": "четыреста восемьдесят один"})) is Outcome.MALFORMED
    assert handle(body({**GOOD, "id": None})) is Outcome.MALFORMED


def test_empty_type_is_malformed() -> None:
    assert handle(body({**GOOD, "type": ""})) is Outcome.MALFORMED
    assert handle(body({**GOOD, "type": 7})) is Outcome.MALFORMED


def test_unparsable_occurred_at_is_malformed() -> None:
    assert handle(body({**GOOD, "occurred_at": "вчера"})) is Outcome.MALFORMED


def test_naive_occurred_at_is_malformed() -> None:
    """Момент без смещения отвергаем, а не приписываем ему пояс.

    У core он всегда со смещением. Его отсутствие означает, что сообщение собрал кто-то
    другой, и выдумывать за него пояс — верный способ разъехаться на часы.
    """
    assert handle(body({**GOOD, "occurred_at": "2026-08-23T13:20:00"})) is Outcome.MALFORMED


# ---------------------------------------------------------------- версия контракта
def test_unknown_version_is_refused() -> None:
    """Незнакомая версия отвергается явно, а не читается наугад.

    Практический вывод: при несовместимой правке контракта клиента деплоят ПЕРВЫМ, иначе
    сообщения новой версии будут выброшены. Ради этого version в контракте и есть.
    """
    with pytest.raises(Malformed, match="версия"):
        parse(body({**GOOD, "version": 2}))

    assert handle(body({**GOOD, "version": 2})) is Outcome.MALFORMED


@override_settings(INTEGRATION_EVENT_VERSION=2)
def test_the_understood_version_is_configurable() -> None:
    """Версию, которую мы понимаем, задаёт настройка, а не константа в коде."""
    assert handle(body({**GOOD, "version": 2})) is Outcome.PROCESSED
    assert handle(body({**GOOD, "version": 1})) is Outcome.MALFORMED


# ------------------------------------------------------- намерение исходов
def test_malformed_means_ack_not_requeue() -> None:
    """Мусор помечен так, чтобы команда его ПОДТВЕРДИЛА, а не вернула в очередь.

    Тест на намерение, а не на код: значений всего три, и цена путаницы между MALFORMED и
    RETRY — заблокированная очередь. Битое сообщение возвращалось бы вечно, а за ним
    встали бы все настоящие изменения.
    """
    assert Outcome.MALFORMED is not Outcome.RETRY
    # handle никогда не просит переспросить по своей воле: RETRY — это про инфраструктуру
    # (упала база), и его выставит Phase 6, а не разбор сообщения.
    for payload in ("мусор".encode("utf-8"), body("[]"), body({**GOOD, "version": 99})):
        assert handle(payload) is not Outcome.RETRY


def test_utf8_in_the_message_survives() -> None:
    """Русский текст в сообщении не ломает разбор.

    Сейчас текстовых полей в контракте нет, но лог печатает тело при отказе — и падение
    на кодировке в обработчике ОШИБКИ было бы особенно обидным.
    """
    assert handle('{"плохой": "json"'.encode("utf-8")) is Outcome.MALFORMED


# ------------------------------------------------------- адрес брокера
def test_celery_style_vhost_is_normalised_for_pika() -> None:
    """Адрес с `//` на конце приводится к vhost `/`, а не к пустому.

    Тонкость, на которой легко потерять вечер. В .env адрес один на всех и записан в
    привычной для Celery форме `amqp://user:pass@host:5672//`: kombu (то есть core) читает
    двойной слэш как стандартный vhost `/`, а pika — как vhost с ПУСТЫМ именем и падает
    с `NOT_ALLOWED - vhost  not found`.

    Проверка стоит здесь, а не в голове: правка адреса в .env иначе ломала бы одну из
    сторон, и падало бы это не там, где правили.
    """
    from cases.management.commands.consume_case_events import broker_parameters

    assert broker_parameters("amqp://u:p@host:5672//").virtual_host == "/"


def test_explicit_vhost_is_left_alone() -> None:
    """Заданный явно vhost не трогаем — нормализация касается только формы с `//`."""
    from cases.management.commands.consume_case_events import broker_parameters

    assert broker_parameters("amqp://u:p@host:5672/soroka").virtual_host == "soroka"
    assert broker_parameters("amqp://u:p@host:5672/%2F").virtual_host == "/"
