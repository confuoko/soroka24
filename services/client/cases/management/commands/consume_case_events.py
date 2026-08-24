"""Постоянно работающий процесс: слушает очередь case_changes.

    python manage.py consume_case_events

Соединение держится открытым, и RabbitMQ отдаёт сообщение сразу, как только оно появилось.
Опроса раз в минуту здесь нет и не должно быть: он и медленнее (изменение ждёт до минуты
впустую), и дороже (шестьдесят пустых запросов вместо одного висящего соединения).

Здесь только транспорт: соединение, подтверждения, сигналы. Решение — что с сообщением
делать — в cases/consumer.py, и проверяется оно без живого брокера.

## Кто объявляет топологию

Обе стороны, и это не дублирование. Объявление exchange и очереди идемпотентно, зато
порядок запуска контейнеров перестаёт иметь значение: поднимется consumer первым — очередь
будет ждать сообщений; поднимется publisher первым — сообщения будут ждать в очереди.

Параметры (durable, тип exchange) обязаны совпадать с теми, что задаёт publisher. Иначе
RabbitMQ отвергнет повторное объявление с другими параметрами — и, что важно, отвергнет
громко: PRECONDITION_FAILED вместо тихого расхождения.

## Ровно один процесс не нужен

В отличие от publisher'а, реплик consumer'а может быть несколько: RabbitMQ отдаёт каждое
сообщение ОДНОМУ подписчику, а обработка идемпотентна. Это способ разложить нагрузку, а не
источник дублей.
"""
import logging
import signal

import pika
from django.conf import settings
from django.core.management.base import BaseCommand

from cases.consumer import Outcome, handle

logger = logging.getLogger(__name__)


def broker_parameters(url: str) -> pika.URLParameters:
    """Разобрать адрес брокера, приведя vhost к форме, которую понимает pika.

    Тонкость, на которой легко потерять вечер. Адрес в .env один на всех и записан в
    привычной для Celery форме:

        amqp://soroka:soroka@rabbitmq:5672//

    Двойной слэш на конце для kombu (то есть для core) означает vhost `/` — стандартный.
    А pika читает тот же адрес как vhost с ПУСТЫМ именем и падает с
    `NOT_ALLOWED - vhost  not found`, где между словами видна дырка от пустого имени.

    Правильная для pika запись того же vhost — `/%2F`. Приводим здесь, а не в .env, чтобы
    общий адрес остался в той единственной форме, которую в этом репозитории все уже знают,
    и чтобы правка .env не ломала одну из сторон молча.
    """
    if url.endswith("//"):
        url = f"{url[:-1]}%2F"
    return pika.URLParameters(url)


