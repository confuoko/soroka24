"""ParsedCase — то, что парсер достал из страницы. Граница между HTML и БД.

    HTML → парсер → ParsedCase → sync_case → PostgreSQL

В старом core парсеры отдавали обычный `dict`, а его форма была описана только в
докстринге на 70 строк. Опечатка в имени ключа означала молча пропавшее поле.

## Главное, что нужно понять про этот модуль: UNSET

У скалярных полей карточки ТРИ состояния, а не два, и разница между вторым и третьим —
самое неочевидное место всего парсинга:

    поле = "02-0123/2026"   на странице есть метка, вот её значение
    поле = None             метка на странице ПРОПАЛА → колонку в БД надо обнулить
    поле = UNSET            у этого портала такой метки не бывает ВООБЩЕ → колонку
                            в БД трогать нельзя

Наборы меток у порталов разные: карточка мировых судов Москвы отдаёт 11 скалярных полей,
движок msudrf — 5, портал Петербурга — 4. Если бы отсутствующие поля приезжали как None,
то при каждом обходе петербургского дела обнулялись бы, например, «Номер заявления» и
«Дата регистрации» — просто потому, что на этом портале таких меток нет.

Поэтому каждый парсер выставляет ТОЛЬКО свои поля, а остальные остаются UNSET. Собирает
их для записи в БД метод card_fields(): он отдаёт ровно те поля, которые парсер реально
прислал.

У списков (события, стороны, документы…) такой проблемы нет: пустой список и есть
«на странице ничего не нашлось», отдельного UNSET им не нужно.

У дочерних строк необязательные поля — обычный None: репозитории читают их через
.get(), поэтому None и отсутствие для них всегда означали одно и то же.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date, datetime
from typing import Any


class Unset:
    """Тип значения UNSET. Существует ровно один экземпляр — сам UNSET.

    Отдельный класс, а не None и не строка-заглушка: None здесь занят и означает
    «метка пропала со страницы», а любое значение-заглушка рано или поздно совпало бы
    с настоящими данными.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


#: «Этого поля у портала не бывает вообще» — не путать с None.
UNSET = Unset()


@dataclass
class ParsedSide:
    """Участник дела."""

    # Роль ровно так, как её называет портал: «Истец», «Взыскатель», «Должник»,
    # «Подсудимый», «Обвиняемый». Словарь ролей у судов открытый, поэтому строка.
    # None бывает у портала Петербурга, где роль в таблице не указана.
    role: str | None
    full_name: str


@dataclass
class ParsedEvent:
    """Строка «Истории состояний» / «Движения дела»."""

    # Дата И время события МЕСТНЫМ временем суда, naive. Портал пояса не указывает,
    # в момент это превращает слой БД. Времени на странице может не быть — тогда
    # местная полночь. В identity события входит только ДАТА (см. event_uid).
    event_date: datetime
    state_description: str
    document_str: str | None = None
    # «Дата размещения» на портале. Отдают только типы B и D; в identity не входит.
    published_at: date | None = None


@dataclass
class ParsedPlace:
    """Строка «Истории местонахождения»."""

    place_date: date
    place_description: str
    comment: str | None = None


@dataclass
class ParsedSession:
    """Судебное заседание."""

    # Дата со временем, местным временем суда. В отличие от события, время ВХОДИТ
    # в identity заседания (см. court_session_uid) — поэтому подстановка полуночи
    # при пустой ячейке обязана быть детерминированной.
    session_date: datetime
    stage: str
    place: str | None = None
    result: str | None = None
    basis: str | None = None


@dataclass
class ParsedDocument:
    """Документ по делу — только метаданные, ни текста, ни ссылки на файл.

    ПОРЯДОК строк в списке значим: из него считается номер повторения в identity
    документа (см. document_uid). Портал отдаёт до 21 одинаковой строки «Приложение»
    за одну дату, и различает их только позиция. Пересортировать список нельзя.
    """

    document_date: date
    document_type: str


# Скалярные поля карточки — те, что парсер берёт из меток «поле: значение».
# Порядок как в модели Case, чтобы читать рядом.
CARD_FIELD_NAMES = (
    "application_number",
    "incoming_number",
    "code",
    "receipt_date",
    "registration_date",
    "accepted_date",
    "first_instance_date",
    "first_instance_decision",
    "decision_effective_date",
    "superior_case_number",
    "category",
    "status",
)


@dataclass
class ParsedCase:
    """Всё, что парсер достал из карточки дела.

    Скалярные поля по умолчанию UNSET: парсер выставляет только те, которые его портал
    действительно показывает (см. докстринг модуля). Списки по умолчанию пусты.

    Чего здесь нет намеренно:

    * УИД и номер дела как ключа карточки — они приходят не из содержимого страницы,
      а из навигации (таблица результатов поиска, адрес ссылки), и разрешаются до
      разбора. Поле `code` здесь есть только потому, что портал Москвы печатает номер
      и на самой карточке;
    * адрес страницы — его знает тот, кто ходил на портал, а не тот, кто читает HTML;
    * суд — определяется по справочнику, а не по тексту страницы.
    """

    application_number: str | None | Unset = UNSET
    incoming_number: str | None | Unset = UNSET
    code: str | None | Unset = UNSET
    receipt_date: date | None | Unset = UNSET
    registration_date: date | None | Unset = UNSET
    accepted_date: date | None | Unset = UNSET
    first_instance_date: date | None | Unset = UNSET
    first_instance_decision: str | None | Unset = UNSET
    decision_effective_date: date | None | Unset = UNSET
    superior_case_number: str | None | Unset = UNSET
    category: str | None | Unset = UNSET
    status: str | None | Unset = UNSET

    judge_names: list[str] = field(default_factory=list)
    sides: list[ParsedSide] = field(default_factory=list)
    events: list[ParsedEvent] = field(default_factory=list)
    place_history: list[ParsedPlace] = field(default_factory=list)
    court_sessions: list[ParsedSession] = field(default_factory=list)
    documents: list[ParsedDocument] = field(default_factory=list)

    def card_fields(self) -> dict[str, Any]:
        """Скалярные поля, которые парсер РЕАЛЬНО прислал (без UNSET).

        Именно это записывается в карточку. Поле со значением None здесь остаётся —
        оно означает «метка пропала со страницы, обнули колонку». Поля, которого у
        портала не бывает, здесь не будет вовсе, и колонка останется нетронутой.
        """
        return {
            name: getattr(self, name)
            for name in CARD_FIELD_NAMES
            if getattr(self, name) is not UNSET
        }

    def is_empty(self) -> bool:
        """Разбор пустой: ни одного заполненного поля и ни одной строки.

        Нужно как охранник перед записью: страница иногда приходит недорендеренной
        (браузер отдал <html><head></head><body></body></html>), и сохранение такого
        разбора затёрло бы события, судей и стороны уже существующей карточки. Парсер
        при этом обязан не падать, а вернуть именно пустой результат.
        """
        return not any(
            getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) is not UNSET
        )
