"""Базовый интерфейс клиента суда и общие исключения.

Клиент суда знает, КАК добраться до карточки дела по УИД (навигация в браузере)
и КАК её разобрать. Под каждый тип суда/страницы — свой класс-наследник.
"""
from abc import ABC, abstractmethod


class CourtError(Exception):
    """Базовая ошибка при работе с судом."""


class UnsupportedCourt(CourtError):
    """УИД не относится ни к одному поддержанному суду."""


class CaseNotFound(CourtError):
    """По УИД ничего не нашлось на странице поиска."""


class CourtClient(ABC):
    """Интерфейс клиента суда.

    page_type — тип страницы (по нему выбирается парсер в app/parsers/).
    """

    page_type: str

    @abstractmethod
    def fetch_case_html(self, uid: str) -> str:
        """Найти дело по УИД и вернуть HTML его карточки."""

    @abstractmethod
    def parse(self, html: str) -> dict:
        """Разобрать HTML карточки в данные дела (пока — заглушка)."""
