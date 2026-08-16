"""Celery-таски мониторинга.

sync_case — синхронизация дела: сходить браузером в суд, найти карточки, разобрать их и
создать/обновить Case. Тяжёлая часть (Chromium) вынесена из API в фоновую задачу; API
лишь ставит задачу и отдаёт её id.

Карточек у одной задачи может быть несколько: поиск по УИД отдаёт таблицу, где по одному
УИД видны и приказное производство, и последовавшее исковое, иногда в разных участках.
Обходим все строки — карточка это тройка «УИД + суд + номер дела», и каждая строка даёт
свою. Отказ на одной карточке не уносит остальные.

Суд определяется НЕ по УИД, а по тому же источнику, откуда пришло дело: по номеру участка
из строки таблицы (поиск по УИД) либо по хосту ссылки. Собрать код суда из УИД нельзя —
у 36 московских судов номер участка не совпадает с числом в коде.

enqueue_case_resync — поставить дело на повторный парсинг, зная только его id в БД
(sync_case принимает id задачи, а не дела).

Каждый вызов парсинга оставляет след: HTML страницы ложится снапшотом в S3, а в
Case.diff_history дозаписывается запись о том, что изменилось — в том числе когда
изменений нет и когда сайт суда не открылся.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from celery.exceptions import Retry
from celery.utils.log import get_task_logger

from app.browser import lease_proxy
from app.captcha import CaptchaAttempt
from app.celery_app import celery_app
from app.config import (
    HTML_SNAPSHOT_ENABLED,
    MONITORING_BATCH_LIMIT,
    MONITORING_INTERVAL_HOURS,
    MONITORING_SPACING_SECONDS,
    S3_BUCKET,
)
from app.courts import (
    CaseNotFound,
    FetchedCard,
    UnsupportedCourt,
    define_court_by_uid,
    define_court_by_url,
)
from app.models.database import Case, session_scope
from app.monitoring.case_update import CaseChanges, update_case
from app.monitoring.parse_history import (
    STATUS_CHANGED,
    STATUS_FETCH_ERROR,
    STATUS_NO_CHANGES,
    STATUS_PARSE_ERROR,
    append_parse_entry,
    build_entry,
    changes_to_dict,
    last_entry,
)
from app.repositories import (
    CaptchaSolveRepository,
    CaseRepository,
    CourtRepository,
    SearchTaskRepository,
)
from app.storage import is_failure_key, save_snapshot, snapshot_sha256, url_label
from app.storage.html_snapshots import card_folder

logger = get_task_logger(__name__)


@dataclass(frozen=True)
class CourtRef:
    """Суд, вынутый из сессии: только id и код.

    Объект Court живёт в своей session_scope и за её пределами уже недоступен, а суд нужен
    и дальше — в ключе снапшота и при поиске карточки. Тащить ради этого открытую сессию
    через весь обход незачем: id и кода хватает.
    """

    id: int
    code: str


def _log_changes(uid: str, changes: CaseChanges) -> None:
    """Записать в лог, что изменилось по делу за эту синхронизацию."""
    if not changes.has_changes():
        logger.info("Дело %s: изменений нет", uid)
        return
    for change in changes.field_changes:
        logger.info(
            "Изменилось поле дела %s: %s: %r -> %r", uid, change.field, change.old, change.new
        )
    for event in changes.new_events:
        logger.info("Новое событие по делу %s: %s — %s", uid, event.event_date, event.state_description)
    for event in changes.updated_events:
        logger.info("Изменён документ события по делу %s: %s — %s", uid, event.event_date, event.state_description)
    for event in changes.removed_events:
        logger.info("Удалено событие по делу %s: %s — %s", uid, event.event_date, event.state_description)
    for place in changes.new_places:
        logger.info("Новое местонахождение по делу %s: %s — %s", uid, place.place_date, place.place_description)
    for place in changes.updated_places:
        logger.info("Изменён комментарий местонахождения по делу %s: %s — %s", uid, place.place_date, place.place_description)
    for place in changes.removed_places:
        logger.info("Удалено местонахождение по делу %s: %s — %s", uid, place.place_date, place.place_description)
    for session in changes.new_sessions:
        logger.info("Назначено заседание по делу %s: %s — %s", uid, session.session_date, session.stage)
    for session in changes.updated_sessions:
        logger.info(
            "Изменено заседание по делу %s: %s — %s (результат: %s)",
            uid, session.session_date, session.stage, session.result,
        )
    for session in changes.removed_sessions:
        logger.info("Снято заседание по делу %s: %s — %s", uid, session.session_date, session.stage)
    for document in changes.new_documents:
        logger.info("Новый документ по делу %s: %s — %s", uid, document.document_date, document.document_type)
    for document in changes.removed_documents:
        logger.info("Удалён документ по делу %s: %s — %s", uid, document.document_date, document.document_type)
    for judge in changes.added_judges:
        logger.info("Привязан судья: %s к делу %s", judge.full_name, uid)
    for judge in changes.removed_judges:
        logger.info("Отвязан судья: %s от дела %s", judge.full_name, uid)
    for side in changes.added_sides:
        logger.info("Привязана сторона: %s (%s) к делу %s", side.full_name, side.type.value, uid)
    for side in changes.removed_sides:
        logger.info("Отвязана сторона: %s (%s) от дела %s", side.full_name, side.type.value, uid)


def _take_snapshot(
    uid: str,
    html: str,
    fetched_at: datetime,
    court: CourtRef,
    code: str,
) -> tuple[dict | None, bool]:
    """Положить HTML карточки в S3. Возвращает (данные снапшота, html_unchanged).

    Если разметка побайтово совпала с прошлым разом (тот же sha256), новый объект не
    заливаем — переиспользуем ключ предыдущей записи истории. Это частый случай: дело
    проверяется регулярно, а меняется редко.

    Суд и номер дела обязательны: карточка — это тройка «УИД + суд + номер», и без них
    нельзя ни найти прошлый снапшот, ни положить новый в папку своей карточки.

    Недоступный S3 не должен ронять парсинг: дело важнее архива разметки, поэтому
    ошибку заливки только логируем, а разбор продолжается со snapshot=None.
    """
    if not HTML_SNAPSHOT_ENABLED:
        return None, False

    sha = snapshot_sha256(html)

    # Короткое чтение: чем закончился предыдущий парсинг ЭТОЙ карточки.
    with session_scope() as session:
        case = CaseRepository(session).get_by_uid_court_code(uid, court.id, code)
        previous = last_entry(case) if case is not None else None

    # Ключ переиспользуем только от УСПЕШНОГО парсинга: в последней записи истории теперь
    # может лежать ключ страницы отказа (капча/блокировка), и подставлять его карточке
    # нельзя — история дела стала бы ссылаться на мусор.
    previous_key = previous.get("html_key") if previous is not None else None
    if previous_key and not is_failure_key(previous_key) and previous.get("html_sha256") == sha:
        return (
            {
                "html_bucket": previous.get("html_bucket"),
                "html_key": previous["html_key"],
                "html_sha256": sha,
                "html_size": previous.get("html_size"),
            },
            True,
        )

    try:
        return (
            save_snapshot(uid, html, fetched_at, card=card_folder(court.code, code)),
            False,
        )
    except Exception as exc:
        logger.warning("Не удалось сохранить снапшот HTML дела %s в S3: %s", uid, exc)
        return None, False


def _find_single_card(session, uid: str, court: CourtRef | None, code: str | None):
    """Карточка дела, к которой относится происходящее, — если она определяется однозначно.

    Номер дела известен не всегда: в ветках ошибок задача успевает узнать суд (а иногда и
    его не успевает), но до таблицы результатов или до разбора не доходит. Тогда карточку
    ищем по паре «УИД + суд», и если их там несколько — не выбираем никакую: приписать
    отказ или расход на капчу случайной карточке хуже, чем не приписать никакой.
    """
    if court is None:
        return None

    cases = CaseRepository(session)
    if code is not None:
        return cases.get_by_uid_court_code(uid, court.id, code)

    found = cases.list_by_uid_and_court(uid, court.id)
    if len(found) > 1:
        logger.info(
            "У дела %s в суде %s несколько карточек (%s) — карточка не определена",
            uid,
            court.code,
            ", ".join(case.code for case in found),
        )
        return None
    return found[0] if found else None


def _record_parse_entry(
    uid: str,
    status: str,
    fetched_at: datetime,
    task_id: int,
    snapshot: dict | None = None,
    error: str | None = None,
    html_unchanged: bool = False,
    court: CourtRef | None = None,
    code: str | None = None,
) -> None:
    """Дозаписать в историю дела запись о неудачном парсинге (если дело уже есть в БД).

    Если дела в БД ещё нет (первый парсинг провалился) или карточка не определяется
    однозначно, дозаписывать некуда — такой провал остаётся в SearchTask
    (last_error/status).
    """
    if court is None:
        # Суд не определён: до таблицы результатов (или до хоста) дело не дошло.
        logger.info("Суд дела %s не определён — запись истории парсинга пропущена", uid)
        return

    with session_scope() as session:
        case = _find_single_card(session, uid, court, code)
        if case is None:
            logger.info("Дело %s ещё не создано — запись истории парсинга пропущена", uid)
            return
        append_parse_entry(
            case,
            build_entry(
                status=status,
                fetched_at=fetched_at,
                task_id=task_id,
                snapshot=snapshot,
                error=error,
                html_unchanged=html_unchanged,
            ),
        )


def _save_failure_page(
    uid: str, exc: BaseException, fetched_at: datetime
) -> tuple[dict | None, int | None]:
    """Положить в S3 страницу, на которой упали. Возвращает (данные снапшота, HTTP-статус).

    Снимок приходит приложенным к исключению клиента суда (CourtError.page): живой браузер
    есть только внутри клиента, здесь его уже нет. Если снимка нет (упало до открытия
    страницы или снять не удалось) — сохранять нечего.

    Ошибку S3 глотаем: архив разметки не важнее самой причины отказа, ради записи которой
    мы сюда и пришли.
    """
    page = getattr(exc, "page", None)
    if page is None:
        return None, None
    if not HTML_SNAPSHOT_ENABLED:
        return None, page.status
    try:
        return save_snapshot(uid, page.html, fetched_at, failed=True), page.status
    except Exception as storage_exc:
        logger.warning(
            "Не удалось сохранить страницу отказа дела %s в S3: %s", uid, storage_exc
        )
        return None, page.status


def _captcha_recorder(task_id: int, celery_retry: int, source_url: str | None = None):
    """Собрать колбэк, который пишет расход на капчу в БД.

    Каждая запись идёт СВОЕЙ короткой транзакцией, а не копится до конца задачи:
    деньги списаны в момент, когда сервис отдал ответ, а поход браузера после этого
    может идти ещё минуту и закончиться падением воркера — расход бы потерялся.

    Дело здесь обычно ещё неизвестно (задачу заводили ссылкой, УИД берётся с самой
    страницы), поэтому case_id проставляется позже — _attach_captcha_costs().

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
            logger.warning("Не удалось записать расход на капчу задачи %s: %s", task_id, exc)

    return _record


