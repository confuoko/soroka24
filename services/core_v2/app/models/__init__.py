"""ORM-модели домена «дела и суды».

Импортировать отсюда: `from app.models import Case, Event`. Так не приходится помнить,
в каком из модулей лежит нужная модель, а сами модули остаются небольшими.

Этот файл ещё и обязателен для работы: он импортирует ВСЕ модули моделей, а значит
регистрирует все таблицы в `Base.metadata`. Без него alembic увидел бы пустую схему и
предложил удалить всё, а строковые ссылки в relationship («Mapped[list["Event"]]») не
разрешились бы. По той же причине его импортирует alembic/env.py.

Порядок импортов ниже — снизу вверх по зависимостям, и это не косметика: `case.py`
обращается к таблицам связей из `people.py`, а дети и задачи ссылаются на `Case`.

Подключение к БД (engine, сессии, Base) лежит НЕ здесь, а в app/database.py: в старом
core движок и модели жили в одном файле на 857 строк, и было не видно, где кончается
«как мы говорим с базой» и начинается «что мы в ней храним».
"""
from app.models.enums import CourtLevel, OutboxEventType, SearchStatus, SideType
from app.models.court import Court
from app.models.people import Judge, Side, case_judge, case_side
from app.models.case import Case, CaseUrl
from app.models.case_children import CourtSession, Document, Event, PlaceHistory
from app.models.jobs import SearchTask
from app.models.fetching import CaptchaSolve, Proxy
from app.models.outbox import OutboxEvent
from app.models.integration_outbox import IntegrationOutboxEvent

__all__ = [
    # перечисления
    "CourtLevel",
    "OutboxEventType",
    "SearchStatus",
    "SideType",
    # справочники
    "Court",
    "Judge",
    "Side",
    # карточка дела и её адреса
    "Case",
    "CaseUrl",
    # строки внутри карточки
    "CourtSession",
    "Document",
    "Event",
    "PlaceHistory",
    # таблицы связей «дело ↔ судья» и «дело ↔ сторона»
    "case_judge",
    "case_side",
    # работа
    "SearchTask",
    # инфраструктура похода на портал
    "CaptchaSolve",
    "Proxy",
    # зафиксированные изменения: домен-лог и очередь на публикацию наружу
    "OutboxEvent",
    "IntegrationOutboxEvent",
]
