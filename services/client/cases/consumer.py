"""Разбор и обработка одного сообщения из очереди case_changes.

Отдельно от management-команды сознательно: там транспорт (соединение, ack, сигналы), здесь
решение. Так всю логику — что считать мусором, что переспросить, что выбросить — можно
проверить без живого RabbitMQ.

## Что означает сообщение

    {"id": 1502, "type": "event_new", "version": 1,
     "case_id": 481, "entity_id": 712, "occurred_at": "2026-08-23T13:20:00Z"}

«По делу 481 core обнаружил новое событие, id этого события 712». И всё: судебных данных
в сообщении нет и не будет. Понадобятся — заберём по HTTP (`GET /cases/481`).

`id` — идентификатор СООБЩЕНИЯ, а не события. На нём держится идемпотентность: доставка
at-least-once, и то же сообщение может прийти дважды.

## Про ack и повторную доставку

Три исхода, и разница между ними не формальность:

    PROCESSED  ack                — обработали
    MALFORMED  ack                — выбросили ОСОЗНАННО
    RETRY      nack(requeue=True) — не смогли, но сможем позже

Выбрасывать мусор приходится потому, что альтернатива хуже. `nack(requeue=True)` на
непонятном сообщении вернёт его в очередь, мы получим его снова, снова не поймём — и так
навсегда, забив очередь одним битым сообщением и заблокировав все остальные. Поэтому мусор
уходит из очереди с громким WARNING в логе.

RETRY — только для того, что действительно пройдёт со второго раза: недоступная база,
таймаут. Пока consumer только логирует, этот исход не встречается; он появится в Phase 6.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)


class Outcome(str, Enum):
    """Что делать с сообщением после обработки."""

    PROCESSED = "processed"   # ack
    MALFORMED = "malformed"   # ack: выбрасываем осознанно
    RETRY = "retry"           # nack(requeue=True)


@dataclass(frozen=True)
class CaseChange:
    """Разобранное сообщение об изменении по делу."""

    id: int
    type: str
    case_id: int
    entity_id: Optional[int]
    occurred_at: datetime
    version: int


class Malformed(Exception):
    """Сообщение не разбирается или разбирается не в то, чего мы ждём."""


def parse(body: bytes) -> CaseChange:
    """Разобрать тело сообщения. Всё, что не сходится, — Malformed.

    Проверяем строго, и не из аккуратности. Сообщение приходит из другого сервиса, который
    деплоят отдельно; молча принять `case_id` строкой или потерявшийся `id` значит записать
    мусор в свою базу и разбираться с ним потом, когда источник уже забыт.
    """
    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise Malformed(f"не JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise Malformed(f"ожидался объект, пришло {type(payload).__name__}")

    missing = {"id", "type", "case_id", "version", "occurred_at"} - set(payload)
    if missing:
        raise Malformed(f"нет обязательных полей: {sorted(missing)}")

    version = payload["version"]
    if version != settings.INTEGRATION_EVENT_VERSION:
        # Версия для этого и заведена: незнакомый формат надо отвергнуть явно, а не читать
        # поля наугад. Практический вывод — при несовместимой правке контракта клиента
        # деплоят ПЕРВЫМ, иначе сообщения новой версии будут выброшены.
        raise Malformed(
            f"версия контракта {version!r}, а мы понимаем "
            f"{settings.INTEGRATION_EVENT_VERSION}"
        )

    try:
        occurred_at = datetime.fromisoformat(payload["occurred_at"])
    except (ValueError, TypeError) as exc:
        raise Malformed(f"occurred_at не разбирается: {payload['occurred_at']!r}") from exc

    if occurred_at.tzinfo is None:
        # Момент без смещения нельзя положить в timestamptz, не выдумав пояс. Выдумывать
        # не будем: у core он всегда со смещением, и его отсутствие означает, что сообщение
        # собрал кто-то другой.
        raise Malformed(f"occurred_at без смещения: {payload['occurred_at']!r}")

    try:
        message_id = int(payload["id"])
        case_id = int(payload["case_id"])
        entity_id = payload.get("entity_id")
        entity_id = int(entity_id) if entity_id is not None else None
    except (TypeError, ValueError) as exc:
        raise Malformed(f"идентификаторы не числа: {exc}") from exc

    event_type = payload["type"]
    if not isinstance(event_type, str) or not event_type:
        raise Malformed(f"тип изменения не строка: {event_type!r}")

    return CaseChange(
        id=message_id,
        type=event_type,
        case_id=case_id,
        entity_id=entity_id,
        occurred_at=occurred_at,
        version=version,
    )


def handle(body: bytes) -> Outcome:
    """Обработать одно сообщение и сказать, что с ним делать в очереди.

    Пока только логирует: цепочку «core → outbox → RabbitMQ → Django» надо сначала
    увидеть работающей целиком, и лишний код на этом шаге только мешал бы понять, где она
    рвётся (ТЗ, Phase 5). Раскладка по подписчикам — Phase 6, она встанет здесь же.
    """
    try:
        change = parse(body)
    except Malformed as exc:
        # Осознанно выбрасываем. Не requeue: битое сообщение вернулось бы и зациклилось,
        # заблокировав очередь целиком.
        logger.warning(
            "Сообщение выброшено (%s). Тело: %.300s", exc, body.decode("utf-8", "replace")
        )
        return Outcome.MALFORMED

    logger.info(
        "Изменение #%s: %s по делу %s (сущность %s, обнаружено %s)",
        change.id, change.type, change.case_id, change.entity_id,
        change.occurred_at.isoformat(),
    )
    return Outcome.PROCESSED
