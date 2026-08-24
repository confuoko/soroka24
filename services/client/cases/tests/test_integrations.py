"""Интеграция с core: границы, таймауты и то, что отказ — не авария.

Проверяется НЕ работа requests, а решения этого модуля: какие коды ответа считать
осмысленными, что делать с непонятным ответом и почему недоступность core не должна
ронять вызывающего.

Главный тест — test_422_is_a_valid_answer_not_a_failure. `POST /search_case` отдаёт 422 с
ПОЛЕЗНЫМ телом («не тот формат УИД», «этот портал не поддержан»), и трактовать его как
аварию значило бы показать пользователю «сервис недоступен» вместо внятного объяснения,
что именно не так с его ссылкой.
"""
from unittest.mock import Mock, patch

import pytest
import requests
from django.test import override_settings

from cases.integrations import core

pytestmark = pytest.mark.django_db


def response(status: int, payload=None, text: str = "") -> Mock:
    mock = Mock()
    mock.status_code = status
    mock.text = text or str(payload)
    if payload is None:
        mock.json.side_effect = ValueError("не JSON")
    else:
        mock.json.return_value = payload
    return mock


# --------------------------------------------------------------- коды ответа
def test_422_is_a_valid_answer_not_a_failure() -> None:
    """422 у /search_case — осмысленный отказ с объяснением, а не авария.

    Тело несёт готовый человеческий текст, который надо показать как есть.
    """
    payload = {"status": "unsupported_court", "message": "Портал пока не поддержан."}

    with patch.object(core._session, "request", return_value=response(422, payload)):
        assert core.search_case("https://x") == payload


def test_202_is_a_valid_answer() -> None:
    """202 — задача заведена, это нормальный ответ на добавление дела."""
    payload = {"status": "processing", "task_id": 77}

    with patch.object(core._session, "request", return_value=response(202, payload)):
        assert core.search_case("https://x")["task_id"] == 77


def test_unexpected_status_is_an_outage() -> None:
    """500 от core — недоступность, а не данные."""
    with patch.object(core._session, "request", return_value=response(500, {}, "boom")):
        with pytest.raises(core.CoreUnavailable):
            core.get_case(481)


def test_422_on_a_read_is_still_an_outage() -> None:
    """А вот у чтения карточки 422 осмысленным не считается.

    Список допустимых кодов задан на КАЖДУЮ ручку отдельно, а не глобально: у /search_case
    422 несёт данные, у /cases/{id} — означает, что мы отправили что-то не то.
    """
    with patch.object(core._session, "request", return_value=response(422, {})):
        with pytest.raises(core.CoreUnavailable):
            core.get_case(481)


def test_non_json_is_an_outage() -> None:
    """Ответ не JSON (страница ошибки от прокси) — тоже недоступность."""
    with patch.object(core._session, "request", return_value=response(200, None, "<html>")):
        with pytest.raises(core.CoreUnavailable):
            core.get_case(481)


def test_network_error_becomes_core_unavailable() -> None:
    """Таймаут и обрыв превращаются в наше исключение, а не текут наружу как requests'ы.

    Вызывающему незачем знать, что мы ходим requests: сменим клиент — обработка отказов
    во views останется той же.
    """
    with patch.object(
        core._session, "request", side_effect=requests.Timeout("слишком долго")
    ):
        with pytest.raises(core.CoreUnavailable):
            core.get_case(481)


# ------------------------------------------------------------------- запросы
def test_summaries_are_asked_in_one_request() -> None:
    """Витрины идут одним запросом со списком id через запятую."""
    payload = [{"id": 1}, {"id": 2}]

    with patch.object(core._session, "request", return_value=response(200, payload)) as mock:
        core.list_case_summaries([2, 1, 2])

    assert mock.call_args.args == ("GET", f"{core.settings.CORE_API_URL}/cases")
    # Дубли склеены, порядок нормализован: серверу всё равно, а лишний трафик ни к чему.
    assert mock.call_args.kwargs["params"] == {"ids": "1,2"}


def test_empty_summary_request_never_leaves_the_process() -> None:
    """Спрашивать core про ноль дел незачем — не спрашиваем.

    Иначе пустая страница «мои дела» стоила бы сетевой запрос на каждый показ.
    """
    with patch.object(core._session, "request") as mock:
        assert core.list_case_summaries([]) == []

    mock.assert_not_called()


def test_summaries_by_id_indexes_the_answer() -> None:
    payload = [{"id": 10, "code": "a"}, {"id": 17, "code": "b"}]

    with patch.object(core._session, "request", return_value=response(200, payload)):
        assert core.summaries_by_id([10, 17]) == {10: payload[0], 17: payload[1]}


def test_monitoring_list_is_sorted_and_deduplicated() -> None:
    """В core уезжает список без дублей.

    core их и сам склеит, но полагаться на это значит держать защиту в чужом сервисе.
    """
    with patch.object(
        core._session, "request", return_value=response(200, {"monitored": 2})
    ) as mock:
        core.replace_monitored_cases([17, 10, 17])

    assert mock.call_args.kwargs["json"] == {"case_ids": [10, 17]}
    # force не передан — значит пустой список core отклонит, и это то, что нужно.
    assert mock.call_args.kwargs["params"] is None


def test_force_is_passed_when_asked() -> None:
    """Пустой список с force проходит: пользователь правда отписался от всего."""
    with patch.object(
        core._session, "request", return_value=response(200, {"monitored": 0})
    ) as mock:
        core.replace_monitored_cases([], force=True)

    assert mock.call_args.kwargs["params"] == {"force": "true"}


# ------------------------------------------------------------------ настройки
@override_settings(CORE_API_URL="http://core-v2-api:8000/")
def test_trailing_slash_in_the_base_url_does_not_double() -> None:
    """Слэш в конце CORE_API_URL не даёт //cases: адрес собирается предсказуемо."""
    with patch.object(core._session, "request", return_value=response(200, {})) as mock:
        core.get_case(481)

    assert mock.call_args.args[1] == "http://core-v2-api:8000/cases/481"


def test_every_request_carries_a_timeout() -> None:
    """Таймаут передаётся всегда.

    Запрос без таймаута висит вечно: воркер gunicorn занят, страница у пользователя не
    отвечает, и виноват при этом соседний сервис.
    """
    with patch.object(core._session, "request", return_value=response(200, {})) as mock:
        core.get_case(481)

    assert mock.call_args.kwargs["timeout"] == core.settings.CORE_API_TIMEOUT


def test_409_is_a_refusal_not_an_outage() -> None:
    """409 на список мониторинга — отказ core, а не его недоступность.

    Разница не педантизм: единственная причина такого отказа — пустой список без force,
    то есть срабатывание защиты от НАШЕЙ аварии. Свалив это в CoreUnavailable, мы бы
    отправили того, кто разбирается, искать проблему в соседнем сервисе, тогда как искать
    надо у себя: не собрался queryset.
    """
    payload = {"detail": "empty case_ids would unmonitor 3 cases"}

    with patch.object(core._session, "request", return_value=response(409, payload)):
        with pytest.raises(core.MonitoringRefused, match="unmonitor"):
            core.replace_monitored_cases([])


def test_refusal_is_not_swallowed_as_success() -> None:
    """sync_monitoring на отказе возвращает None, а не делает вид, что всё прошло."""
    from cases import monitoring

    payload = {"detail": "empty case_ids would unmonitor 3 cases"}
    with patch.object(core._session, "request", return_value=response(409, payload)):
        assert monitoring.sync_monitoring() is None
