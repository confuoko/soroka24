"""Клиент сервиса распознавания капчи rucaptcha.com.

Работает по их API v2: создаём задачу с картинкой в base64, потом опрашиваем
результат, пока сервис не ответит «готово». Разгадывают живые люди, поэтому ответ
приходит не мгновенно — обычно 10-30 секунд.

Ходим стандартным urllib, без внешних библиотек: в проекте намеренно нет HTTP-клиента
(вся сеть идёт через браузер), и тащить зависимость ради двух POST-запросов незачем.

Каждая разгадка стоит денег, поэтому наружу отдаём не только текст ответа, но и
стоимость: сервис присылает её в ответе getTaskResult (поле cost). Считать расход
постфактум по количеству капч нельзя — цена плавает от нагрузки сервиса.

Важно: сюда ходим НАПРЯМУЮ, а не через прокси суда. Это посторонний сервис, заворачивать
его в релей ни к чему, а лишний прыжок только добавил бы точку отказа.
"""
import base64
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

from app.config import CAPTCHA_LANGUAGE_POOL, CAPTCHA_TIMEOUT, RUCAPTCHA_API_KEY

logger = logging.getLogger(__name__)

API_URL = "https://api.rucaptcha.com"

# Имя сервиса в учёте расходов: сейчас решатель один, но платить придётся и другим.
PROVIDER = "rucaptcha"
# Валюта баланса личного кабинета. В ответе сервиса её нет, поэтому фиксируем здесь.
CURRENCY = "RUB"

# Сколько ждать ответа самого API на один запрос (не путать с ожиданием разгадки).
HTTP_TIMEOUT = 30
# Пауза перед первым опросом результата: раньше разгадывать просто не успевают.
FIRST_POLL_DELAY = 5
# Интервал между последующими опросами.
POLL_INTERVAL = 5

# Исходы одной попытки разгадать капчу (поле status у CaptchaAttempt).
ATTEMPT_SOLVED = "solved"    # ответ получен, деньги списаны
ATTEMPT_TIMEOUT = "timeout"  # не дождались; сервис мог решить позже и всё равно списать


class CaptchaError(RuntimeError):
    """Капчу разгадать не удалось: сервис отказал, не уложился в срок или нет ключа."""


@dataclass(frozen=True)
class CaptchaAttempt:
    """Одна попытка разгадать капчу — то, что нужно для учёта расходов.

    Фиксированного тарифа у сервиса нет: цена плавает от нагрузки, поэтому посчитать
    расход постфактум по количеству капч нельзя. Единственный источник истины — поле
    cost в ответе getTaskResult, и забрать его можно только здесь и сейчас.

    cost — Decimal, а не float: это деньги. None означает «цена неизвестна»: сервис
    поля не прислал или мы не дождались ответа. В отчётах такие строки надо считать
    отдельно, а не подменять нулём — иначе расход выглядит меньше, чем он есть.

    captcha_key и attempt_no заполняет клиент суда (dataclasses.replace): сам решатель
    не знает ни про S3, ни про то, какая это по счёту проверка за поход.
    """

    task_id: int
    status: str
    provider: str = PROVIDER
    currency: str = CURRENCY
    text: Optional[str] = None
    cost: Optional[Decimal] = None
    solve_count: Optional[int] = None
    requested_at: Optional[datetime] = None
    ready_at: Optional[datetime] = None
    captcha_key: Optional[str] = None
    attempt_no: Optional[int] = None

    @property
    def latency_ms(self) -> Optional[int]:
        """Сколько заняла разгадка (мс) — или None, если одна из отметок неизвестна."""
        if self.requested_at is None or self.ready_at is None:
            return None
        return int((self.ready_at - self.requested_at).total_seconds() * 1000)


# Кому отдавать запись о попытке. Возвращать ничего не нужно.
AttemptSink = Callable[[CaptchaAttempt], None]


def _parse_cost(body: dict) -> Optional[Decimal]:
    """Достать стоимость решения из ответа сервиса.

    Через str: cost приходит строкой («0.00299»), но даже если сервис пришлёт число,
    Decimal(str(...)) не даст двоичной погрешности. Что угодно неожиданное — None:
    из-за поля учёта отказываться от уже оплаченной разгадки было бы глупо.
    """
    raw = body.get("cost")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        logger.warning("Сервис вернул непонятную стоимость капчи: %r", raw)
        return None


def _report(sink: Optional[AttemptSink], attempt: CaptchaAttempt) -> None:
    """Отдать запись о попытке в учёт, чем бы он ни был.

    Ошибку учёта глотаем: страница дела дороже строки в отчёте, и падать здесь —
    значит потерять уже оплаченную разгадку.
    """
    if sink is None:
        return
    try:
        sink(attempt)
    except Exception as exc:
        logger.warning("Не удалось записать расход на капчу %s: %s", attempt.task_id, exc)


