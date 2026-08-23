"""Доступ к судьям (Judge) в БД."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Judge


class JudgeRepository:
    """Чтение и запись судей. Работает в рамках переданной сессии."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_or_create(self, full_name: str) -> Judge:
        """Найти судью по ФИО или создать нового.

        Дедуп глобальный по строке full_name (на портале это фамилия + инициалы,
        напр. «Каурова Д.С.»). Уникального ограничения в БД нет — берём первого совпавшего.
        """
        judge = self._session.scalar(
            select(Judge).where(Judge.full_name == full_name)
        )
        if judge is None:
            judge = Judge(full_name=full_name)
            self._session.add(judge)
            self._session.flush()  # чтобы получить judge.id ещё до commit
        return judge

    def get_or_create_many(self, full_names: list[str]) -> list[Judge]:
        """get_or_create для списка ФИО (сохраняя порядок)."""
        return [self.get_or_create(name) for name in full_names]