def _attach_captcha_costs_to_case(task_id: int, case_id: int) -> None:
    """Привязать расходы задачи к делу по его id в БД.

    Ошибку глотаем и здесь: расход уже записан на задачу, и потерять из-за отчёта
    саму синхронизацию было бы хуже, чем потерять привязку к делу.
    """
    try:
        with session_scope() as session:
            CaptchaSolveRepository(session).attach_case(task_id, case_id)
    except Exception as exc:
        logger.warning("Не удалось привязать расходы задачи %s к делу: %s", task_id, exc)


def _attach_captcha_costs(
    task_id: int, uid: str | None, court: CourtRef | None, code: str | None = None
) -> None:
    """Привязать расходы задачи к карточке дела, если она уже есть в БД.

    Зовём и на успехе, и на отказах: провалившийся обход тоже стоил денег, а карточка
    при повторных обходах обычно уже существует. Если карточки ещё нет (дело качаем
    впервые), расход пока висит только на задаче и привяжется, когда она появится.

    Капчу разгадывают один раз за заход, а карточек из этого захода может выйти несколько
    (по строке таблицы на каждую) — расход привязываем к первой сохранённой, размазывать
    его по всем нельзя: деньги списаны однажды.
    """
    if not uid or court is None:
        return
    try:
        with session_scope() as session:
            case = _find_single_card(session, uid, court, code)
            case_id = case.id if case is not None else None
    except Exception as exc:
        logger.warning("Не удалось найти дело для расходов задачи %s: %s", task_id, exc)
        return
    if case_id is not None:
        _attach_captcha_costs_to_case(task_id, case_id)


