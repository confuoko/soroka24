"""Учёт расходов на разгаданные капчи (CaptchaSolve) в БД.

Строка пишется на каждое обращение к решателю, поэтому запись обязана быть
идемпотентной: колбэк учёта может сработать дважды (ретрай воркера, повторная
обработка), а расход от этого удваиваться не должен. Отсюда ON CONFLICT DO NOTHING
по паре «сервис + id задачи у сервиса».
"""
from decimal import Decimal
from typing import Optional
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.captcha import ATTEMPT_SOLVED, CaptchaAttempt
from app.models.database import CaptchaSolve


def _host_of(url: Optional[str]) -> Optional[str]:
    """Хост из ссылки на карточку (или None). Нужен, чтобы видеть участок в отчёте."""
    if not url:
        return None
    return urlsplit(url).hostname


class CaptchaSolveRepository:
    """Запись и подсчёт расходов на капчу. Работает в рамках переданной сессии."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        attempt: CaptchaAttempt,
        *,
        search_task_id: Optional[int] = None,
        case_id: Optional[int] = None,
        court_id: Optional[int] = None,
        source_url: Optional[str] = None,
        captcha_bucket: Optional[str] = None,
        celery_retry: Optional[int] = None,
    ) -> None:
        """Записать расход на одну капчу.

        Повторная запись того же решения молча ничего не делает: строку опознаём по
        паре (provider, provider_task_id).
        """
        self._session.execute(
            insert(CaptchaSolve)
            .values(
                provider=attempt.provider,
                provider_task_id=attempt.task_id,
                search_task_id=search_task_id,
                case_id=case_id,
                court_id=court_id,
                host=_host_of(source_url),
                status=attempt.status,
                cost=attempt.cost,
                currency=attempt.currency,
                solve_count=attempt.solve_count,
                attempt_no=attempt.attempt_no,
                celery_retry=celery_retry,
                captcha_bucket=captcha_bucket,
                captcha_key=attempt.captcha_key,
                requested_at=attempt.requested_at,
                # solved_at — только для разгаданных: у таймаута ready_at означает
                # «когда мы сдались», а не «когда решили», и путать их нельзя.
                solved_at=attempt.ready_at if attempt.status == ATTEMPT_SOLVED else None,
                latency_ms=attempt.latency_ms,
            )
            .on_conflict_do_nothing(constraint="uq_captcha_solve_provider_task")
        )

    def attach_case(self, search_task_id: int, case_id: int) -> int:
        """Привязать расходы задачи к делу. Возвращает число обновлённых строк.

        Нужно потому, что в момент разгадки дело часто ещё неизвестно: задачу заводят
        ссылкой, а УИД (и значит карточку) мы узнаём только с полученной страницы —
        капчу как раз и решаем, чтобы до неё добраться.

        Уже привязанные строки не трогаем: одна задача = одна карточка, и переклеивать
        расход на другое дело было бы ошибкой.
        """
        result = self._session.execute(
            update(CaptchaSolve)
            .where(
                CaptchaSolve.search_task_id == search_task_id,
                CaptchaSolve.case_id.is_(None),
            )
            .values(case_id=case_id)
        )
        return result.rowcount

    def total_cost_by_case(self, case_id: int) -> Decimal:
        """Сколько денег ушло на капчу по этому делу за всё время.

        SUM игнорирует NULL, то есть строки с неизвестной ценой сюда не попадают —
        их надо смотреть через unknown_cost_count(), иначе расход выглядит меньше
        настоящего.
        """
        return self._session.scalar(
            select(func.coalesce(func.sum(CaptchaSolve.cost), 0)).where(
                CaptchaSolve.case_id == case_id
            )
        )

    def total_cost_by_task(self, search_task_id: int) -> Decimal:
        """Сколько стоила капча в рамках одной задачи синхронизации."""
        return self._session.scalar(
            select(func.coalesce(func.sum(CaptchaSolve.cost), 0)).where(
                CaptchaSolve.search_task_id == search_task_id
            )
        )

    def unknown_cost_count(self, case_id: int) -> int:
        """Сколько капч по делу оплачено, но цена неизвестна (не дождались ответа).

        Это честность отчёта: пока число не ноль, сумма по делу — оценка снизу.
        """
        return self._session.scalar(
            select(func.count())
            .select_from(CaptchaSolve)
            .where(CaptchaSolve.case_id == case_id, CaptchaSolve.cost.is_(None))
        )
