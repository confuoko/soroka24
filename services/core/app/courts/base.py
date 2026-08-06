"""Базовый интерфейс клиента суда и общие исключения.

Клиент суда знает, КАК добраться до карточки дела по УИД (навигация в браузере)
и КАК её разобрать. Под каждый тип суда/страницы — свой класс-наследник.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.browser import ChromiumSession


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


# Статусы, при которых повторить попытку осмысленно: портал лёг (5xx), нас притормозили
# (429) или отсекли по IP (403). Проверять их приходится вручную: для Playwright любой
# ответ сервера — успешная навигация, исключение он бросает только на сетевых отказах.
# Без явной проверки страница ошибки молча уезжала бы в парсер, а тот падал бы с
# неретраибельной ошибкой разбора — то есть временный сбой хоронил бы задачу навсегда.
RETRYABLE_STATUSES = frozenset({403, 429})


def capture_page(session: ChromiumSession, status: int | None) -> PageSnapshot | None:
    """Снять страницу для разбора отказа. Само снятие не должно ронять ничего сверху.

    Браузер в момент отказа может быть уже нездоров (упал контекст, повисла вкладка),
    поэтому любую ошибку снятия глотаем: исходная причина отказа важнее снимка.
    """
    try:
        return PageSnapshot(html=session.content(), url=session.page.url, status=status)
    except Exception:
        return None


def is_retryable_status(status: int | None) -> bool:
    """Портал ответил так, что имеет смысл прийти ещё раз (и с другого прокси)?"""
    return status is not None and (status >= 500 or status in RETRYABLE_STATUSES)


def check_status(
    session: ChromiumSession, uid: str, status: int | None, where: str
) -> None:
    """Упасть сразу, если портал ответил ошибкой, — не дожидаясь таймаута.

    Иначе клиент искал бы элементы на странице ошибки все 30 секунд, а в тексте отказа
    оставался бы бесполезный «Page.fill: Timeout» вместо честного кода ответа.

    Снимок снимаем только при отказе: карточка дела весит под полмегабайта, и дёргать
    page.content() на каждой удачной навигации незачем.
    """
    if is_retryable_status(status):
        raise FetchFailed(
            uid,
            RuntimeError(f"{where} ответила HTTP {status}"),
            page=capture_page(session, status),
        )


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
