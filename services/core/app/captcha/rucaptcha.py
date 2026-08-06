"""Клиент сервиса распознавания капчи rucaptcha.com.

Работает по их API v2: создаём задачу с картинкой в base64, потом опрашиваем
результат, пока сервис не ответит «готово». Разгадывают живые люди, поэтому ответ
приходит не мгновенно — обычно 10-30 секунд.

Ходим стандартным urllib, без внешних библиотек: в проекте намеренно нет HTTP-клиента
(вся сеть идёт через браузер), и тащить зависимость ради двух POST-запросов незачем.

Важно: сюда ходим НАПРЯМУЮ, а не через прокси суда. Это посторонний сервис, заворачивать
его в релей ни к чему, а лишний прыжок только добавил бы точку отказа.
"""
import base64
import json
import logging
import time
import urllib.error
import urllib.request

from app.config import CAPTCHA_LANGUAGE_POOL, CAPTCHA_TIMEOUT, RUCAPTCHA_API_KEY

logger = logging.getLogger(__name__)

API_URL = "https://api.rucaptcha.com"

# Сколько ждать ответа самого API на один запрос (не путать с ожиданием разгадки).
HTTP_TIMEOUT = 30
# Пауза перед первым опросом результата: раньше разгадывать просто не успевают.
FIRST_POLL_DELAY = 5
# Интервал между последующими опросами.
POLL_INTERVAL = 5


class CaptchaError(RuntimeError):
    """Капчу разгадать не удалось: сервис отказал, не уложился в срок или нет ключа."""


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


def solve_image(png: bytes) -> tuple[str, int]:
    """Разгадать картинку с капчей. Возвращает (текст ответа, id задачи в сервисе).

    id задачи нужен только для report_incorrect; в рабочем пути он не используется,
    но возвращать его дешевле, чем потом искать.
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
    logger.debug("Капча отправлена на распознавание, задача %s", task_id)

    deadline = time.monotonic() + CAPTCHA_TIMEOUT
    time.sleep(FIRST_POLL_DELAY)
    while True:
        result = _post("getTaskResult", {"clientKey": key, "taskId": task_id})
        if result.get("status") == "ready":
            answer = result["solution"]["text"]
            logger.debug("Капча разгадана: задача %s, ответ из %d симв.", task_id, len(answer))
            return answer, task_id
        if time.monotonic() >= deadline:
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
