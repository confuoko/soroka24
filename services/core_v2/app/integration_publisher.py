"""OutboxPublisher: отдельный процесс, увозящий изменения в RabbitMQ.

    python -m app.integration_publisher

Цикл простой до скуки:

    взять неопубликованное  →  опубликовать  →  отметить published_at  →  коммит

и так пока не попросят остановиться.

## Почему это отдельный процесс, а не часть обхода

Потому что обход не должен зависеть от брокера. Публиковать прямо в транзакции сверки —
значит связать сохранение судебных данных с доступностью RabbitMQ: упал брокер, и дело,
за которым мы честно сходили на портал, не сохранится. Transactional Outbox ровно эту
связь и разрывает: обход пишет в свою таблицу и заканчивается, а доставка — чужая забота.

Не Celery-задача, потому что задача нужна тогда, когда работу надо *запланировать*, а
здесь работа есть всегда, и её надо просто делать. Задача раз в секунду означала бы
секундный поток мусора в очереди задач.

## Доставка at-least-once, и это осознанно

Публикация в брокер и `UPDATE published_at` — две разные операции в двух разных системах,
между ними процесс может умереть. Тогда сообщение уедет повторно. Exactly-once потребовал
бы распределённой транзакции с брокером; вместо этого читающий обязан быть идемпотентным,
и у него для этого есть неизменный id сообщения.

Чего мы при этом НЕ допускаем — потери. Отмечаем published_at только у тех сообщений,
которые брокер подтвердил, и упавшая на середине порция помечает ровно свой успешный
префикс (см. publish_batch).

## Ровно одна реплика

Больше не нужно: секундного опроса хватает с огромным запасом. Больше и не сломает —
take_unpublished берёт строки через FOR UPDATE SKIP LOCKED, поэтому второй publisher
возьмёт следующие, а не те же. Защита стоит дёшево, а цена ошибки — дубли у каждого
подписчика.
"""
import json
import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from kombu import Connection, Exchange, Producer, Queue

from app import config
from app.database import session_scope
from app.models import IntegrationOutboxEvent
from app.repositories import IntegrationOutboxRepository

logger = logging.getLogger(__name__)

# Топология: durable exchange + durable очередь, привязанная к нему.
#
# durable и у того, и у другой, плюс persistent-сообщения ниже: перезапуск брокера не
# должен стирать накопленное. Иначе `docker compose restart rabbitmq` тихо уносил бы
# изменения, о которых пользователю уже никто не расскажет — в outbox они помечены
# опубликованными.
#
# direct, а не topic: маршрутизировать нечего, потребителю нужны все изменения без
# разбора. Появится потребитель, которому нужна часть, — тип поменяется здесь.
CASE_CHANGES_EXCHANGE = Exchange(
    config.CASE_CHANGES_EXCHANGE, type="direct", durable=True
)
CASE_CHANGES_QUEUE = Queue(
    config.CASE_CHANGES_QUEUE,
    exchange=CASE_CHANGES_EXCHANGE,
    routing_key=config.CASE_CHANGES_QUEUE,
    durable=True,
)

# Политика повторов при обрыве соединения. Publisher живёт часами рядом с брокером,
# который перезапускают; одна transient-ошибка не повод падать.
RETRY_POLICY = {
    "interval_start": 0,
    "interval_step": 1,
    "interval_max": 10,
    "max_retries": 5,
}


def message_of(row: IntegrationOutboxEvent) -> dict:
    """Сообщение в том виде, в каком его увидит подписчик.

    Собирается из колонок, а не из JSONB-поля, и это существенно: схема таблицы И ЕСТЬ
    контракт (см. app/models/integration_outbox.py). Добавить поле в сообщение, не
    добавив его в схему, нельзя — а значит, формат не может измениться незаметно.

    occurred_at — строкой ISO 8601 с смещением. Голый datetime в JSON не уходит, а
    смещение обязательно: без него подписчик не отличит момент по Москве от момента по UTC.
    """
    return {
        "id": row.id,
        "type": row.event_type,
        "version": row.version,
        "case_id": row.case_id,
        "entity_id": row.entity_id,
        "occurred_at": row.occurred_at.isoformat(),
    }


@dataclass
class Batch:
    """Итог одной порции: сколько ушло, сколько ждало, и что помешало.

    error возвращается, а не бросается, ровно ради частичного успеха: порция, упавшая на
    пятидесятом сообщении, должна оставить помеченными первые сорок девять. Брось мы
    исключение — session_scope откатил бы транзакцию вместе с этими отметками, и все сорок
    девять уехали бы повторно.
    """

    published: int = 0
    taken: int = 0
    error: Optional[BaseException] = None

    @property
    def had_more(self) -> bool:
        """Порция была полной — значит, в таблице почти наверняка есть ещё."""
        return self.taken >= config.PUBLISHER_BATCH_SIZE


