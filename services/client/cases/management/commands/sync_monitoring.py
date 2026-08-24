"""Сверка: отправить core актуальный список дел на мониторинге.

Обычно синхронизация происходит сама — сразу после подписки и отписки. Эта команда нужна
для случаев, когда тот вызов не состоялся:

* core был недоступен в момент подписки (тогда мы записали WARNING и разошлись);
* подписки правили руками через админку;
* core накатили с чистой базы, и флаги обхода надо восстановить.

Безопасна к повторному запуску: операция замещающая и идемпотентная, повторный вызов с тем
же списком меняет в core 0 строк.
"""
from django.core.management.base import BaseCommand

from cases import monitoring


class Command(BaseCommand):
    help = "Отправить в core_v2 полный список дел, за которыми следят пользователи"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--force-empty",
            action="store_true",
            help=(
                "Разрешить отправку пустого списка, то есть снять с обхода ВСЕ дела. "
                "Без этого флага core отклонит пустой список с 409 — защита от нашей же "
                "аварии, когда queryset не собрался."
            ),
        )

    def handle(self, *args, **options) -> None:
        case_ids = monitoring.monitored_case_ids()
        self.stdout.write(f"Дел на мониторинге по нашим подпискам: {len(case_ids)}")

        result = monitoring.sync_monitoring(force_empty=options["force_empty"])
        if result is None:
            # Причина уже в логе: либо core недоступен, либо он отклонил пустой список.
            # Не пересказываем её здесь своими словами — переврём.
            raise SystemExit(
                self.style.ERROR(
                    "Список не отправлен, причина выше в логе. "
                    "Если список действительно пуст — повторите с --force-empty"
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Готово: на обходе {result.get('monitored')}, "
                f"добавлено {result.get('added')}, снято {result.get('removed')}"
            )
        )
        unknown = result.get("unknown_ids") or []
        if unknown:
            self.stdout.write(
                self.style.WARNING(f"Подписки на дела, которых нет в core: {unknown}")
            )
