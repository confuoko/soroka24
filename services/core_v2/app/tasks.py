"""Celery-задачи: тонкая обёртка над обходом.

Задача отвечает ровно за три вещи, которых нет и не должно быть в самом обходе:

1. **фоновое исполнение** — эндпоинт не может ждать полминуты, пока браузер ходит
   в суд через прокси и разгадывает капчу;
2. **повторы** — временный отказ (403, таймаут, севшая капча) надо повторить, и с
   другого прокси;
3. **запись `SearchTask`** — наблюдаемая снаружи ручка на асинхронную работу: по её id
   спрашивают «ну что там».

Всё остальное — в app/services/discovery.py. В старом core наоборот: тело задачи
`_sync_case` занимало 190 строк и делало решительно всё, включая определение суда,
разрешение identity, выбор парсера и сохранение.

Здесь же живёт учёт расходов на капчу: он привязан к `SearchTask`, а обход про задачи
не знает — он лишь зовёт колбэк на каждую оплаченную проверку.
"""
import json
from pathlib import Path

from celery.exceptions import Retry
from celery.utils.log import get_task_logger

from app import config
from app.captcha import CaptchaAttempt
from app.celery_app import celery_app
from app.config import COURTS_JSON_PATH, S3_BUCKET
from app.database import session_scope
from app.models import Case
from app.repositories import (
    CaptchaSolveRepository,
    CaseRepository,
    CourtRepository,
    SearchTaskRepository,
)
from app.services.discovery import (
    CrawlResult,
    discover_case,
    is_terminal,
    page_status_of,
    resync_case,
)

logger = get_task_logger(__name__)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def run_search_task(self, task_id: int) -> None:
    """Обойти дело по заведённой задаче поиска.

    Задача несёт либо УИД, либо ссылку — по тому, как дело попало в систему. Обход у них
    один и тот же (см. discover_case), различается только источник.

    Внешний `try` здесь не для красоты: без него любое непредвиденное исключение
    оставляло бы задачу в RUNNING навсегда. Терминальный статус ставится только явными
    вызовами, и никто такую задачу потом не подберёт. Хуже того, RUNNING считается
    активным статусом (`get_active_by_uid`), то есть залипшая задача навсегда
    заблокировала бы повторный запрос этого УИД через API.
    """
    try:
        _run(self, task_id)
    except Retry:
        # Штатный повтор: self.retry бросает Retry, наследника Exception. Статус
        # остаётся RUNNING осознанно — задача вернётся. Без этой ветки каждый повтор
        # уходил бы в FAILED.
        raise
    except Exception as exc:
        _mark_failed(task_id, f"Непредвиденная ошибка: {exc}")
        raise  # пробрасываем, чтобы трейс остался в логах воркера


def _run(celery_task, task_id: int) -> None:
    """Тело задачи: подготовить, сходить, записать результат."""
    with session_scope() as session:
        repo = SearchTaskRepository(session)
        task = repo.get(task_id)
        if task is None:
            return  # задачу удалили — делать нечего
        repo.mark_running(task)
        uid = task.uid
        source_url = task.source_url

    # Номер повтора берём заранее: по нему в отчёте видно, что до дела пришлось идти
    # несколько раз.
    on_captcha = _captcha_recorder(task_id, celery_task.request.retries, source_url)
    on_uid = lambda found: _record_uid(task_id, found)  # noqa: E731

    try:
        result = discover_case(
            uid=uid, source_url=source_url, on_captcha=on_captcha, on_uid=on_uid
        )
    except Exception as exc:
        _handle_failure(celery_task, task_id, exc, uid)
        return

    _finish(task_id, result)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def resync_case_task(self, case_id: int, task_id: int) -> None:
    """Обойти уже известное дело ещё раз.

    Задача заводится до постановки в очередь (см. enqueue_case_resync) — так же, как у
    обхода по запросу пользователя: наблюдаемая ручка нужна и здесь.
    """
    try:
        with session_scope() as session:
            repo = SearchTaskRepository(session)
            task = repo.get(task_id)
            if task is None:
                return
            repo.mark_running(task)
            uid = task.uid
            source_url = task.source_url

        on_captcha = _captcha_recorder(task_id, self.request.retries, source_url)
        on_uid = lambda found: _record_uid(task_id, found)  # noqa: E731

        try:
            result = resync_case(case_id, on_captcha=on_captcha, on_uid=on_uid)
        except Exception as exc:
            _handle_failure(self, task_id, exc, uid)
            return

        _finish(task_id, result)
    except Retry:
        raise
    except Exception as exc:
        _mark_failed(task_id, f"Непредвиденная ошибка: {exc}")
        raise