def publish_batch(publish: Callable[[dict], None], limit: Optional[int] = None) -> Batch:
    """Опубликовать одну порцию неопубликованных сообщений.

    publish передаётся аргументом, а не берётся из модуля: так эту функцию — то есть всю
    логику «что взять, что отметить, что делать с половинчатым успехом» — можно проверить
    без живого RabbitMQ. Настоящий producer подставляет run().

    Порядок операций важен и обратный интуитивному: сначала публикуем, потом отмечаем.
    Наоборот было бы хуже — отметив до публикации, при падении мы бы сообщение ПОТЕРЯЛИ, а
    так в худшем случае отправим дважды. Из двух неприятностей выбрана обратимая.
    """
    batch = Batch()
    limit = limit if limit is not None else config.PUBLISHER_BATCH_SIZE

    with session_scope() as session:
        repo = IntegrationOutboxRepository(session)
        rows = repo.take_unpublished(limit)
        batch.taken = len(rows)
        if not rows:
            return batch

        sent = []
        try:
            for row in rows:
                publish(message_of(row))
                sent.append(row)
        except BaseException as exc:  # noqa: BLE001 — причина в докстринге Batch
            batch.error = exc

        # Отмечаем ровно то, что ушло. Коммитит session_scope на выходе — и коммитит
        # даже при случившейся ошибке, потому что исключение мы не пробрасываем.
        repo.mark_published(sent)
        batch.published = len(sent)

    return batch


def run() -> None:
    """Бесконечный цикл публикации. Останавливается по SIGTERM/SIGINT.

    Обработка сигналов здесь не для красоты: `docker compose stop` присылает SIGTERM, и
    без обработчика процесс умирает посреди порции. Часть сообщений уже в брокере, отметки
    не закоммичены — после перезапуска они уедут снова. Работать это будет (подписчик
    идемпотентен), но дубли на каждом деплое ни к чему.

    Соединение с брокером держим ОДНО на весь цикл, а не открываем на каждую порцию:
    установка AMQP-соединения дороже самой публикации, и раз в секунду это было бы
    заметно.
    """
    stopping = False

    def request_stop(signum, _frame) -> None:
        nonlocal stopping
        stopping = True
        logger.info("Получен сигнал %s — останавливаемся после текущей порции", signum)

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    logger.info(
        "Publisher запущен: брокер %s, exchange %s, очередь %s, порция %s, опрос %s с",
        _safe_url(config.INTEGRATION_BROKER_URL),
        config.CASE_CHANGES_EXCHANGE,
        config.CASE_CHANGES_QUEUE,
        config.PUBLISHER_BATCH_SIZE,
        config.PUBLISHER_POLL_SECONDS,
    )

    with Connection(config.INTEGRATION_BROKER_URL) as connection:
        producer = Producer(connection)

        # Объявляем топологию заранее, чтобы очередь существовала ДО первого сообщения.
        # Иначе изменения, случившиеся раньше первого запуска подписчика, ушли бы в
        # exchange без привязанной очереди — то есть в никуда, и молча.
        CASE_CHANGES_QUEUE.maybe_bind(connection)
        CASE_CHANGES_QUEUE.declare()

        def publish(message: dict) -> None:
            producer.publish(
                json.dumps(message, ensure_ascii=False),
                exchange=CASE_CHANGES_EXCHANGE,
                routing_key=config.CASE_CHANGES_QUEUE,
                content_type="application/json",
                content_encoding="utf-8",
                # persistent: сообщение переживёт перезапуск брокера. Вместе с durable
                # очередью это и означает «не потеряем».
                delivery_mode=2,
                retry=True,
                retry_policy=RETRY_POLICY,
                declare=[CASE_CHANGES_QUEUE],
            )

        while not stopping:
            try:
                batch = publish_batch(publish)
            except Exception:
                # Сюда попадает только то, что случилось ВНЕ публикации: недоступная БД,
                # обрыв на take_unpublished. Не роняем процесс — база вернётся.
                logger.exception("Порция не обработана, повторим через %s с",
                                 config.PUBLISHER_POLL_SECONDS)
                time.sleep(config.PUBLISHER_POLL_SECONDS)
                continue

            if batch.published:
                logger.info("Опубликовано сообщений: %s", batch.published)
            if batch.error is not None:
                # Часть порции ушла и отмечена; остаток возьмём следующим кругом.
                logger.warning(
                    "Публикация прервана после %s из %s сообщений: %s",
                    batch.published, batch.taken, batch.error,
                )
                time.sleep(config.PUBLISHER_POLL_SECONDS)
                continue

            # Полная порция означает «в таблице есть ещё» — идём за следующей сразу,
            # иначе накопившийся хвост разбирался бы по сто сообщений в секунду.
            if not batch.had_more:
                time.sleep(config.PUBLISHER_POLL_SECONDS)

    logger.info("Publisher остановлен")


def _safe_url(url: str) -> str:
    """Адрес брокера без пароля — его незачем писать в лог."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    return f"{scheme}://***@{rest.rpartition('@')[2]}"


if __name__ == "__main__":
    # Конфигурации логов в сервисе нет (логи собирает docker), поэтому настраиваем
    # минимально здесь: без этого logger.info не был бы виден вовсе, и процесс выглядел
    # бы мёртвым.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run()
