"""Доступ к сторонам (Side) в БД."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.database import Side, SideType

# Метка роли с портала -> тип стороны. Всё, чего нет в словаре, считаем «Другое»
# (напр. «Привлекаемое лицо», «Заявитель», «Взыскатель», «Должник»).
_ROLE_TO_TYPE = {
    "Истец": SideType.PLAINTIFF,
    "Ответчик": SideType.DEFENDANT,
}


class SideRepository:
    """Чтение и запись сторон-справочника. Работает в рамках переданной сессии."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_or_create(self, full_name: str, type_: SideType) -> Side:
        """Найти сторону по паре (ФИО, тип) или создать новую.

        Дедуп по (full_name, type): одно и то же лицо в разных ролях (напр. истец
        по одному делу и ответчик по другому) — это разные записи справочника.
        Уникального ограничения в БД нет — берём первую совпавшую.
        """
        side = self._session.scalar(
            select(Side).where(Side.full_name == full_name, Side.type == type_)
        )
        if side is None:
            side = Side(full_name=full_name, type=type_)
            self._session.add(side)
            self._session.flush()  # чтобы получить side.id ещё до commit
        return side

    def get_or_create_many(self, sides: list[dict]) -> list[Side]:
        """get_or_create для списка {"role", "full_name"} из парсера (сохраняя порядок).

        Роль с портала сопоставляем с SideType: истец/ответчик — явно, всё остальное — «Другое».
        """
        result: list[Side] = []
        for item in sides:
            type_ = _ROLE_TO_TYPE.get(item["role"], SideType.OTHER)
            result.append(self.get_or_create(item["full_name"], type_))
        return result