def enqueue_case_resync(
    case_id: int, queue: str = "regular", countdown: int = 0
) -> int | None:
    """Поставить дело на повторный обход. Возвращает id заведённой задачи или None.

    Задачу создаём тем же способом, каким дело попало в систему: есть сохранённая
    ссылка — по ней (у таких порталов поиска по УИД нет), иначе по УИД.

    Дедупликации по активным задачам здесь намеренно НЕТ. Задача, воркер которой умер
    жёстко, остаётся в RUNNING навсегда, и такая проверка заблокировала бы ручной
    перезапуск дела совсем. Лишний обход — меньшее зло.

    Очередь по умолчанию regular: ручной прогон не должен вытеснять срочные запросы
    пользователей из urgent.
    """
    with session_scope() as session:
        case = session.get(Case, case_id)
        if case is None:
            logger.info("Дело id=%s не найдено — повторный обход не запущен", case_id)
            return None
        source_url = CaseRepository(session).primary_url(case)
        tasks = SearchTaskRepository(session)
        task_id = (
            tasks.create(source_url=source_url).id
            if source_url
            else tasks.create(uid=case.uid).id
        )

    # apply_async только ПОСЛЕ коммита: иначе воркер может схватить задачу раньше, чем
    # строка появится в БД.
    resync_case_task.apply_async(
        args=[case_id, task_id], queue=queue, countdown=countdown
    )
    return task_id


@celery_app.task
def sync_monitored_cases() -> dict:
    """Ночной прогон: поставить на повторный обход все дела на мониторинге.

    Единственное, что делает эта задача, — ВЫБИРАЕТ дела. Сам обход — существующий
    resync, тот же, которым идёт ручной прогон из админки и запрос пользователя. Своей
    логики синхронизации для мониторинга нет и быть не должно: обход дела не зависит от
    того, почему за ним пошли.

    Каждое дело ставится в очередь ОДИН раз, сколько бы подписчиков у него ни было:
    флаг живёт на карточке, а не на подписке (см. Case.is_on_monitoring).

    Дела разносятся по времени через countdown. Это не оптимизация, а условие работы:
    один поход — 25-35 секунд браузера, аренда прокси из ограниченного пула и оплаченная
    капча. Поставь тысячу дел одновременно — воркеры разберут очередь вперегонки, прокси
    кончатся, и капча будет оплачена за попытки, которые упадут по таймауту.

    Ошибку постановки одного дела глотаем: из-за одного недоступного дела не должен
    сорваться прогон остальных. Тихо это не проходит — в лог уходит WARNING, а в отчёте
    задачи видно расхождение между selected и enqueued.
    """
    limit = config.MONITORING_BATCH_LIMIT or None
    with session_scope() as session:
        case_ids = CaseRepository(session).list_monitored_ids(limit)

    enqueued = 0
    for position, case_id in enumerate(case_ids):
        try:
            task_id = enqueue_case_resync(
                case_id,
                queue="regular",
                countdown=position * config.MONITORING_SPACING_SECONDS,
            )
        except Exception as exc:
            logger.warning("Дело id=%s не поставлено на обход: %s", case_id, exc)
            continue
        # None означает «дела с таким id уже нет» — оно исчезло между выборкой и
        # постановкой. Считать его поставленным нельзя.
        if task_id is not None:
            enqueued += 1

    logger.info(
        "Ночной прогон: выбрано %s дел, поставлено на обход %s, разнос %s с, лимит %s",
        len(case_ids),
        enqueued,
        config.MONITORING_SPACING_SECONDS,
        limit or "нет",
    )
    return {"selected": len(case_ids), "enqueued": enqueued}


