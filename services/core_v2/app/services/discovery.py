"""Два входа в один и тот же путь сохранения.

    discover_case(uid=…)        дело ещё неизвестно, пришло УИД
    discover_case(source_url=…) дело ещё неизвестно, пришло ссылкой
    resync_case(case_id)        дело уже есть, берём его сохранённый источник

Дальше все три идут ОДНОЙ дорогой:

    клиент суда → FetchedCard → суд + УИД + номер дела → get_parser → ParsedCase
                                                                          ↓
                                                        sync_case → PostgreSQL

Отдельного «refresh» рядом с «sync» здесь нет и быть не должно. Discovery и повторный
обход различаются ровно тем, ОТКУДА взялся адрес или УИД; всё, что происходит после,
у них общее. Поэтому обе функции лежат в одном файле, рядом: так видно, что путь один,
а не два похожих.

Чего здесь НЕТ намеренно:

* **Celery.** Ни retry, ни countdown, ни обращения к задаче. Функции просто падают с
  исключением, а решать, повторять ли поход, — дело того, кто их вызвал. Отличать
  временный отказ от окончательного помогает is_terminal().
* **SearchTask.** Запись о задаче — это наблюдаемая снаружи ручка на асинхронную работу,
  а не часть обхода. Её ведёт слой входных точек.
* **Пользователи и мониторинг.** Кто и зачем следит за делом, core не знает.

В старом core всё перечисленное лежало в одной функции `_sync_case` на 190 строк внутри
Celery-задачи, вместе с retry и учётом капчи.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.config import HTML_SNAPSHOT_ENABLED
from app.courts import (
    CaseNotFound,
    FetchedCard,
    UnsupportedCourt,
    define_court_by_uid,
    define_court_by_url,
    portal_for,
)
from app.database import session_scope
from app.models import Case
from app.integration_events import to_integration_events
from app.outbox import changes_to_events
from app.parsers import UnsupportedPage, get_parser
from app.repositories import (
    CaseRepository,
    CourtRepository,
    IntegrationOutboxRepository,
    OutboxEventRepository,
)
from app.services.case_sync import CaseChanges, sync_case
from app.services.identity import resolve_case_code, resolve_case_uid
from app.services.proxy_pool import lease_proxy
from app.storage.html_snapshots import card_folder, save_snapshot
from app.validators import is_synthetic_uid

logger = logging.getLogger(__name__)

# Отказы, повторять которые бессмысленно: портал не поддержан, карточки на странице нет,
# вёрстку разобрать нечем. Со второго захода страница будет ровно та же.
TERMINAL_ERRORS = (UnsupportedCourt, CaseNotFound, UnsupportedPage)


def is_terminal(exc: BaseException) -> bool:
    """Этот отказ окончательный или стоит прийти ещё раз (и с другого прокси)?

    Знание живёт здесь одной строкой, чтобы вызывающие не растаскивали таксономию
    ошибок по своим except-ветвям и не разъезжались в ней.
    """
    return isinstance(exc, TERMINAL_ERRORS)


def page_status_of(exc: BaseException) -> int | None:
    """HTTP-статус страницы, на которой упали (или None, если снимка нет).

    Снимок приходит приложенным к исключению клиента суда: живой браузер есть только
    внутри клиента, здесь его уже нет. По статусу потом видно, отказал портал (403) или
    упало раньше.
    """
    page = getattr(exc, "page", None)
    return page.status if page is not None else None


@dataclass(frozen=True)
class CourtRef:
    """Суд, вынутый из сессии: только id и код.

    Объект Court живёт в своей session_scope и за её пределами уже недоступен, а суд
    нужен и дальше — в ключе снимка страницы и при поиске карточки. Тащить ради этого
    открытую сессию через весь обход (а в середине его браузер и полминуты сети) незачем:
    id и кода хватает.
    """

    id: int
    code: str


@dataclass
class CrawlResult:
    """Что вышло из одного захода на портал.

    saved_case_ids пуст, а failures нет — значит дошли до портала, но ни одной карточки
    сохранить не смогли. Это отказ, и вызывающий должен его так и трактовать.

    Карточек в одном заходе может быть несколько: по одному УИД портал показывает и
    приказное производство, и последовавшее исковое, иногда в разных участках.
    """

    uid: str | None = None
    court: CourtRef | None = None
    saved_case_ids: list[int] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    fetched_at: datetime | None = None
    captchas_solved: int = 0

    def is_success(self) -> bool:
        return bool(self.saved_case_ids)


# --------------------------------------------------------------------------- входы
def discover_case(
    uid: str | None = None,
    source_url: str | None = None,
    on_captcha=None,
    on_uid=None,
) -> CrawlResult:
    """Найти дело на портале и сохранить его карточки.

    Ровно один из uid / source_url обязателен — они соответствуют двум разным устройствам
    порталов, а не двум способам сделать одно и то же:

    * **source_url** — у портала нет поиска, зато карточка открывается прямым адресом
      (msudrf.ru, Петербург). Карточка ровно одна: открывается тот адрес, который
      попросили, таблицы результатов нет. УИД узнаём уже из страницы.
    * **uid** — у портала есть поиск по УИД (mos-sud.ru). Карточек может выйти несколько:
      поиск отдаёт таблицу.

    on_captcha — куда сообщать о каждой оплаченной капче. Сам обход в учёт не лезет: он
    только зовёт колбэк, а что с ним делать, знает вызывающий (у него есть БД и номер
    задачи, у обхода — нет).

    on_uid — зовётся, как только УИД стал известен, ДО разбора и сохранения. Нужен затем,
    чтобы УИД был виден снаружи даже если дальше всё упадёт: у ссылочной ветки он
    узнаётся только со страницы, и потерять его вместе с отказом обидно. Тот же приём,
    что с on_captcha: обход сообщает факт, а записывает его вызывающий.

    Падает окончательным исключением, если до карточек дойти не удалось; отличить
    окончательный отказ от временного помогает is_terminal(). Отказ на ОДНОЙ карточке
    из нескольких исключением не считается: он попадает в CrawlResult.failures, потому
    что это разные производства и поехавшая разметка одного к другому отношения не имеет.
    """
    if not uid and not source_url:
        raise ValueError("Нужен либо УИД дела, либо ссылка на его карточку")

    # Суд дела, пришедшего ссылкой, известен ЕЩЁ ДО похода — по хосту. Резолвим сразу:
    # он всё равно понадобится дальше, а заодно незачем тратить прокси и платную капчу
    # на портал, суда которого нет в справочнике.
    url_court = _court_by_url(source_url) if source_url else None
    if source_url and url_court is None:
        host = urlsplit(source_url).hostname
        raise UnsupportedCourt(f"Суда с сайтом {host} нет в справочнике судов")

    portal = portal_for(uid=uid, url=source_url)
    # Портал вычисляем ДО аренды прокси: до разных порталов доходят разные адреса, и пул
    # должен выдать годный. Клиента суда на этот момент ещё нет — именно поэтому
    # portal_for ищет класс, а не создаёт экземпляр.
    proxy = lease_proxy(portal=portal)

    if source_url:
        logger.info(
            "Дело по ссылке %s: идём через прокси %s", source_url, proxy or "напрямую"
        )
        client = define_court_by_url(
            source_url, proxy=proxy, on_captcha_attempt=on_captcha
        )
        cards = [client.fetch_card_by_url(source_url)]
    else:
        logger.info("Дело %s: идём через прокси %s", uid, proxy or "напрямую")
        client = define_court_by_uid(uid, proxy=proxy, on_captcha_attempt=on_captcha)
        cards = client.fetch_cases_by_uid(uid)
        logger.info("По УИД %s найдено карточек: %d", uid, len(cards))

    fetched_at = datetime.now(timezone.utc)

    if source_url:
        # Номер дела достаём ПЕРВЫМ: он и есть доказательство, что открылась карточка, а
        # не страница «дело снято с публикации» или поехавшая разметка. Раньше эту роль
        # играл УИД, но его на карточке может не быть вовсе.
        card_code = resolve_case_code(portal or "", cards[0])
        with session_scope() as session:
            uid = resolve_case_uid(session, cards[0].html, source_url, url_court.code)
        _warn_if_uid_points_elsewhere(source_url, uid, url_court)
        if on_uid is not None:
            on_uid(uid)
        # Номер, добытый со страницы, кладём в карточку — дальше он берётся оттуда.
        cards = [_with_case_code(cards[0], card_code)]
        logger.info("По ссылке %s найдено дело %s", source_url, uid)

    return _save_cards(
        cards,
        uid=uid,
        portal=portal or "",
        url_court=url_court,
        source_url=source_url,
        fetched_at=fetched_at,
    )


def resync_case(case_id: int, on_captcha=None, on_uid=None) -> CrawlResult:
    """Обойти УЖЕ известное дело ещё раз.

    Не мониторинг. Кто и когда решает, что пора идти снова, core не знает — он умеет
    только «сходи по этому делу сейчас».

    Источник берём тот, каким дело попало в систему: есть сохранённая ссылка — идём по
    ней (у таких порталов поиска по УИД нет), иначе по УИД. Дальше — тот же путь, что у
    discover_case, вплоть до того же sync_case.

    Обратите внимание: обход по УИД пройдёт ВСЕ карточки этого УИД, а не только ту, ради
    которой его завели, — поиск на портале отдаёт их одной таблицей. Это не лишняя
    работа: страница всё равно одна, а соседние производства заодно обновятся.
    """
    with session_scope() as session:
        case = session.get(Case, case_id)
        if case is None:
            raise CaseNotFound(f"Дела id={case_id} нет в базе")
        source_url = CaseRepository(session).primary_url(case)
        uid = case.uid

    if source_url:
        return discover_case(
            source_url=source_url, on_captcha=on_captcha, on_uid=on_uid
        )
    return discover_case(uid=uid, on_captcha=on_captcha, on_uid=on_uid)


# ------------------------------------------------------------------ общая часть
def _save_cards(
    cards: list[FetchedCard],
    uid: str,
    portal: str,
    url_court: CourtRef | None,
    source_url: str | None,
    fetched_at: datetime,
) -> CrawlResult:
    """Разобрать и сохранить каждую найденную карточку.

    Отказ на одной карточке не уносит остальные: это разные производства, и то, что у
    одного поехала разметка, к другому отношения не имеет.
    """
    result = CrawlResult(uid=uid, court=url_court, fetched_at=fetched_at)
    result.captchas_solved = sum(card.captchas_solved for card in cards)

    for card in cards:
        # Суд — из того же источника, что и сама карточка: номер участка из строки
        # таблицы либо хост ссылки. Из УИД он НЕ выводится: у 36 московских судов номер
        # участка не совпадает с числом в коде суда (участок № 463 — это код 77MS0466,
        # а 77MS0463 — совсем другой суд), так что собрать код арифметикой нельзя.
        court = url_court
        if court is None and card.participok_no is not None:
            court = _court_by_participok(uid[:4], card.participok_no)
        if court is None:
            result.failures.append(
                f"{card.case_code}: новый суд, требуется завести справочник "
                f"(участок № {card.participok_no})"
            )
            continue

        if card.case_code is None:
            # До сюда дойти не должно: у ссылочной ветки номер подставлен выше, у
            # поисковой он приходит из таблицы результатов.
            result.failures.append("карточка без номера дела — сохранять нечего")
            continue

        # Архив разметки — ДО разбора, чтобы страница сохранилась даже если парсер упадёт.
        _take_snapshot(uid, card.html, fetched_at, court, card.case_code)

        try:
            parsed = get_parser(portal, card.html).parse(card.html)
        except Exception as exc:
            # Отказ разбора не временный: поехала разметка либо вёрстка неизвестна.
            result.failures.append(
                f"{card.case_code}: не удалось разобрать страницу: {exc}"
            )
            continue

        if parsed.is_empty():
            # Пустой разбор НЕ сохраняем. Страница считается источником истины, поэтому
            # у уже существующей карточки такой разбор удалил бы все события и отвязал
            # судей со сторонами — смена разметки на портале молча вымела бы историю дела.
            error = "разбор не дал ни одного поля — похоже, у портала другая разметка"
            logger.warning("Дело %s, карточка %s: %s", uid, card.case_code, error)
            result.failures.append(f"{card.case_code}: {error}")
            continue

        with session_scope() as session:
            court_row = CourtRepository(session).get_by_code(court.code)
            changes = sync_case(
                session,
                uid,
                parsed,
                court_row,
                card.case_code,
                # Ссылку, которой завели дело, передаём в сверку: она ляжет в список
                # адресов карточки, и по ней её будут открывать при каждом обходе.
                source_url=source_url,
            )
            result.saved_case_ids.append(changes.case.id)
            _log_changes(uid, changes)

            # Отмечаем и факт похода, и факт изменения — это разные даты, и обе нужны.
            # updated_at ни на то, ни на другое не годится: строку трогает любой обход,
            # в том числе холостой.
            CaseRepository(session).mark_checked(
                changes.case, fetched_at, changed=changes.has_changes()
            )

            # События об изменениях — здесь же, в ЭТОЙ транзакции. В том и смысл
            # Transactional Outbox: изменение карточки и факт события коммитятся вместе,
            # поэтому событие не может ни потеряться, ни появиться без изменения.
            #
            # И до коммита, а не после: у удалённых событий и местонахождений атрибуты
            # сейчас ещё загружены в сессию, а после коммита читать их было бы нечем.
            domain_events = changes_to_events(changes)
            OutboxEventRepository(session).emit(changes.case, domain_events)

            # Те же изменения — вторым, публичным представлением, и снова в ЭТОЙ
            # транзакции. Дублирование сознательное: payload домен-лога меняется вместе со
            # сверкой, а клиентский сервис от таких правок ломаться не должен
            # (см. app/models/integration_outbox.py).
            #
            # ПОСЛЕ emit, и это важно: он флашит, и только после флаша у новых событий,
            # заседаний и документов появляются id. Позвать раньше — получить пустой
            # entity_id у всех новых сообщений, причём молча.
            IntegrationOutboxRepository(session).emit(
                to_integration_events(changes.case.id, domain_events)
            )

    if not result.saved_case_ids:
        logger.warning(
            "Дело %s: не сохранено ни одной карточки (%s)",
            uid,
            "; ".join(result.failures) or "причина неизвестна",
        )
    elif result.failures:
        logger.warning(
            "Дело %s: часть карточек не сохранена (%s)", uid, "; ".join(result.failures)
        )
    return result


def _with_case_code(card: FetchedCard, case_code: str) -> FetchedCard:
    """Копия карточки с проставленным номером дела (FetchedCard неизменяем)."""
    return replace(card, case_code=case_code)


def _warn_if_uid_points_elsewhere(
    source_url: str, uid: str, url_court: CourtRef
) -> None:
    """Сверить код суда из УИД с тем, который определили по ссылке.

    Первые 8 символов УИД — код суда. Расхождение означает, что ссылка ведёт не туда,
    куда мы решили, либо на портале поехала нумерация участков. Не роняем — дело
    сохранить всё равно надо, — но в логе такое должно быть видно.

    У самодельного ключа сверять нечего: код суда в него и подставлен из справочника.
    """
    if is_synthetic_uid(uid) or uid[:8] == url_court.code:
        return
    logger.warning(
        "Ссылка %s: суд по ссылке %s, а УИД со страницы указывает на %s",
        source_url,
        url_court.code,
        uid[:8],
    )


def _court_by_url(url: str) -> CourtRef | None:
    """Суд по ссылке на карточку (или None, если его нет в справочнике).

    Обычно хватает хоста (на msudrf.ru у каждого участка свой поддомен), но у порталов
    с одним хостом на весь регион суд ищется по номеру участка из пути — развилку держит
    CourtRepository.get_by_url.
    """
    with session_scope() as session:
        court = CourtRepository(session).get_by_url(url)
        return CourtRef(id=court.id, code=court.code) if court is not None else None


def _court_by_participok(region_code: str, participok_no: int) -> CourtRef | None:
    """Суд по номеру участка из таблицы результатов (или None, если его нет в справочнике)."""
    with session_scope() as session:
        court = CourtRepository(session).get_by_participok(region_code, participok_no)
        return CourtRef(id=court.id, code=court.code) if court is not None else None


def _take_snapshot(
    uid: str, html: str, fetched_at: datetime, court: CourtRef, code: str
) -> None:
    """Положить HTML карточки в S3, если архив разметки включён.

    Архив нужен для отладки парсеров, поэтому по умолчанию выключен. Суд и номер дела
    нужны, чтобы страница легла в папку своей карточки: карточка — это тройка
    «УИД + суд + номер».

    Недоступный S3 не должен ронять обход: дело важнее архива разметки.
    """
    if not HTML_SNAPSHOT_ENABLED:
        return
    try:
        save_snapshot(uid, html, fetched_at, card=card_folder(court.code, code))
    except Exception as exc:
        logger.warning("Не удалось сохранить снимок HTML дела %s в S3: %s", uid, exc)


def _log_changes(uid: str, changes: CaseChanges) -> None:
    """Записать в лог, что изменилось по делу за эту синхронизацию."""
    if not changes.has_changes():
        logger.info("Дело %s: изменений нет", uid)
        return
    for change in changes.field_changes:
        logger.info(
            "Изменилось поле дела %s: %s: %r -> %r",
            uid, change.field, change.old, change.new,
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
    for court_session in changes.new_sessions:
        logger.info("Назначено заседание по делу %s: %s — %s", uid, court_session.session_date, court_session.stage)
    for court_session in changes.updated_sessions:
        logger.info(
            "Изменено заседание по делу %s: %s — %s (результат: %s)",
            uid, court_session.session_date, court_session.stage, court_session.result,
        )
    for court_session in changes.removed_sessions:
        logger.info("Снято заседание по делу %s: %s — %s", uid, court_session.session_date, court_session.stage)
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