def _post(method: str, payload: dict) -> dict:
    """Отправить JSON в метод API и вернуть разобранный ответ.

    Ошибку уровня API (errorId != 0) превращаем в CaptchaError сразу здесь, чтобы
    вызывающий код не разбирал коды руками.
    """
    request = urllib.request.Request(
        f"{API_URL}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise CaptchaError(f"{method}: сервис недоступен ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise CaptchaError(f"{method}: сервис ответил не JSON") from exc

    if body.get("errorId"):
        code = body.get("errorCode", "?")
        description = body.get("errorDescription", "")
        raise CaptchaError(f"{method}: сервис вернул ошибку {code} — {description}")
    return body


def _require_key() -> str:
    if not RUCAPTCHA_API_KEY:
        raise CaptchaError(
            "Не задан RUCAPTCHA_API_KEY — разгадывать капчу нечем. "
            "Пропишите ключ в .env и прокиньте его в контейнер."
        )
    return RUCAPTCHA_API_KEY


def solve_image(png: bytes, on_attempt: Optional[AttemptSink] = None) -> CaptchaAttempt:
    """Разгадать картинку с капчей. Возвращает запись о попытке с текстом ответа.

    on_attempt — куда сообщить о расходе. Зовём его в каждом исходе, где задача у
    сервиса УЖЕ СОЗДАНА (разгадали или не дождались): начиная с этого момента деньги
    могут быть списаны. На отказе createTask (нет ключа, пустой баланс, сервис лёг)
    не зовём — задачи нет, платить не за что.
    """
    key = _require_key()

    created = _post(
        "createTask",
        {
            "clientKey": key,
            "task": {"type": "ImageToTextTask", "body": base64.b64encode(png).decode()},
            # Капча на порталах судов набрана КИРИЛЛИЦЕЙ. Без русского пула её отдают
            # исполнителям с латинской раскладкой, и ответ приходит транслитом —
            # проверено на живых делах: три попытки подряд впустую.
            "languagePool": CAPTCHA_LANGUAGE_POOL,
        },
    )
    task_id = created["taskId"]
    requested_at = datetime.utcnow()
    logger.debug("Капча отправлена на распознавание, задача %s", task_id)

    deadline = time.monotonic() + CAPTCHA_TIMEOUT
    time.sleep(FIRST_POLL_DELAY)
    while True:
        result = _post("getTaskResult", {"clientKey": key, "taskId": task_id})
        if result.get("status") == "ready":
            answer = result["solution"]["text"]
            attempt = CaptchaAttempt(
                task_id=task_id,
                status=ATTEMPT_SOLVED,
                text=answer,
                cost=_parse_cost(result),
                solve_count=result.get("solveCount"),
                requested_at=requested_at,
                ready_at=datetime.utcnow(),
            )
            logger.debug(
                "Капча разгадана: задача %s, ответ из %d симв., стоимость %s",
                task_id, len(answer), attempt.cost,
            )
            _report(on_attempt, attempt)
            return attempt
        if time.monotonic() >= deadline:
            # Учёт ведём и здесь: мы не дождались, но исполнитель мог сдать ответ
            # секундой позже, и деньги всё равно списались бы. Цена такой попытки нам
            # неизвестна (cost приходит только вместе с решением), поэтому None.
            _report(
                on_attempt,
                CaptchaAttempt(
                    task_id=task_id,
                    status=ATTEMPT_TIMEOUT,
                    requested_at=requested_at,
                    ready_at=datetime.utcnow(),
                ),
            )
            raise CaptchaError(
                f"Капчу не разгадали за {CAPTCHA_TIMEOUT} с (задача {task_id})"
            )
        time.sleep(POLL_INTERVAL)


def report_incorrect(task_id: int) -> None:
    """Пожаловаться, что ответ не подошёл: сервис вернёт деньги и учтёт в статистике.

    НАМЕРЕННО НЕ ВЫЗЫВАЕТСЯ в рабочем пути. Единственный момент, когда это выглядело бы
    уместным, — когда после ввода ответа снова показали капчу. Но портал умеет показать
    вторую капчу и на ПРАВИЛЬНО разгаданную первую, так что жалоба била бы по верным
    ответам. Метод оставлен на случай, если появится способ достоверно отличить
    неверный ответ от повторной проверки.
    """
    _post("reportIncorrect", {"clientKey": _require_key(), "taskId": task_id})
