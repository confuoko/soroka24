# Доступ к данным (Repository pattern): вся работа с БД собрана здесь.
from app.repositories.cases import CaseRepository
from app.repositories.courts import CourtRepository
from app.repositories.judges import JudgeRepository
from app.repositories.search_tasks import SearchTaskRepository

__all__ = [
    "CaseRepository",
    "CourtRepository",
    "JudgeRepository",
    "SearchTaskRepository",
]
