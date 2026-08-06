"""Базовый интерфейс клиента суда и общие исключения.

Клиент суда знает, КАК добраться до карточки дела по УИД (навигация в браузере)
и КАК её разобрать. Под каждый тип суда/страницы — свой класс-наследник.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class PageSnapshot:
    """Снимок страницы, на которой мы упали: чем сайт ответил в момент отказа.

    Снимать его можно только внутри ChromiumSession, пока браузер жив: как только клиент
    суда вышел из своего `with`, страницы уже нет. Поэтому клиент прикладывает снимок к
    исключению, а сохраняет его в S3 уже вызывающий код (app/monitoring/tasks.py).

    url и status важны не меньше разметки: редирект на страницу блокировки виден по url,
    а 403 — по статусу, даже если тело выглядит как обычная страница.
    """

    html: str
    url: str | None = None
    status: int | None = None


class CourtError(Exception):
    """Базовая ошибка при работе с судом.

    page — снимок страницы отказа, если его удалось снять (иначе None).
    """

    def __init__(self, *args, page: PageSnapshot | None = None) -> None:
        super().__init__(*args)
        self.page = page


class UnsupportedCourt(CourtError):
    """УИД не относится ни к одному поддержанному суду."""


class CaseNotFound(CourtError):
    """По УИД ничего не нашлось на странице поиска."""


class FetchFailed(CourtError):
    """Не удалось добраться до карточки дела (таймаут, капча, поменялась разметка).

    Ошибка считается временной: вызывающий код её ретраит. Оборачивает исходное
    исключение, чтобы к нему можно было приложить снимок страницы.
    """

    def __init__(self, uid: str, reason: BaseException, page: PageSnapshot | None = None) -> None:
        super().__init__(f"{uid}: {reason}", page=page)
        self.uid = uid
        self.reason = reason


class NewCourtException(CourtError):
    """Суд с карточки ещё не заведён в БД (нужно добавить справочник суда)."""


class CourtClient(ABC):
    """Интерфейс клиента суда.

    page_type — тип страницы (по нему выбирается парсер в app/parsers/).

    Конструктор наследника обязан принимать именованный аргумент proxy
    (ProxySettings | None) — его передаёт define_court_by_uid, арендовав прокси из
    пула. На портал суда клиент должен ходить только через него.
    """

    page_type: str

    @abstractmethod
    def fetch_case_html(self, uid: str) -> str:
        """Найти дело по УИД и вернуть HTML его карточки."""

    @abstractmethod
    def parse(self, html: str) -> dict:
        """Разобрать HTML карточки в данные дела (пока — заглушка)."""
