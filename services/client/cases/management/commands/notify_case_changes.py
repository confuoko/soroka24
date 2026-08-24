"""Сообщить пользователям об изменениях, о которых им ещё не сообщали.

**Писем эта команда пока не шлёт.** Доставка — заглушка в `cases/notifications.py`, она
только пишет строку в лог. Канала (почта, Telegram) в проекте ещё нет, и это не поломка:
цепочка до unread в UI работает, а рассылку заказчик пока не просил. Когда канал появится,
изменится одна функция `deliver`, а эта команда — нет.

Запускать руками. Расписание (CronJob рядом с `resolve_pending_searches`) появится вместе с
каналом: гонять заглушку по расписанию незачем.

Идемпотентна: то, о чём уже сообщили, второй раз не отправляется — у таких строк проставлен
`notified_at`.
"""
from django.core.management.base import BaseCommand

from cases import notifications


class Command(BaseCommand):
    help = "Разослать уведомления об изменениях по делам (сейчас — заглушка в лог)"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help=(
                "Обработать не больше N уведомлений. 0 — все. Ограничение считается в "
                "уведомлениях, а не в изменениях: одно уведомление — это все новости по "
                "одному делу для одного человека, и резать его пополам бессмысленно."
            ),
        )

    def handle(self, *args, **options) -> None:
        delivered = notifications.notify_pending(limit=options["limit"])

        if not delivered:
            self.stdout.write("Сообщать не о чем")
            return

        self.stdout.write(
            self.style.SUCCESS(f"Уведомлений доставлено: {delivered}")
        )
        self.stdout.write(
            self.style.WARNING(
                "Напоминание: доставка — заглушка, писем никому не ушло. "
                "Настоящий канал подключается в cases/notifications.deliver"
            )
        )