@celery_app.task
def sync_courts_from_json(src: str | None = None) -> dict:
    """Создать/обновить суды в БД по JSON-справочнику.

    Фоновой задачей, потому что справочник большой (~7700 записей) и синхронный проход
    по нему из админки заблокировал бы обработчик запроса на несколько секунд.
    """
    path = Path(src) if src else COURTS_JSON_PATH
    entries = json.loads(path.read_text(encoding="utf-8"))

    with session_scope() as session:
        created, updated = CourtRepository(session).sync_from_entries(entries)

    logger.info(
        "Справочник судов из %s: создано %s, обновлено %s, всего в файле %s",
        path, created, updated, len(entries),
    )
    return {"src": str(path), "created": created, "updated": updated, "total": len(entries)}


# ------------------------------------------------------- результат и отказы
def _finish(task_id: int, result: CrawlResult) -> None:
    """Записать в задачу итог обхода."""
    if not result.saved_case_ids:
        # До портала дошли, но ни одной карточки не сохранили. Перечисляем, что помешало.
        _attach_captcha_costs(task_id, result)
        _mark_failed(
            task_id, "; ".join(result.failures) or "Не сохранено ни одной карточки"
        )
        return

    # Капчу разгадывали один раз за заход, а карточек из него могло выйти несколько —
    # расход привязываем к первой сохранённой. Размазывать нельзя: деньги списаны однажды.
    _attach_captcha_costs_to_case(task_id, result.saved_case_ids[0])
    _mark_success(task_id, result.saved_case_ids[0])


def _handle_failure(celery_task, task_id: int, exc: Exception, uid: str | None) -> None:
    """Решить, повторять ли поход, и записать это в задачу.

    Само деление ошибок на окончательные и временные живёт в обходе (is_terminal) —
    здесь только реакция на него. Капчи по дороге были оплачены, даже если карточку мы
    так и не получили, поэтому расход привязываем в обеих ветках.
    """
    status = page_status_of(exc)
    _attach_captcha_costs(task_id, CrawlResult(uid=uid))

    if is_terminal(exc):
        _mark_failed(task_id, str(exc), page_status=status)
        return

    # Счётчик попыток проверяем САМИ, до вызова retry: если в retry(exc=...) передан exc,
    # то при исчерпании попыток Celery пробрасывает именно его, а не
    # MaxRetriesExceededError. Ловить MaxRetriesExceededError здесь бесполезно — эта
    # ветка не срабатывала, и задача оставалась в RUNNING с исчерпанными попытками.
    if celery_task.request.retries >= celery_task.max_retries:
        _mark_failed(task_id, f"Исчерпаны попытки: {exc}", page_status=status)
        return

    _record_error(task_id, str(exc), page_status=status)
    raise celery_task.retry(exc=exc, countdown=30)


# ---------------------------------------------------- жизненный цикл SearchTask
def _record_uid(task_id: int, uid: str) -> None:
    """Записать в задачу УИД, найденный на странице.

    Задача, заведённая по ссылке, создаётся без УИД — узнать его можно только сходив на
    портал. Сохраняем сразу, чтобы он был виден в статусе задачи и в админке даже если
    разбор дальше упадёт.
    """
    with session_scope() as session:
        repo = SearchTaskRepository(session)
        task = repo.get(task_id)
        if task is not None:
            repo.set_uid(task, uid)


def _record_error(task_id: int, error: str, page_status: int | None = None) -> None:
    """Записать ошибку, не меняя статус: попытки ещё могут остаться."""
    with session_scope() as session:
        task = SearchTaskRepository(session).get(task_id)
        if task is not None:
            task.last_error = error
            if page_status is not None:
                task.page_status = page_status


def _mark_failed(task_id: int, error: str, page_status: int | None = None) -> None:
    """Пометить задачу окончательно проваленной."""
    with session_scope() as session:
        repo = SearchTaskRepository(session)
        task = repo.get(task_id)
        if task is not None:
            repo.mark_failed(task, error, page_status=page_status)


