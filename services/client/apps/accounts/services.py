"""Операции над пользователями — то, что вызывают и веб, и бот, и API.

Функции здесь принимают и возвращают доменные объекты и НЕ знают ни про request,
ни про DRF, ни про шаблоны. Именно поэтому их сможет вызвать телеграм-бот, не
поднимая HTTP-слой: у него будет тот же register_user, что и у формы регистрации.
"""
from django.contrib.auth import get_user_model
from django.db import transaction

from apps.accounts.models import Subscription

User = get_user_model()


@transaction.atomic
def register_user(
    *,
    username: str,
    password: str,
    email: str | None = None,
    telegram_id: int | None = None,
) -> User:
    """Создать пользователя вместе с его подпиской.

    Подписка заводится сразу и в той же транзакции: пользователь без подписки —
    состояние, которого в сервисе не бывает, и проверять его потом в каждом месте
    дороже, чем не допустить здесь.
    """
    user = User.objects.create_user(
        username=username,
        password=password,
        email=email or "",
        telegram_id=telegram_id,
    )
    Subscription.objects.create(user=user)
    return user


@transaction.atomic
def get_or_create_telegram_user(telegram_id: int, username: str | None = None) -> User:
    """Найти пользователя по telegram_id или завести нового (задел под бота).

    В телеграме пароля нет, поэтому у такого пользователя он не задан
    (set_unusable_password) — войти на сайт этим аккаунтом нельзя, пока человек
    не привяжет к нему логин с паролем.
    """
    user = User.objects.filter(telegram_id=telegram_id).first()
    if user is not None:
        return user

    user = User(username=username or f"tg_{telegram_id}", telegram_id=telegram_id)
    user.set_unusable_password()
    user.save()
    Subscription.objects.create(user=user)
    return user


def link_telegram(user: User, telegram_id: int) -> User:
    """Привязать телеграм к уже существующему аккаунту."""
    user.telegram_id = telegram_id
    user.save(update_fields=["telegram_id"])
    return user
