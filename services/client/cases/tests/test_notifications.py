"""Уведомления: выборка «о чём не сообщали», группировка и отметка notified_at.

Канала доставки пока нет — `deliver` пишет в лог. Проверяется поэтому не письмо, а всё
остальное: то, что при подключении настоящего канала переделывать не придётся.

Три теста здесь важнее остальных.

test_one_notification_per_case — ради этого и заведена группировка. Обход находит по
5-8 изменений разом, и уведомление на каждое означало бы восемь писем подряд про одно дело:
ровно то, от чего сервис должен избавлять.

test_failed_delivery_is_retried_next_run — про порядок «сначала доставка, потом отметка».
Отметь мы сначала — упавшая доставка означала бы, что человек не узнает об изменении
никогда: второй раз строка в выборку не попадёт.

test_notified_does_not_touch_read — `notified_at` и `read_at` отвечают на разные вопросы.
Склей их кто-нибудь в одно поле — и «прочитал на сайте» начало бы означать «получил
уведомление».
"""
import datetime as dt
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User

from cases.models import CaseSubscription, UserCaseChange
from cases.notifications import describe, notify_pending

pytestmark = pytest.mark.django_db

CASE_ID = 481
OTHER_CASE_ID = 900
OCCURRED = dt.datetime(2026, 8, 23, 13, 20, tzinfo=dt.timezone.utc)


@pytest.fixture
def natasha():
    return User.objects.create_user("natasha", password="secret123")


@pytest.fixture
def anna():
    return User.objects.create_user("anna", password="secret123")


def subscribe(user, case_id=CASE_ID):
    return CaseSubscription.objects.create(user=user, core_case_id=case_id)


def add_change(subscription, message_id, event_type="event_new", entity_id=712, **kwargs):
    """Строка так, как её создал бы consumer."""
    return UserCaseChange.objects.create(
        user=subscription.user,
        subscription=subscription,
        integration_event_id=message_id,
        event_type=event_type,
        core_entity_id=entity_id,
        occurred_at=OCCURRED,
        **kwargs,
    )


# ------------------------------------------------------------- группировка
def test_one_notification_per_case(natasha) -> None:
    """Восемь изменений по одному делу — ОДНО уведомление, а не восемь."""
    subscription = subscribe(natasha)
    for message_id in range(1, 9):
        add_change(subscription, message_id)

    with patch("cases.notifications.deliver") as deliver:
        assert notify_pending() == 1

    assert deliver.call_count == 1
    user, case_id, changes = deliver.call_args.args
    assert user == natasha
    assert case_id == CASE_ID
    assert len(changes) == 8


def test_different_cases_are_separate_notifications(natasha) -> None:
    """Одному человеку по двум делам — два уведомления: смешивать дела нельзя."""
    add_change(subscribe(natasha), 1)
    add_change(subscribe(natasha, OTHER_CASE_ID), 2)

    with patch("cases.notifications.deliver") as deliver:
        assert notify_pending() == 2

    cases = sorted(call.args[1] for call in deliver.call_args_list)
    assert cases == [CASE_ID, OTHER_CASE_ID]


def test_different_users_are_separate_notifications(natasha, anna) -> None:
    """Одно изменение у двух подписчиков — каждому своё уведомление."""
    add_change(subscribe(natasha), 1)
    add_change(subscribe(anna), 1)

    with patch("cases.notifications.deliver") as deliver:
        assert notify_pending() == 2

    assert sorted(call.args[0].username for call in deliver.call_args_list) == [
        "anna",
        "natasha",
    ]


def test_nothing_to_notify_about(natasha) -> None:
    """Пустая выборка — не ошибка и не повод звать доставку."""
    subscribe(natasha)

    with patch("cases.notifications.deliver") as deliver:
        assert notify_pending() == 0

    deliver.assert_not_called()


# ------------------------------------------------------------ отметка и повтор
def test_delivered_changes_are_stamped(natasha) -> None:
    subscription = subscribe(natasha)
    add_change(subscription, 1)
    add_change(subscription, 2)

    with patch("cases.notifications.deliver"):
        notify_pending()

    assert UserCaseChange.objects.filter(notified_at__isnull=True).count() == 0


