"""Доступ к данным: вся работа с БД собрана здесь.

Каждый репозиторий устроен одинаково: `XRepository(session)`, методы работают в рамках
переданной сессии и НИКОГДА не коммитят. Коммит делает только `session_scope`
(app/database.py) — иначе нельзя было бы записать изменения дела и события outbox одной
транзакцией, а это ключевое свойство.

Классов ровно столько, сколько сущностей: общего базового репозитория и Unit of Work
здесь нет и не будет. Между собой репозитории не импортируются ВОВСЕ — ни один не знает
про соседа. Наружу слой зависит от моделей, app/timezones.py, app/validators.py,
типов из app/parsers/parsed_case.py и (только captcha_solves) от app/captcha.

"""
from app.repositories.captcha_solves import CaptchaSolveRepository
from app.repositories.cases import CaseFieldChange, CaseRepository
from app.repositories.court_sessions import CourtSessionRepository
from app.repositories.courts import CourtRepository
from app.repositories.documents import DocumentRepository
from app.repositories.events import EventRepository
from app.repositories.judges import JudgeRepository
from app.repositories.outbox_events import OutboxEventRepository
from app.repositories.place_history import PlaceHistoryRepository
from app.repositories.proxies import ProxyRepository
from app.repositories.search_tasks import SearchTaskRepository
from app.repositories.sides import SideRepository

__all__ = [
    "CaptchaSolveRepository",
    "CaseFieldChange",
    "CaseRepository",
    "CourtRepository",
    "CourtSessionRepository",
    "DocumentRepository",
    "EventRepository",
    "JudgeRepository",
    "OutboxEventRepository",
    "PlaceHistoryRepository",
    "ProxyRepository",
    "SearchTaskRepository",
    "SideRepository",
]