def _court_by_participok(region_code: str, participok_no: int) -> CourtRef | None:
    """Суд по номеру участка из таблицы результатов (или None, если его нет в справочнике).

    Так определяется суд дела, найденного поиском по УИД. Именно по номеру участка, а не
    по УИД: у 36 московских судов номер участка не совпадает с числом в коде суда
    (участок № 463 — это код 77MS0466, а 77MS0463 — совсем другой суд), так что собрать
    код арифметикой нельзя.
    """
    with session_scope() as session:
        court = CourtRepository(session).get_by_participok(region_code, participok_no)
        return CourtRef(id=court.id, code=court.code) if court is not None else None


def _court_by_url(url: str) -> CourtRef | None:
    """Суд по ссылке на карточку (или None, если его нет в справочнике).

    Так определяется суд дела, пришедшего ссылкой, и известен он ещё до похода на
    портал. Обычно хватает хоста (на msudrf.ru у каждого участка свой поддомен), но у
    порталов с одним хостом на весь регион суд ищется по номеру участка из пути —
    развилку держит CourtRepository.get_by_url.
    """
    with session_scope() as session:
        court = CourtRepository(session).get_by_url(url)
        return CourtRef(id=court.id, code=court.code) if court is not None else None


def _record_uid(task_id: int, uid: str) -> None:
    """Записать в задачу УИД, найденный на странице дела.

    Задача, заведённая по ссылке, создаётся без УИД — узнать его можно только сходив
    на портал. Сохраняем сразу, чтобы он был виден в статусе задачи и в админке даже
    если разбор дальше упадёт.
    """
    with session_scope() as session:
        repo = SearchTaskRepository(session)
        task = repo.get(task_id)
        if task is not None:
            repo.set_uid(task, uid)


