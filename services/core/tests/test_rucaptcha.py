"""Клиент сервиса распознавания капчи: сеть подменена, настоящий rucaptcha не дёргаем.

Проверяем протокол (что и куда отправляется), разбор ответа и поведение на отказах —
всё то, из-за чего в бою можно молча получить не тот результат.
"""
import json
from io import BytesIO

import pytest

from app.captcha import rucaptcha
from app.captcha.rucaptcha import CaptchaError, report_incorrect, solve_image

PNG = b"\x89PNG\r\n\x1a\n-fake-image"


class _FakeResponse(BytesIO):
    """Минимальная замена ответу urlopen: нужен только контекст-менеджер и read()."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture
def api(monkeypatch):
    """Подменить сеть. Возвращает настройщик: список ответов по порядку вызовов.

    Заодно копит отправленные запросы, чтобы проверить сам протокол, а не только
    то, что клиент как-то отработал.
    """
    sent = []

    def _install(*responses):
        queue = list(responses)

        def _urlopen(request, timeout=None):
            sent.append(
                {
                    "url": request.full_url,
                    "body": json.loads(request.data.decode("utf-8")),
                    "timeout": timeout,
                }
            )
            return _FakeResponse(json.dumps(queue.pop(0)).encode("utf-8"))

        monkeypatch.setattr(rucaptcha.urllib.request, "urlopen", _urlopen)
        # Ключ и паузы: тест не должен зависеть от .env и не должен спать.
        monkeypatch.setattr(rucaptcha, "RUCAPTCHA_API_KEY", "test-key")
        monkeypatch.setattr(rucaptcha.time, "sleep", lambda _: None)
        return sent

    return _install


# ------------------------------------------------------------------- нормальный путь
def test_solve_image_polls_until_ready(api) -> None:
    """Задача создаётся, результат опрашивается до готовности, наружу идёт разгадка."""
    sent = api(
        {"errorId": 0, "taskId": 777},
        {"errorId": 0, "status": "processing"},
        {"errorId": 0, "status": "ready", "solution": {"text": "a1b2c"}},
    )

    answer, task_id = solve_image(PNG)

    assert (answer, task_id) == ("a1b2c", 777)
    assert len(sent) == 3


def test_create_task_sends_base64_image(api) -> None:
    """Картинка уходит в base64 внутри ImageToTextTask, ключ — в clientKey."""
    sent = api(
        {"errorId": 0, "taskId": 1},
        {"errorId": 0, "status": "ready", "solution": {"text": "ok"}},
    )

    solve_image(PNG)

    create = sent[0]
    assert create["url"].endswith("/createTask")
    assert create["body"]["clientKey"] == "test-key"
    assert create["body"]["task"]["type"] == "ImageToTextTask"
    import base64

    assert base64.b64decode(create["body"]["task"]["body"]) == PNG


def test_create_task_asks_for_russian_solvers(api) -> None:
    """Капча на порталах судов кириллическая — нужен русскоязычный пул исполнителей.

    Регресс на живой прогон: с пулом по умолчанию три ответа подряд оказались
    транслитом и проверку мы не прошли.
    """
    sent = api(
        {"errorId": 0, "taskId": 1},
        {"errorId": 0, "status": "ready", "solution": {"text": "ok"}},
    )

    solve_image(PNG)

    assert sent[0]["body"]["languagePool"] == "rn"


def test_get_task_result_sends_task_id(api) -> None:
    """За результатом ходим по taskId, который вернул createTask."""
    sent = api(
        {"errorId": 0, "taskId": 42},
        {"errorId": 0, "status": "ready", "solution": {"text": "ok"}},
    )

    solve_image(PNG)

    assert sent[1]["url"].endswith("/getTaskResult")
    assert sent[1]["body"] == {"clientKey": "test-key", "taskId": 42}


# --------------------------------------------------------------------------- отказы
def test_api_error_becomes_captcha_error(api) -> None:
    """errorId != 0 → CaptchaError с кодом и описанием от сервиса, а не KeyError."""
    api({"errorId": 1, "errorCode": "ERROR_ZERO_BALANCE", "errorDescription": "Нет денег"})

    with pytest.raises(CaptchaError) as caught:
        solve_image(PNG)

    assert "ERROR_ZERO_BALANCE" in str(caught.value)
    assert "Нет денег" in str(caught.value)


def test_missing_key_fails_without_network(monkeypatch) -> None:
    """Пустой ключ — падаем сразу и внятно, ни одного запроса в сеть.

    Иначе на проде с незаполненным .env мы бы молча получали страницу капчи вместо дела.
    """
    monkeypatch.setattr(rucaptcha, "RUCAPTCHA_API_KEY", "")

    def _boom(*args, **kwargs):
        raise AssertionError("в сеть ходить не должны")

    monkeypatch.setattr(rucaptcha.urllib.request, "urlopen", _boom)

    with pytest.raises(CaptchaError, match="RUCAPTCHA_API_KEY"):
        solve_image(PNG)


def test_timeout_gives_up(api, monkeypatch) -> None:
    """Сервис вечно отвечает processing → сдаёмся по CAPTCHA_TIMEOUT, а не висим."""
    monkeypatch.setattr(rucaptcha, "CAPTCHA_TIMEOUT", 0)
    api(
        {"errorId": 0, "taskId": 5},
        {"errorId": 0, "status": "processing"},
    )

    with pytest.raises(CaptchaError, match="не разгадали"):
        solve_image(PNG)


def test_unreachable_service_becomes_captcha_error(monkeypatch) -> None:
    """Сеть недоступна → CaptchaError, а не голый URLError из глубины urllib."""
    import urllib.error

    monkeypatch.setattr(rucaptcha, "RUCAPTCHA_API_KEY", "test-key")

    def _boom(*args, **kwargs):
        raise urllib.error.URLError("нет сети")

    monkeypatch.setattr(rucaptcha.urllib.request, "urlopen", _boom)

    with pytest.raises(CaptchaError, match="недоступен"):
        solve_image(PNG)


# ------------------------------------------------- метод есть, но в бою не вызывается
def test_report_incorrect_sends_task_id(api) -> None:
    """report_incorrect написан и работает — хотя из рабочего пути его не зовут.

    Не зовут потому, что повторная капча не означает неверный ответ: портал показывает
    вторую проверку и после верно разгаданной первой.
    """
    sent = api({"errorId": 0, "status": "success"})

    report_incorrect(99)

    assert sent[0]["url"].endswith("/reportIncorrect")
    assert sent[0]["body"] == {"clientKey": "test-key", "taskId": 99}