def _mark_success(task_id: int, case_id: int) -> None:
    """Пометить задачу выполненной и привязать к ней карточку.

    Карточка в задаче одна, а вышло их из обхода могло несколько — кладём первую
    сохранённую. Полный список всегда достаётся запросом по УИД.
    """
    with session_scope() as session:
        repo = SearchTaskRepository(session)
        task = repo.get(task_id)
        if task is not None:
            repo.mark_success(task, case_id)


# ------------------------------------------------------------ расходы на капчу
def _captcha_recorder(task_id: int, celery_retry: int, source_url: str | None = None):
    """Собрать колбэк, который пишет расход на капчу в БД.

    Каждая запись идёт СВОЕЙ короткой транзакцией, а не копится до конца задачи: деньги
    списаны в момент, когда сервис отдал ответ, а поход браузера после этого может идти
    ещё минуту и закончиться падением воркера — расход бы потерялся.

    Дело здесь обычно ещё неизвестно (задачу заводили ссылкой, УИД берётся со страницы),
    поэтому case_id проставляется позже — см. _attach_captcha_costs.

    Ошибку записи глотаем: сорванный учёт не повод отказываться от дела, ради которого
    капчу и разгадывали.
    """

    def _record(attempt: CaptchaAttempt) -> None:
        try:
            with session_scope() as session:
                CaptchaSolveRepository(session).record(
                    attempt,
                    search_task_id=task_id,
                    source_url=source_url,
                    # Бакет в записи от решателя не приходит — он наш, из настроек.
                    captcha_bucket=S3_BUCKET if attempt.captcha_key else None,
                    celery_retry=celery_retry,
                )
            logger.info(
                "Капча задачи %s: %s %s (задача сервиса %s)",
                task_id, attempt.cost, attempt.currency, attempt.task_id,
            )
        except Exception as exc:
            logger.warning(
                "Не удалось записать расход на капчу задачи %s: %s", task_id, exc
            )

    return _record


def _attach_captcha_costs_to_case(task_id: int, case_id: int) -> None:
    """Привязать расходы задачи к делу по его id.

    Ошибку глотаем: расход уже записан на задачу, и потерять из-за отчёта саму
    синхронизацию было бы хуже, чем потерять привязку к делу.
    """
    try:
        with session_scope() as session:
            CaptchaSolveRepository(session).attach_case(task_id, case_id)
    except Exception as exc:
        logger.warning(
            "Не удалось привязать расходы задачи %s к делу: %s", task_id, exc
        )


def _attach_captcha_costs(task_id: int, result: CrawlResult) -> None:
    """Привязать расходы к карточке дела, если она уже есть в БД.

    Зовём и на успехе, и на отказах: провалившийся обход тоже стоил денег, а при
    повторных обходах карточка обычно уже существует. Если её ещё нет (дело качаем
    впервые), расход пока висит только на задаче и привяжется, когда она появится.
    """
    if not result.uid or result.court is None:
        return
    try:
        with session_scope() as session:
            case = _find_single_card(session, result.uid, result.court)
            case_id = case.id if case is not None else None
    except Exception as exc:
        logger.warning("Не удалось найти дело для расходов задачи %s: %s", task_id, exc)
        return
    if case_id is not None:
        _attach_captcha_costs_to_case(task_id, case_id)


def _find_single_card(session, uid: str, court):
    """Карточка дела, если она определяется ОДНОЗНАЧНО.

    Номер дела в ветках отказа известен не всегда, поэтому ищем по паре «УИД + суд». Если
    карточек там несколько — не выбираем никакую: приписать расход на капчу случайной
    карточке хуже, чем не приписать никакой.
    """
    found = CaseRepository(session).list_by_uid_and_court(uid, court.id)
    if len(found) > 1:
        logger.info(
            "У дела %s в суде %s несколько карточек (%s) — карточка не определена",
            uid, court.code, ", ".join(case.code for case in found),
        )
        return None
    return found[0] if found else None
