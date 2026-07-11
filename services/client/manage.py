#!/usr/bin/env python
"""Утилита командной строки Django."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Не удалось импортировать Django. Установлен ли он и активировано ли "
            "виртуальное окружение?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
