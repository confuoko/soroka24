"""Сериализация вывода парсера в стабильный JSON — для golden-файлов.

Зачем свой сериализатор, а не `json.dumps(..., default=str)`:

1. **Тип значения должен быть виден в golden-файле.** `date` и `datetime` — не одно и
   то же: календарные даты обязаны оставаться `date` (см. R3 в services/core_v2_AUDIT.md),
   и регрессия «дата превратилась в дату-со-временем» должна ронять тест. Поэтому дата
   пишется как {"__date__": "..."}, а момент — как {"__datetime__": "..."}.

2. **Наличие ключа значимо само по себе.** Отсутствующий ключ и ключ со значением None —
   это два разных указания для CaseRepository (риск R1). JSON-словарь различает их
   естественным образом: отсутствующий ключ просто отсутствует. Ничего не досеиваем.

3. **Порядок элементов в списках значим.** Порядок строк документов участвует в
   identity через occurrence (риск R2), поэтому списки НЕ сортируются никогда.
   Сортируются только ключи словарей — чтобы diff golden-файла был читаемым.

4. **Наивное локальное время обязано остаться наивным.** Если у datetime появится
   tzinfo, это попадёт в golden как отдельное поле и тест упадёт.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any


def encode(value: Any) -> Any:
    """Превратить значение из вывода парсера в JSON-совместимое, сохранив тип."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    # Порядок проверок важен: datetime — подкласс date, поэтому datetime первым.
    if isinstance(value, dt.datetime):
        encoded: dict[str, Any] = {"__datetime__": value.isoformat()}
        if value.tzinfo is not None:
            # Парсеры обязаны отдавать наивное локальное время. Если тут появился
            # пояс — это регрессия, и она должна быть видна в diff'е golden-файла.
            encoded["tzinfo"] = str(value.tzinfo)
        return encoded

    if isinstance(value, dt.date):
        return {"__date__": value.isoformat()}

    if isinstance(value, dt.time):
        return {"__time__": value.isoformat()}

    if isinstance(value, dict):
        return {str(key): encode(item) for key, item in value.items()}

    if isinstance(value, (list, tuple)):
        # Списки не сортируем: порядок — часть контракта (R2).
        return [encode(item) for item in value]

    # Всё остальное — незнакомый тип. Пишем и тип, и значение: это само по себе
    # находка, о которой должен узнать тот, кто смотрит diff.
    return {"__repr__": repr(value), "__type__": type(value).__name__}


def dumps(value: Any) -> str:
    """Каноничный JSON: отступ 2, ключи отсортированы, юникод как есть."""
    return json.dumps(encode(value), ensure_ascii=False, indent=2, sort_keys=True)


def parse_or_error(parser, html: str) -> Any:
    """Разобрать страницу, а исключение записать в снимок вместо падения.

    Парсер обязан выдерживать пустой и чужой документ, отдавая пустой результат, а не
    исключение (риск R11). Если он всё же бросил — это тоже фиксируемое поведение:
    golden-файл запомнит текст исключения, и его исчезновение или появление будет
    видно как изменение.
    """
    try:
        return parser.parse(html)
    except Exception as exc:  # noqa: BLE001 — фиксируем ЛЮБОЕ исключение осознанно
        return {"__error__": f"{type(exc).__name__}: {exc}"}
