"""Пользователь и его подписка.

Платежей пока нет: подписка — это только дата начала, от которой считается номер
текущего месяца. Когда появится оплата, сюда добавятся сами платежи, а
started_at останется точкой отсчёта.
"""
import calendar
from datetime import datetime

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """Пользователь сервиса.

    telegram_id заводим сразу, хотя бота ещё нет: у одного человека web и бот —
    один и тот же аккаунт, и добавлять поле в модель пользователя после первой
    миграции заметно неудобнее, чем сейчас.
    """

    telegram_id = models.BigIntegerField(
        "Telegram ID",
        unique=True,
        null=True,
        blank=True,
        db_index=True,
    )

    class Meta:
        verbose_name = "пользователь"
        verbose_name_plural = "пользователи"

    def __str__(self) -> str:
        return self.username


class Subscription(models.Model):
    """Подписка пользователя.

    Одна на пользователя. Месяц подписки считается от started_at, а не по
    календарю: человек, подписавшийся 20-го числа, платит за период с 20-го по
    20-е, и лимиты должны обнуляться тогда же.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="subscription",
        verbose_name="пользователь",
    )
    started_at = models.DateTimeField("начало подписки", default=timezone.now)
    is_active = models.BooleanField("активна", default=True)
    created_at = models.DateTimeField("создана", auto_now_add=True)
    updated_at = models.DateTimeField("изменена", auto_now=True)

    class Meta:
        verbose_name = "подписка"
        verbose_name_plural = "подписки"

    def __str__(self) -> str:
        return f"подписка {self.user.username} с {self.started_at:%d.%m.%Y}"

    @property
    def months_elapsed(self) -> int:
        """Сколько полных месяцев прошло с начала подписки (0 — первый месяц).

        Считаем по календарю, а не делением дней на 30: иначе у месяцев разной
        длины период уезжал бы относительно даты подписки.
        """
        now = timezone.now()
        months = (now.year - self.started_at.year) * 12 + (now.month - self.started_at.month)
        # День месяца ещё не наступил — текущий месяц не начался.
        if (now.day, now.time()) < (self.started_at.day, self.started_at.time()):
            months -= 1
        return max(months, 0)

    @property
    def current_period_start(self) -> datetime:
        """Начало текущего месяца подписки."""
        months = self.months_elapsed
        year = self.started_at.year + (self.started_at.month - 1 + months) // 12
        month = (self.started_at.month - 1 + months) % 12 + 1
        # День может не существовать в этом месяце (31-е в феврале) — прижимаем к концу.
        day = min(self.started_at.day, calendar.monthrange(year, month)[1])
        return self.started_at.replace(year=year, month=month, day=day)
