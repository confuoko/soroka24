"""Дело, поставленное пользователем на мониторинг.

Самих данных дела здесь нет — они живут в core. Здесь только связка
«пользователь ↔ дело в core» и кэш витрины, чтобы список дел рендерился одним
SQL-запросом, а не N походами в core-api по HTTP.
"""
from django.conf import settings
from django.db import models


class MonitoredCase(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", "ищем дело"
        ACTIVE = "active", "на мониторинге"
        FAILED = "failed", "не удалось получить"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="monitored_cases",
        verbose_name="пользователь",
    )
    # Пока работаем только со ссылками: у порталов без поиска по УИД карточка
    # доступна только по прямому адресу, и он же используется при каждом обходе.
    source_url = models.URLField("ссылка на дело", max_length=1000)
    state = models.CharField(
        "состояние", max_length=16, choices=State.choices, default=State.PENDING
    )

    # id сущностей в БД core.
    core_task_id = models.IntegerField("id задачи в core", null=True, blank=True)
    core_case_id = models.IntegerField("id дела в core", null=True, blank=True)

    # --- кэш витрины (обновляется задачами из tasks.py) ---
    core_status = models.CharField("статус дела", max_length=255, blank=True)
    # Дата ПОСЛЕДНЕГО ИЗМЕНЕНИЯ на портале (Case.last_changed_at в core).
    # Осознанно НЕ Case.updated_at: то поле двигается при каждом обходе, потому что
    # на каждом обходе дозаписывается история парсинга.
    core_changed_at = models.DateTimeField("последнее изменение", null=True, blank=True)
    # Дата ПОСЛЕДНЕЙ ПРОВЕРКИ (Case.last_checked_at в core) — ставится на каждом
    # обходе, даже холостом. Пользователю важны обе даты: «дело не менялось» ≠
    # «дело давно не проверяли».
    core_last_checked_at = models.DateTimeField("последняя проверка", null=True, blank=True)
    last_error = models.TextField("последняя ошибка", blank=True)

    created_at = models.DateTimeField("добавлено", auto_now_add=True)
    updated_at = models.DateTimeField("изменено", auto_now=True)

    class Meta:
        verbose_name = "дело на мониторинге"
        verbose_name_plural = "дела на мониторинге"
        ordering = ("-created_at",)
        constraints = [
            # Одну и ту же ссылку пользователь не добавляет дважды.
            models.UniqueConstraint(
                fields=("user", "source_url"), name="uq_monitored_user_url"
            ),
            # Разные ссылки могут привести к одной карточке — второй раз то же дело
            # тому же пользователю не заводим. NULL в условие не попадает, поэтому
            # дела в состоянии pending друг другу не мешают.
            models.UniqueConstraint(
                fields=("user", "core_case_id"),
                condition=models.Q(core_case_id__isnull=False),
                name="uq_monitored_user_case",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user.username}: дело {self.core_case_id or '—'} ({self.state})"
