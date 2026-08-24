"""Довести до конца ожидания поиска, чьи вкладки закрыли.

Быстрый путь развязки — сама страница ожидания: она обновляется раз в несколько секунд и
создаёт подписку, как только core сообщает об успехе. Но человек может закрыть вкладку, и
тогда дело в core появится, а подписки не будет — при этом пользователь уверен, что дело
добавил. Эта команда закрывает именно эту дырку.

Запускать по расписанию ОС (cron, планировщик задач) — раз в минуту достаточно. Celery в
клиентском сервисе не заводим: одна периодическая команда его не оправдывает.

Идемпотентна: уже развязанные ожидания не трогаются.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from cases import monitoring
from cases.models import PendingCaseSearch


class Command(BaseCommand):
    help = "Спросить core о судьбе незавершённых поисков и создать подписки"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--older-than-seconds",
            type=int,
            default=0,
            help=(
                "Брать только ожидания старше N секунд. Смысл в том, чтобы не мешать "
                "открытой странице: она опрашивает core сама, и дублировать её работу "
                "незачем."
            ),
        )
        parser.add_argument(
            "--give-up-after-hours",
            type=int,
            default=24,
            help=(
                "Ожидания старше N часов пометить провалившимися. Задача в core могла "
                "залипнуть в RUNNING (воркер умер жёстко), и такое ожидание висело бы "
                "вечно, показывая пользователю «ищем»."
            ),
        )

    def handle(self, *args, **options) -> None:
        now = timezone.now()
        cutoff = now - timedelta(seconds=options["older_than_seconds"])

        pendings = PendingCaseSearch.objects.filter(
            resolved_at__isnull=True, created_at__lte=cutoff
        ).select_related("user")

        resolved = 0
        given_up = 0
        for pending in pendings:
            age = now - pending.created_at
            if age > timedelta(hours=options["give_up_after_hours"]):
                pending.last_error = (
                    f"Поиск не завершился за {options['give_up_after_hours']} ч — "
                    "задача в core, похоже, потерялась"
                )
                pending.resolved_at = now
                pending.save(update_fields=["last_error", "resolved_at"])
                given_up += 1
                continue

            before = pending.resolved_at
            monitoring.resolve_pending(pending)
            if before is None and pending.resolved_at is not None:
                resolved += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Просмотрено ожиданий: {len(pendings)}, развязано: {resolved}, "
                f"признано потерянными: {given_up}"
            )
        )
