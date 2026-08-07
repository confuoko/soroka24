"""Клиент сервиса распознавания капчи: сеть подменена, настоящий rucaptcha не дёргаем.

Проверяем протокол (что и куда отправляется), разбор ответа и поведение на отказах —
всё то, из-за чего в бою можно молча получить не тот результат. Отдельный блок — про
учёт денег: стоимость приходит только вместе с ответом сервиса, второго шанса её
узнать нет.
"""
import json
from decimal import Decimal
from io import BytesIO

import pytest

from app.captcha import rucaptcha
from app.captcha.rucaptcha import (
    ATTEMPT_SOLVED,
    ATTEMPT_TIMEOUT,
    CaptchaError,
    report_incorrect,
    solve_image,
)

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

    solved = solve_image(PNG)

    assert (solved.text, solved.task_id, solved.status) == ("a1b2c", 777, ATTEMPT_SOLVED)
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


# ----------------------------------------------------------------------- учёт денег
def test_cost_comes_back_as_decimal(api) -> None:
    """Стоимость разбираем в Decimal, а не во float: это деньги, и их складывают.

    float дал бы 0.0324 + 0.0324 != 0.0648 при суммировании расходов по делу.
    """
    api(
        {"errorId": 0, "taskId": 1},
        {
            "errorId": 0,
            "status": "ready",
            "solution": {"text": "ok"},
            "cost": "0.0324",
            "solveCount": 2,
        },
    )

    solved = solve_image(PNG)

    assert solved.cost == Decimal("0.0324")
    assert isinstance(solved.cost, Decimal)
    assert solved.solve_count == 2


def test_missing_cost_does_not_lose_the_answer(api) -> None:
    """Сервис не прислал cost → цена неизвестна, но разгадка всё равно уходит наружу.

    Отказываться от уже оплаченной капчи из-за поля учёта было бы глупо.
    """
    api(
        {"errorId": 0, "taskId": 1},
        {"errorId": 0, "status": "ready", "solution": {"text": "ok"}},
    )

    solved = solve_image(PNG)

    assert solved.text == "ok"
    assert solved.cost is None


def test_broken_cost_is_treated_as_unknown(api) -> None:
    """Мусор вместо цены — тоже «неизвестно», а не падение на Decimal."""
    api(
        {"errorId": 0, "taskId": 1},
        {"errorId": 0, "status": "ready", "solution": {"text": "ok"}, "cost": "дорого"},
    )

    assert solve_image(PNG).cost is None


def test_solved_attempt_is_reported(api) -> None:
    """Об успешной разгадке сообщаем в учёт: именно в этот момент списаны деньги."""
    api(
        {"errorId": 0, "taskId": 42},
        {"errorId": 0, "status": "ready", "solution": {"text": "ok"}, "cost": "0.03"},
    )
    reported = []

    solve_image(PNG, on_attempt=reported.append)

    assert len(reported) == 1
    assert reported[0].task_id == 42
    assert reported[0].status == ATTEMPT_SOLVED
    assert reported[0].cost == Decimal("0.03")
    # Латентность считается по отметкам времени и заполняется всегда.
    assert reported[0].latency_ms is not None


def test_timeout_is_reported_with_unknown_cost(api, monkeypatch) -> None:
    """Не дождались — расход всё равно записываем, но цену признаём неизвестной.

    Исполнитель мог сдать ответ секундой позже нашего дедлайна, и деньги списались бы.
    Промолчать здесь — значит систематически занижать расход по делу.
    """
    monkeypatch.setattr(rucaptcha, "CAPTCHA_TIMEOUT", 0)
    api(
        {"errorId": 0, "taskId": 5},
        {"errorId": 0, "status": "processing"},
    )
    reported = []

    with pytest.raises(CaptchaError):
        solve_image(PNG, on_attempt=reported.append)

    assert len(reported) == 1
    assert reported[0].status == ATTEMPT_TIMEOUT
    assert reported[0].cost is None
    assert reported[0].task_id == 5


def test_failed_create_task_is_not_reported(api) -> None:
    """createTask отказал → задачи у сервиса нет, платить не за что, в учёт не пишем."""
    api({"errorId": 1, "errorCode": "ERROR_ZERO_BALANCE", "errorDescription": "Нет денег"})
    reported = []

    with pytest.raises(CaptchaError):
        solve_image(PNG, on_attempt=reported.append)

    assert reported == []


def test_broken_accounting_does_not_break_solving(api) -> None:
    """Упавший учёт не должен стоить нам страницы дела: капча уже оплачена."""
    api(
        {"errorId": 0, "taskId": 1},
        {"errorId": 0, "status": "ready", "solution": {"text": "ok"}},
    )

    def _boom(attempt):
        raise RuntimeError("БД недоступна")

    assert solve_image(PNG, on_attempt=_boom).text == "ok"


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
