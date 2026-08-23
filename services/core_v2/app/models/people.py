"""Судьи и стороны + таблицы связей «дело ↔ судья» и «дело ↔ сторона».

Ни Judge, ни Side не знают про Case: обратной связи (back_populates) у них нет, и это
намеренно — справочник общий для многих дел, ходить от судьи ко всем его делам нам
незачем. Поэтому этот модуль ничего не импортирует из case.py, а case.py импортирует
его. Направление одностороннее, цикла не возникает.

Таблицы связей лежат здесь же: на них ссылается Case.judges/Case.sides через параметр
secondary, и это единственный настоящий импорт объектов между модулями моделей —
остальные связи объявлены строками имён и разрешаются реестром SQLAlchemy лениво.
"""
from sqlalchemy import BigInteger, Column, Enum, ForeignKey, String, Table
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.enums import SideType

# Таблицы-связки нужны для связи «многие-ко-многим»: у дела много судей/сторон, а каждый из них — во многих делах.
# ondelete="CASCADE" на обоих концах => удаление любого конца стирает только строку-связь, дело и справочник живут.
# Суда в этом списке нет: у карточки он ровно один и хранится обычным внешним ключом.

case_judge = Table(
    "case_judge",
    Base.metadata,
    Column("case_id", ForeignKey("case.id", ondelete="CASCADE"), primary_key=True),
    Column("judge_id", ForeignKey("judge.id", ondelete="CASCADE"), primary_key=True),
)

case_side = Table(
    "case_side",
    Base.metadata,
    Column("case_id", ForeignKey("case.id", ondelete="CASCADE"), primary_key=True),
    Column("side_id", ForeignKey("side.id", ondelete="CASCADE"), primary_key=True),
)

class Judge(Base):
    """Судья-справочник: общий для многих дел (у дела может быть несколько судей)."""

    __tablename__ = "judge"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # ФИО судьи одной строкой (обязательное).
    full_name: Mapped[str] = mapped_column(String)


class Side(Base):
    """Сторона-справочник (истец/ответчик/другое): общая для многих дел."""

    __tablename__ = "side"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # ФИО/название стороны (обязательное).
    full_name: Mapped[str] = mapped_column(String)
    # Роль ровно так, как её называет портал: «Истец», «Взыскатель», «Должник»,
    # «Привлекаемое лицо», «Подсудимый», «Обвиняемый», «Административный истец»…
    # Словарь ролей у судов открытый, поэтому храним текстом, а не enum'ом.
    # Пара (full_name, role) — ключ дедупа справочника.
    role: Mapped[str | None] = mapped_column(String)
    # Грубая классификация роли для фильтров: истец / ответчик / другое (обязательная).
    # Всё, что не истец и не ответчик, схлопывается в «Другое» — точная роль в role.
    type: Mapped[SideType] = mapped_column(Enum(SideType))
