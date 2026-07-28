"""Базовый интерфейс парсера карточки дела (стратегия под тип HTML-страницы).

Парсер — чистое преобразование HTML -> данные: без БД, без сети. Под каждый тип
страницы (см. CourtClient.page_type) — свой класс-наследник; выбор — в registry.py.
"""
from abc import ABC, abstractmethod


class CaseParser(ABC):
    """Интерфейс парсера.

    page_type — тип страницы, по которому реестр выбирает нужную стратегию
    (совпадает с CourtClient.page_type у соответствующего клиента суда).
    """

    page_type: str

    @abstractmethod
    def parse(self, html: str) -> dict:
        """Разобрать HTML карточки в данные дела.

        Возвращает словарь (любой список пуст, если данных нет):
            {
              "judge_names": ["Каурова Д.С.", ...],
              "sides": [{"role": "Истец", "full_name": "..."}, ...],
              "events": [
                {"event_date": date, "state_description": "...",
                 "document_str": "..." | None},
                ...
              ],
              "place_history": [
                {"place_date": date, "place_description": "В канцелярии",
                 "comment": "..." | None},
                ...
              ],
            }
        """