def test_second_run_notifies_about_nothing(natasha) -> None:
    """Повторный прогон не сообщает о том же второй раз."""
    add_change(subscribe(natasha), 1)

    with patch("cases.notifications.deliver"):
        assert notify_pending() == 1

    with patch("cases.notifications.deliver") as deliver:
        assert notify_pending() == 0
    deliver.assert_not_called()


def test_new_change_after_a_run_is_notified(natasha) -> None:
    """Пришло новое — сообщаем, хотя по этому делу уже сообщали."""
    subscription = subscribe(natasha)
    add_change(subscription, 1)
    with patch("cases.notifications.deliver"):
        notify_pending()

    add_change(subscription, 2)

    with patch("cases.notifications.deliver") as deliver:
        assert notify_pending() == 1
    assert len(deliver.call_args.args[2]) == 1


def test_failed_delivery_is_retried_next_run(natasha, anna) -> None:
    """Упавшая доставка не теряет уведомление и не мешает остальным.

    Порядок «сначала доставка, потом отметка» именно для этого: отметь мы сначала — упавшая
    группа никогда бы не попала в выборку снова.
    """
    add_change(subscribe(natasha), 1)
    add_change(subscribe(anna), 1)

    # Первому — отказ, второму — успех.
    with patch("cases.notifications.deliver", side_effect=[RuntimeError("SMTP лёг"), None]):
        assert notify_pending() == 1

    # Ровно одна строка осталась неотмеченной — та, чья доставка не прошла.
    assert UserCaseChange.objects.filter(notified_at__isnull=True).count() == 1

    with patch("cases.notifications.deliver") as deliver:
        assert notify_pending() == 1
    assert deliver.call_count == 1
    assert UserCaseChange.objects.filter(notified_at__isnull=True).count() == 0


def test_limit_caps_notifications(natasha) -> None:
    """--limit режет уведомления, а не изменения."""
    for case_id in (CASE_ID, OTHER_CASE_ID, 901):
        add_change(subscribe(natasha, case_id), case_id)

    with patch("cases.notifications.deliver") as deliver:
        assert notify_pending(limit=2) == 2

    assert deliver.call_count == 2
    assert UserCaseChange.objects.filter(notified_at__isnull=True).count() == 1


# ------------------------------------------------------- notified_at vs read_at
def test_notified_does_not_touch_read(natasha) -> None:
    """Уведомили — но человек этого ещё не видел: unread остаётся."""
    add_change(subscribe(natasha), 1)

    with patch("cases.notifications.deliver"):
        notify_pending()

    change = UserCaseChange.objects.get()
    assert change.notified_at is not None
    assert change.read_at is None


def test_already_read_is_still_notified(natasha) -> None:
    """Прочитанное на сайте всё равно попадает в выборку.

    Правило «уже прочитал — не писать» зависит от задержки доставки, которой пока нет:
    решать его надо вместе с настоящим каналом, а не молча зашивать сюда.
    """
    add_change(subscribe(natasha), 1, read_at=OCCURRED)

    with patch("cases.notifications.deliver") as deliver:
        assert notify_pending() == 1
    assert deliver.call_count == 1


# ------------------------------------------------------------------ describe
def test_known_types_are_translated() -> None:
    assert describe("event_new") == "новое событие"
    assert describe("session_new") == "назначено заседание"


def test_unknown_type_survives_as_is() -> None:
    """Незнакомый тип не роняет рассылку и не пропадает молча.

    core может начать публиковать новый тип раньше, чем мы про него узнаем. Показать сырое
    имя честнее, чем промолчать об изменении.
    """
    assert describe("session_moved_to_another_room") == "session_moved_to_another_room"


def test_every_published_type_has_a_description() -> None:
    """Все 16 типов, которые публикует core, переведены.

    Список сверен с INTEGRATION_TYPE_BY_DOMAIN в core_v2. Разойдётся — человек увидит в
    уведомлении сырое `place_updated` вместо человеческих слов.
    """
    from cases.notifications import DESCRIPTIONS

    published = {
        "case_field_changed",
        "event_new", "event_updated", "event_removed",
        "place_new", "place_updated", "place_removed",
        "session_new", "session_updated", "session_removed",
        "document_new", "document_removed",
        "judge_added", "judge_removed",
        "side_added", "side_removed",
    }

    assert published <= set(DESCRIPTIONS)
