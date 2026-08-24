"""Хендлер: domain event → integration event.

    CaseChanges → changes_to_events → DomainEvent → to_integration_events → IntegrationEvent

Одна функция и одна таблица соответствий. Ни фабрик, ни mapper-фреймворка: преобразование
здесь настолько простое, что любая абстракция над ним будет длиннее самого преобразования.

## Зачем вообще преобразовывать, если поля совпадают

Сейчас публичное имя типа буква в букву совпадает с внутренним (`event_new` и там и там),
и соблазн взять DomainEvent как контракт очень велик. Брать нельзя, и вот почему.

DomainEvent — внутренняя структура: она меняется вместе со сверкой. Появится у заседаний
новое изменяемое поле — поменяется payload; разъедутся ветки updated — поменяется набор
типов. Всё это правки внутри core, и они НЕ должны требовать деплоя клиентского сервиса.

Таблица INTEGRATION_TYPE_BY_DOMAIN и есть та самая развязка, только сделанная руками, а не
фреймворком: пока в ней есть строка, внутреннее имя можно переименовать, и наружу это не
просочится. Полноту таблицы проверяет тест — забыть новый тип не получится молча.

## Что уезжает наружу

Минимум, которого хватает, чтобы сказать «по делу 481 появилось событие 712»: тип, id
дела, id сущности, момент. Полные судебные данные клиент берёт по HTTP.

Не уезжает: payload сверки, SQLAlchemy-объекты, парсер, карточка дела целиком.
"""
from dataclasses import dataclass
from typing import Optional

from app.models import OutboxEventType
from app.outbox import DomainEvent

# Версия контракта сообщения. Поднимать при НЕсовместимом изменении формата — тогда
# клиент увидит незнакомую версию и откажется разбирать, вместо того чтобы молча прочитать
# поля неправильно. Добавление необязательного поля версию не меняет.
INTEGRATION_EVENT_VERSION = 1

# Внутренний тип изменения → публичное имя в сообщении.
#
# Перечислено явно и целиком, а не собрано автоматически из OutboxEventType. Именно это и
# есть развязка: автоматическое соответствие протащило бы любое переименование внутреннего
# enum прямо в контракт, и клиент сломался бы на правке, которой у него нет причин
# замечать.
#
# Публикуются ВСЕ типы. Что из этого достойно показа пользователю и что из этого повод
# написать письмо, решает клиентский сервис: он знает подписки, интерфейс и настройки
# уведомлений, а core не знает ничего из этого. Фильтр здесь означал бы, что новый тип в
# UI требует деплоя core.
INTEGRATION_TYPE_BY_DOMAIN: dict[OutboxEventType, str] = {
    OutboxEventType.CASE_FIELD_CHANGED: "case_field_changed",
    OutboxEventType.EVENT_NEW: "event_new",
    OutboxEventType.EVENT_UPDATED: "event_updated",
    OutboxEventType.EVENT_REMOVED: "event_removed",
    OutboxEventType.PLACE_NEW: "place_new",
    OutboxEventType.PLACE_UPDATED: "place_updated",
    OutboxEventType.PLACE_REMOVED: "place_removed",
    OutboxEventType.SESSION_NEW: "session_new",
    OutboxEventType.SESSION_UPDATED: "session_updated",
    OutboxEventType.SESSION_REMOVED: "session_removed",
    OutboxEventType.DOCUMENT_NEW: "document_new",
    OutboxEventType.DOCUMENT_REMOVED: "document_removed",
    OutboxEventType.JUDGE_ADDED: "judge_added",
    OutboxEventType.JUDGE_REMOVED: "judge_removed",
    OutboxEventType.SIDE_ADDED: "side_added",
    OutboxEventType.SIDE_REMOVED: "side_removed",
}


@dataclass(frozen=True)
class IntegrationEvent:
    """Изменение в том виде, в каком его увидит клиентский сервис.

    Ни ORM-объектов, ни payload сверки: только то, чего хватает, чтобы понять, что
    случилось, и при необходимости прийти за подробностями по HTTP.

    id здесь нет: он появится при записи в БД и станет идентификатором сообщения. До
    записи его не существует, и придумывать заранее нечего.
    """

    event_type: str
    case_id: int
    entity_id: Optional[int]
    version: int = INTEGRATION_EVENT_VERSION


def to_integration_events(
    case_id: int, events: list[DomainEvent]
) -> list[IntegrationEvent]:
    """Превратить domain events в публичные integration events.

    ВЫЗЫВАТЬ ПОСЛЕ OutboxEventRepository.emit, и это не вопрос стиля. У только что
    созданных Event/CourtSession/Document id появляется лишь при flush, а флашит именно
    emit. Позовёте раньше — ошибки не будет, просто у всех новых сообщений entity_id
    окажется пустым, и клиент не сможет сказать, ЧТО именно появилось.

    У удалённых строк (ветки *_removed) id читается нормально: сессия живёт с
    expire_on_commit=False, и атрибуты удалённого объекта остаются доступны.

    Неизвестный тип — исключение, а не пропуск. Молча выброшенное сообщение означало бы,
    что пользователь не увидел изменения, о котором мы знали; падение на обходе заметят
    сразу.
    """
    integration_events: list[IntegrationEvent] = []

    for event in events:
        try:
            event_type = INTEGRATION_TYPE_BY_DOMAIN[event.event_type]
        except KeyError:
            raise KeyError(
                f"тип изменения {event.event_type!r} не описан в "
                "INTEGRATION_TYPE_BY_DOMAIN — добавьте его публичное имя"
            ) from None

        integration_events.append(
            IntegrationEvent(
                event_type=event_type,
                case_id=case_id,
                entity_id=getattr(event.entity, "id", None),
            )
        )

    return integration_events
