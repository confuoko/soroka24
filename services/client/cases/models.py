"""Модели клиентского сервиса: кто на что подписан и кто чего ждёт.

Судебных сущностей здесь НЕТ и быть не должно. Ни `Case`, ни `CaseEvent`, ни
`CourtSession`, ни `Document`, ни `Judge`, ни `Side` — всё это живёт в core_v2 и берётся у
него по HTTP при показе (ТЗ §2). Копия судебной модели в двух сервисах означала бы, что
любая правка парсера требует миграции здесь, а расхождение копий никто не заметит.

Что знает этот сервис и не знает core:

    какие дела интересны пользователям  →  подписки
    что кому уже показано              →  unread (UserCaseChange, Phase 6)
    кто чего ждёт прямо сейчас         →  PendingCaseSearch

Обратное тоже верно: этот сервис не знает, как устроены сайты судов, и знать не должен.
"""
from django.conf import settings
from django.db import models


class CaseSubscription(models.Model):
    """Пользователь следит за делом из core.

    `core_case_id` — id КАРТОЧКИ в core (тройка «УИД + суд + номер дела»), а не «дела
    вообще»: по одному УИД карточек бывает несколько — разные инстанции, приказное
    производство и последовавшее исковое. Следят за конкретной карточкой.

    Внешнего ключа тут нет и быть не может: дело живёт в другой базе. Значит, id может
    указывать на карточку, которой уже нет, и заметить это — наша забота. Ответ
    `PUT /monitoring/cases` возвращает `unknown_ids` ровно для этого.

    Полей `is_on_monitoring`, интервала обхода, тарифа, папок, Telegram здесь нет
    сознательно (ТЗ §2): всё это либо забота core, либо ещё не понадобилось.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscriptions",
        verbose_name="пользователь",
    )
    core_case_id = models.BigIntegerField("id дела в core", db_index=True)
    # Отписка НЕ удаляет строку: так видно, что пользователь когда-то следил за делом, и
    # повторная подписка не создаёт дубль (UNIQUE ниже всё равно не дал бы).
    is_active = models.BooleanField("активна", default=True)
    created_at = models.DateTimeField("создана", auto_now_add=True)

    class Meta:
        verbose_name = "подписка на дело"
        verbose_name_plural = "подписки на дела"
        constraints = [
            # Дважды подписаться на одно дело нельзя. Из этого же ограничения следует,
            # что список для мониторинга строится с distinct: одно дело — один обход,
            # сколько бы подписчиков у него ни было.
            models.UniqueConstraint(
                fields=["user", "core_case_id"], name="uq_subscription_user_case"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user} → дело #{self.core_case_id}"


class PendingCaseSearch(models.Model):
    """Пользователь ждёт ответа по ссылке: дела ещё нет, и подписки ещё нет.

    Между «добавил» и «нашли» проходит 30-60 секунд: core в это время идёт на портал через
    прокси и разгадывает капчу. Без этой строки ожидание жило бы только в открытой
    вкладке — закрыл её, и подписка не создалась бы, хотя дело в core уже появилось.
    Пользователь при этом уверен, что дело добавил.

    Строка живёт до развязки:

        core вернул success  →  создаём подписку, ставим resolved_at
        core вернул failed   →  ставим resolved_at и last_error, показываем причину

    Разрешать её умеют оба пути: страница ожидания (быстрый, пока вкладка открыта) и
    management-команда `resolve_pending_searches` (медленный, для закрытых вкладок).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pending_searches",
        verbose_name="пользователь",
    )
    # То, что ввёл пользователь: ссылка или УИД. Храним как есть — пока дело не найдено,
    # это единственное, чем его можно назвать на экране. Ни номера, ни УИД ещё нет: УИД
    # становится известен только с полученной страницы суда.
    query = models.CharField("запрос", max_length=1000)
    core_task_id = models.BigIntegerField("id задачи в core")
    created_at = models.DateTimeField("создана", auto_now_add=True)
    # Пусто — ещё ждём. Заполнено — развязка случилась, успешная или нет.
    resolved_at = models.DateTimeField("развязка", null=True, blank=True)
    # Техническая строка от core («403», «captcha timeout»). Пользователю показываем её
    # под «подробностями»: сама по себе она ему ничего не говорит.
    last_error = models.CharField("ошибка", max_length=500, blank=True, default="")

    class Meta:
        verbose_name = "ожидание поиска дела"
        verbose_name_plural = "ожидания поиска дел"
        constraints = [
            # Одна задача core — одно ожидание у пользователя. Страховка от двойного
            # submit формы: повторный POST не заведёт второе ожидание той же задачи.
            models.UniqueConstraint(
                fields=["user", "core_task_id"], name="uq_pending_user_task"
            ),
        ]

    def __str__(self) -> str:
        state = "развязано" if self.resolved_at else "ждём"
        return f"{self.user} → задача #{self.core_task_id} ({state})"

    @property
    def is_pending(self) -> bool:
        return self.resolved_at is None