class Command(BaseCommand):
    help = "Слушать очередь case_changes и обрабатывать изменения по делам"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help=(
                "Выйти после N сообщений. 0 — работать бесконечно. Нужно для ручной "
                "проверки цепочки: запустил, увидел сообщение, вышел."
            ),
        )

    def handle(self, *args, **options) -> None:
        limit = options["limit"]
        processed = 0
        stopping = False

        def request_stop(signum, _frame) -> None:
            """Попросить цикл остановиться.

            Только флаг, и это важно. Закрывать соединение прямо здесь нельзя: pika в этот
            момент сидит в poll(), и закрытие сокета из обработчика сигнала роняет его
            `OSError: [Errno 9] Bad file descriptor`. BlockingConnection не рассчитан на
            то, что его тронут из-под сигнала.
            """
            nonlocal stopping
            stopping = True
            logger.info("Получен сигнал %s — остановимся после текущего сообщения", signum)

        connection = pika.BlockingConnection(
            broker_parameters(settings.INTEGRATION_BROKER_URL)
        )
        channel = connection.channel()

        # Параметры ОБЯЗАНЫ совпадать с publisher'ом core_v2 — иначе PRECONDITION_FAILED.
        channel.exchange_declare(
            exchange=settings.CASE_CHANGES_EXCHANGE,
            exchange_type="direct",
            durable=True,
        )
        channel.queue_declare(queue=settings.CASE_CHANGES_QUEUE, durable=True)
        channel.queue_bind(
            queue=settings.CASE_CHANGES_QUEUE,
            exchange=settings.CASE_CHANGES_EXCHANGE,
            routing_key=settings.CASE_CHANGES_QUEUE,
        )

        # Без prefetch брокер отдал бы всю очередь разом: при падении процесса всё это
        # вернулось бы и переделывалось заново.
        #
        # При --limit урезаем до самого лимита: иначе брокер налил бы в буфер pika двадцать
        # сообщений, и все они успели бы пройти через обработчик до того, как мы заметим,
        # что лимит достигнут.
        prefetch = settings.CONSUMER_PREFETCH
        if limit:
            prefetch = min(prefetch, limit)
        channel.basic_qos(prefetch_count=prefetch)

        # docker compose stop присылает SIGTERM. Без обработчика процесс умер бы посреди
        # обработки: сообщение не подтверждено, брокер отдаст его заново. Работать это
        # будет (обработка идемпотентна), но дубли на каждом деплое ни к чему.
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

        logger.info(
            "Слушаем очередь %s (exchange %s), prefetch %s",
            settings.CASE_CHANGES_QUEUE,
            settings.CASE_CHANGES_EXCHANGE,
            prefetch,
        )

        def on_message(ch, method, _properties, body) -> None:
            nonlocal processed, stopping

            if limit and processed >= limit:
                # Лимит уже выбран, а сообщение брокер успел отдать. Возвращаем его в
                # очередь НЕтронутым: подтвердить необработанное значило бы потерять
                # изменение, а --limit существует для ручной проверки и врать о том, что
                # он сделал, не должен.
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                stopping = True
                return

            try:
                outcome = handle(body)
            except Exception:
                # Непредвиденное в обработчике. Переспрашиваем: причина, скорее всего,
                # временная (упала база), а терять изменение из-за нашей же ошибки нельзя.
                #
                # Риск зацикливания осознан: если причина НЕ временная, сообщение будет
                # ходить по кругу. Заметно это будет сразу — по потоку трейсбеков в логе,
                # а не по тишине, и это лучше, чем молча выброшенное изменение.
                logger.exception("Сообщение не обработано, вернём в очередь")
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                return

            if outcome is Outcome.RETRY:
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                return

            # PROCESSED и MALFORMED — оба ack. Разница в логе и в намерении: первое
            # обработано, второе выброшено осознанно, чтобы не забить очередь.
            ch.basic_ack(delivery_tag=method.delivery_tag)
            processed += 1

            if limit and processed >= limit:
                logger.info("Обработано %s сообщений — выходим по --limit", processed)
                stopping = True

        channel.basic_consume(
            queue=settings.CASE_CHANGES_QUEUE, on_message_callback=on_message
        )

        # Свой цикл вместо channel.start_consuming(), и не из любви к велосипедам.
        # start_consuming блокируется навсегда и флага не проверяет: выйти из него можно
        # только тронув соединение извне, а этого BlockingConnection не переживает
        # (см. request_stop). process_data_events с лимитом отдаёт управление раз в
        # секунду, и остановка становится обычной проверкой условия.
        try:
            while not stopping:
                connection.process_data_events(time_limit=1)
        finally:
            # Неподтверждённое вернётся брокеру само при закрытии соединения — это и есть
            # правильное поведение: мы за него не отвечаем, пусть отдаст кому-то ещё.
            try:
                if connection.is_open:
                    channel.stop_consuming()
                    connection.close()
            except Exception:
                logger.warning("Соединение закрылось не чисто", exc_info=True)

        self.stdout.write(
            self.style.SUCCESS(f"Остановлены. Обработано сообщений: {processed}")
        )