def _record_error(task_id: int, error: str, page_status=None) -> None:
    """Записать ошибку в задачу, не меняя статус (попытки ещё могут остаться)."""
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
    сохранённую. Полный список всегда выводится запросом по УИД, дублировать его в
    задаче незачем.
    """
    with session_scope() as session:
        repo = SearchTaskRepository(session)
        task = repo.get(task_id)
        if task is not None:
            repo.mark_success(task, case_id)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def sync_case(self, task_id: int) -> None:
    """Обработать задачу поиска: найти дело по УИД и сохранить Case.

    Тело — в _sync_case; здесь только страховка на непредвиденную ошибку. Без неё любое
    исключение, не перечисленное в _sync_case (IntegrityError на коммите дела, недоступная
    БД внутри записи истории), оставляло бы задачу в RUNNING навсегда: терминальный статус
    ставится только явными вызовами, и никто её потом не подберёт. Хуже того, RUNNING
    считается активным статусом (SearchTaskRepository.get_active_by_uid), поэтому такая
    задача навсегда блокирует повторный запрос этого УИД через API.
    """
    try:
        _sync_case(self, task_id)
    except Retry:
        # Штатный ретрай (self.retry бросает Retry — наследника Exception): задача вернётся,
        # статус RUNNING сохраняется осознанно. Без этой ветки каждый ретрай уходил бы в FAILED.
        raise
    except Exception as exc:
        _mark_failed(task_id, f"Непредвиденная ошибка: {exc}")
        raise  # пробрасываем дальше, чтобы трейс остался в логах воркера


def _sync_case(celery_task, task_id: int) -> None:
    """Тело задачи синхронизации. celery_task — сам таск (нужен для retry)."""
    # 1. Помечаем задачу «в работе» (короткая транзакция) и берём, с чем работать.
    with session_scope() as session:
        repo = SearchTaskRepository(session)
        task = repo.get(task_id)
        if task is None:
            return  # задача удалена — делать нечего
        repo.mark_running(task)
        uid = task.uid
        source_url = task.source_url

    # Под каким именем класть страницы в S3. Пока дело пришло ссылкой и УИД неизвестен,
    # имени из УИД нет — берём его из адреса, иначе страницу отказа некуда положить.
    label = uid or url_label(source_url)

    # Куда клиент суда будет сообщать о расходах на капчу. Номер ретрая берём заранее:
    # в отчёте по нему видно, что до дела пришлось идти несколько раз.
    on_captcha = _captcha_recorder(
        task_id, celery_task.request.retries, source_url=source_url
    )

    # Суд дела, пришедшего ссылкой, известен ещё до похода — по хосту. Резолвим сразу:
    # он всё равно понадобится дальше (как суд карточки, в истории парсинга и в учёте
    # капчи), а заодно незачем тратить прокси и капчу на портал, суда которого нет в
    # справочнике. Эндпоинт это уже проверял, но задача приходит и от enqueue_case_resync,
    # где такой проверки нет.
    url_court = _court_by_url(source_url) if source_url else None
    if source_url and url_court is None:
        host = urlsplit(source_url).hostname
        _mark_failed(task_id, f"Суда с сайтом {host} нет в справочнике судов")
        return

    # 2. Долгая часть без БД: сходить браузером в суд за карточками дела.
    fetched_at = datetime.utcnow()
    try:
        # Прокси арендуем внутри try: если пул пуст при COURT_PROXY_REQUIRED=1,
        # ProxyUnavailable уйдёт в ветку временных ошибок ниже — задача поретраится,
        # а браузер даже не запустится (на портал не с того IP ходить нельзя).
        proxy = lease_proxy()
        if source_url:
            # Портал без поиска по УИД: карточка открывается прямой ссылкой, а УИД
            # мы узнаём уже из неё. Ссылка — постоянный адрес дела, поэтому и первый
            # разбор, и каждый повторный обход идут этим же путём. Карточка здесь ровно
            # одна: открывается тот адрес, который попросили, таблицы результатов нет.
            logger.info("Дело по ссылке %s: идём через прокси %s", source_url, proxy or "напрямую")
            client = define_court_by_url(
                source_url, proxy=proxy, on_captcha_attempt=on_captcha
            )
            html = client.fetch_case_html_by_url(source_url)
            uid = client.extract_uid(html)
            label = uid
            # Первые 8 символов УИД — код суда. Сверяем с тем, который определили по
            # ссылке: расхождение означает, что ссылка ведёт не туда, куда мы решили,
            # либо на портале поехала нумерация участков. Не роняем — дело сохранить
            # всё равно надо, но в логе такое должно быть видно.
            if url_court is not None and uid[:8] != url_court.code:
                logger.warning(
                    "Ссылка %s: суд по ссылке %s, а УИД со страницы указывает на %s",
                    source_url, url_court.code, uid[:8],
                )
            _record_uid(task_id, uid)
            cards = [FetchedCard(code=client.extract_case_code(html), html=html)]
            logger.info("По ссылке %s найдено дело %s", source_url, uid)
        else:
            logger.info("Дело %s: идём через прокси %s", uid, proxy or "напрямую")
            client = define_court_by_uid(uid, proxy=proxy, on_captcha_attempt=on_captcha)
            # Карточек может быть несколько: по одному УИД портал показывает и приказное
            # производство, и последовавшее исковое, иногда в разных участках.
            cards = client.fetch_cases_by_uid(uid)
            logger.info("По УИД %s найдено карточек: %d", uid, len(cards))
        fetched_at = datetime.utcnow()
    except (UnsupportedCourt, CaseNotFound) as exc:
        # Окончательные ошибки — повторять бессмысленно.
        snapshot, page_status = _save_failure_page(label, exc, fetched_at)
        _record_parse_entry(
            label, STATUS_FETCH_ERROR, fetched_at, task_id, snapshot=snapshot,
            error=str(exc), court=url_court,
        )
        # Капчи по дороге были оплачены, даже если карточку мы так и не получили.
        _attach_captcha_costs(task_id, uid, url_court)
        _mark_failed(task_id, str(exc), page_status=page_status)
        return
    except Exception as exc:
        # Временная ошибка (403/timeout/сеть): записать и повторить, пока есть попытки.
        # Запись в историю делаем на каждой попытке — так видно, сколько раз суд не открылся.
        snapshot, page_status = _save_failure_page(label, exc, fetched_at)
        _record_parse_entry(
            label, STATUS_FETCH_ERROR, fetched_at, task_id, snapshot=snapshot,
            error=str(exc), court=url_court,
        )
        _attach_captcha_costs(task_id, uid, url_court)
        # Счётчик попыток проверяем САМИ, до вызова retry: если в retry(exc=...) передан exc,
        # то при исчерпании попыток Celery пробрасывает именно его, а не MaxRetriesExceededError
        # (celery/app/task.py: `if max_retries is not None and retries > max_retries` →
        # `raise_with_context(exc)`). Ловить MaxRetriesExceededError здесь бесполезно — эта
        # ветка не срабатывала, и задача оставалась в RUNNING с исчерпанными попытками.
        if celery_task.request.retries >= celery_task.max_retries:
            _mark_failed(task_id, f"Исчерпаны попытки: {exc}", page_status=page_status)
            return
        _record_error(task_id, str(exc), page_status=page_status)
        raise celery_task.retry(exc=exc, countdown=30)

    # 3. Разбираем и сохраняем каждую найденную карточку.
    #    Отказ на одной карточке не должен уносить остальные: это разные производства,
    #    и то, что у одного поехала разметка, к другому отношения не имеет.
    saved_ids: list[int] = []
    failures: list[str] = []

    for card in cards:
        # 3a. Суд — из того же источника, что и сама карточка: номер участка из строки
        #     таблицы либо хост ссылки. Из УИД он НЕ выводится: у 36 московских судов
        #     номер участка не совпадает с числом в коде суда.
        court = url_court
        if court is None and card.participok_no is not None:
            court = _court_by_participok(uid[:4], card.participok_no)
        if court is None:
            failures.append(
                f"{card.code}: новый суд, требуется завести справочник "
                f"(участок № {card.participok_no})"
            )
            continue

        # 3b. Снапшот HTML — до разбора, чтобы разметка сохранилась даже если парсер упадёт.
        snapshot, html_unchanged = _take_snapshot(
            uid, card.html, fetched_at, court, card.code
        )

        # 3c. Разбор карточки. Ошибка разбора не временная (сломалась разметка или тип
        #     страницы неизвестен) — ретраить нечего.
        try:
            data = client.parse(card.html)  # состав словаря — в CaseParser.parse
        except Exception as exc:
            _record_parse_entry(
                uid, STATUS_PARSE_ERROR, fetched_at, task_id,
                snapshot=snapshot, error=str(exc), html_unchanged=html_unchanged,
                court=court, code=card.code,
            )
            failures.append(f"{card.code}: не удалось разобрать страницу: {exc}")
            continue

        # 3d. Создать/обновить карточку со сверкой судей/сторон/событий.
        with session_scope() as session:
            court_row = CourtRepository(session).get_by_code(court.code)

            # Ссылку, которой завели дело, передаём в разбор: она ляжет в список адресов
            # карточки, и по ней её будут открывать при каждом следующем обходе.
            if source_url:
                data["url"] = source_url

            changes = update_case(session, uid, data, court_row, card.code)
            saved_ids.append(changes.case.id)
            _log_changes(uid, changes)

            # Отмечаем и факт похода, и факт изменения — это разные даты, и обе нужны:
            # по last_checked_at планировщик набирает дела, last_changed_at видит
            # пользователь. updated_at ни на то, ни на другое не годится: ниже
            # дозаписывается diff_history, и строка обновляется на каждом обходе.
            CaseRepository(session).mark_checked(
                changes.case, fetched_at, changed=changes.has_changes()
            )

            # Историю пишем здесь же, до коммита: у удалённых событий и местонахождений
            # атрибуты в этот момент ещё загружены в сессии.
            append_parse_entry(
                changes.case,
                build_entry(
                    status=STATUS_CHANGED if changes.has_changes() else STATUS_NO_CHANGES,
                    fetched_at=fetched_at,
                    task_id=task_id,
                    snapshot=snapshot,
                    diff=changes_to_dict(changes),
                    html_unchanged=html_unchanged,
                ),
            )

    if not saved_ids:
        # Ни одной карточки — задача провалена; в ошибке перечисляем, что помешало.
        _mark_failed(task_id, "; ".join(failures) or "Не сохранено ни одной карточки")
        return
    if failures:
        logger.warning("Дело %s: часть карточек не сохранена (%s)", uid, "; ".join(failures))

    # Капчу разгадывали один раз за заход, а карточек из него вышло, возможно, несколько —
    # расход привязываем к первой сохранённой. Для повторных обходов привязка уже есть, и
    # повторный UPDATE ничего не тронет (привязанные строки не меняем).
    _attach_captcha_costs_to_case(task_id, saved_ids[0])

    # Успех фиксируем отдельной транзакцией (записи карточек уже закоммичены).
    _mark_success(task_id, saved_ids[0])


@celery_app.task
def sync_monitored_cases(
    interval_hours: int | None = None, limit: int | None = None
) -> int:
    """Поставить в очередь переобход всех дел, стоящих на мониторинге.

    Запускается по расписанию (beat_schedule в app/celery_app.py) раз в сутки ночью.
    Днём та же очередь regular нужна не так сильно, а срочные запросы пользователей
    идут через urgent и с ночным обходом не пересекаются.

    Дела берутся не все подряд, а те, которых давно не проверяли (last_checked_at
    старше interval_hours). Так повторный запуск в тот же день не гонит браузер по
    второму разу, а дело, добавленное вчера вечером, не ждёт лишние сутки.

    Задачи ставятся С РАЗНОСОМ ПО ВРЕМЕНИ: один поход в суд занимает 25-35 секунд,
    требует прокси из пула и платной капчи. Веер из сотни дел, выпущенный разом,
    выел бы пул и деньги за минуту и почти наверняка словил бы 429 от портала.

    Возвращает число поставленных в очередь дел.
    """
    hours = MONITORING_INTERVAL_HOURS if interval_hours is None else interval_hours
    batch = MONITORING_BATCH_LIMIT if limit is None else limit

    # interval_hours=0 — «взять все, независимо от даты последней проверки»
    # (нужно для ручного прогона и отладки).
    older_than = datetime.utcnow() - timedelta(hours=hours) if hours > 0 else None

    with session_scope() as session:
        case_ids = CaseRepository(session).list_monitored_ids(older_than, limit=batch)

    if not case_ids:
        logger.info("Дел на мониторинге, требующих обхода, нет")
        return 0

    queued = 0
    for position, case_id in enumerate(case_ids):
        task_id = enqueue_case_resync(
            case_id,
            queue="regular",
            countdown=position * MONITORING_SPACING_SECONDS,
        )
        if task_id is not None:
            queued += 1

    logger.info(
        "Поставлено на обход дел: %d (разнос %d с, весь проход займёт ~%d мин)",
        queued,
        MONITORING_SPACING_SECONDS,
        queued * MONITORING_SPACING_SECONDS // 60,
    )
    return queued


def enqueue_case_resync(
    case_id: int, queue: str = "regular", countdown: int = 0
) -> int | None:
    """Поставить дело на повторный парсинг по его id в БД.

    sync_case принимает id ЗАДАЧИ, а не дела, поэтому задачу надо сначала создать —
    этим и занимается функция. Возвращает id созданной задачи (по нему можно следить
    через GET /search_case/tasks/{task_id}) или None, если дела с таким id нет.

    Задачу заводим тем же способом, каким дело попало в систему: есть сохранённая
    ссылка — идём по ней (у таких порталов поиска по УИД нет), иначе по УИД.

    Обратите внимание: задача по УИД обойдёт ВСЕ карточки этого УИД, а не только ту, ради
    которой её завели, — поиск на портале отдаёт их одной таблицей. Это не лишняя работа:
    страница всё равно одна, а соседние производства заодно обновятся.

    Дедупликации по активным задачам здесь намеренно нет: задача, воркер которой умер
    жёстко, навсегда остаётся в статусе RUNNING, и такая проверка заблокировала бы
    ручной перезапуск дела совсем. Лишний парсинг — меньшее зло.

    Очередь по умолчанию regular: ручной прогон не должен вытеснять срочные запросы
    пользователей из urgent.

    countdown — на сколько секунд отложить старт. Нужен ночному обходу, чтобы
    разнести походы в суд во времени (см. sync_monitored_cases).
    """
    with session_scope() as session:
        case = session.get(Case, case_id)
        if case is None:
            logger.info("Дело id=%s не найдено — повторный парсинг не запущен", case_id)
            return None
        source_url = CaseRepository(session).primary_url(case)
        if source_url:
            task_id = SearchTaskRepository(session).create(source_url=source_url).id
        else:
            task_id = SearchTaskRepository(session).create(uid=case.uid).id

    # apply_async только ПОСЛЕ коммита: иначе воркер может схватить задачу раньше,
    # чем строка появится в БД (тот же порядок, что в app/api/routes.py).
    sync_case.apply_async(args=[task_id], queue=queue, countdown=countdown)
    return task_id
