# Доступ к данным (Repository pattern): вся работа с БД собрана здесь.
from app.repositories.cases import CaseFieldChange, CaseRepository
from app.repositories.court_sessions import CourtSessionRepository
from app.repositories.courts import CourtRepository
from app.repositories.documents import DocumentRepository
from app.repositories.events import EventRepository
from app.repositories.judges import JudgeRepository
from app.repositories.place_history import PlaceHistoryRepository
from app.repositories.proxies import ProxyRepository
from app.repositories.search_tasks import SearchTaskRepository
from app.repositories.sides import SideRepository

__all__ = [
    "CaseRepository",
    "CaseFieldChange",
    "CourtRepository",
    "CourtSessionRepository",
    "DocumentRepository",
    "EventRepository",
    "JudgeRepository",
    "PlaceHistoryRepository",
    "ProxyRepository",
    "SearchTaskRepository",
    "SideRepository",
]
