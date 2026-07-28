# Доступ к данным (Repository pattern): вся работа с БД собрана здесь.
from app.repositories.cases import CaseRepository
from app.repositories.courts import CourtRepository
from app.repositories.events import EventRepository
from app.repositories.judges import JudgeRepository
from app.repositories.search_tasks import SearchTaskRepository
from app.repositories.sides import SideRepository

__all__ = [
    "CaseRepository",
    "CourtRepository",
    "EventRepository",
    "JudgeRepository",
    "SearchTaskRepository",
    "SideRepository",
]
