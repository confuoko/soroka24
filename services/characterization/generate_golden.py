"""Пересобрать golden-файлы: полный вывод всех парсеров на всех сохранённых страницах.

Запуск из корня репозитория:

    .venv310\\Scripts\\python.exe services/characterization/generate_golden.py

Пересобирать golden ИМЕЕТ ПРАВО только тот, кто осознанно меняет поведение парсера, и
только вместе с записью «OLD → NEW → reason» в services/core_v2_AUDIT.md. Во всех
остальных случаях расхождение с golden — это найденная регрессия, а не повод перегенерить
файл.

Почему каждый парсер прогоняется по КАЖДОЙ странице, а не только по «своим»:

Так фиксируется и поведение на чужой вёрстке. Оно не абстрактное: строка заголовка,
протёкшая в tbody, приводила к тому, что C-страница, разобранная как B, кладёт текст
заголовка в status и обходит охранник _parse_is_empty (риск R8). Такие случаи должны быть
в контракте, а не всплывать после переноса.

Дополнительно пишется golden по detect_page_type — им определяется выбор парсера для
msudrf, и его поведение обязано сохраниться дословно.
"""
from __future__ import annotations

import sys
from pathlib import Path

CHARACTERIZATION_DIR = Path(__file__).resolve().parent
CORE_DIR = CHARACTERIZATION_DIR.parent / "core"
sys.path.insert(0, str(CORE_DIR))
sys.path.insert(0, str(CHARACTERIZATION_DIR))

from snapshot import dumps, parse_or_error  # noqa: E402

from app.parsers.msudrf_shared import detect_page_type  # noqa: E402
from app.parsers.registry import PARSER_BY_PAGE_TYPE  # noqa: E402

HTML_DIR = CORE_DIR / "html_examples"
GOLDEN_DIR = CHARACTERIZATION_DIR / "golden"

# Не страница дела и не в UTF-8: это выгрузка списка судов с sudrf.ru в windows-1251
# (4 МБ), её читает только scripts/build_courts_json.py. Парсерам карточек её давать
# незачем — исключаем по имени, а не молча по ошибке декодирования.
NOT_A_CASE_PAGE = frozenset({"mir_court_list_full.html"})


def main() -> int:
    GOLDEN_DIR.mkdir(exist_ok=True)
    pages = [p for p in sorted(HTML_DIR.glob("*.html")) if p.name not in NOT_A_CASE_PAGE]
    if not pages:
        print(f"НЕ НАЙДЕНО ни одной страницы в {HTML_DIR}", file=sys.stderr)
        return 1

    html_by_name: dict[str, str] = {}
    for page in pages:
        try:
            html_by_name[page.name] = page.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            # Молча пропускать нельзя: пропуск выглядел бы как «покрыто всё».
            print(f"ПРОПУЩЕНА (не UTF-8): {page.name} — {exc}", file=sys.stderr)
            return 1

    # 1. Вывод каждого парсера по каждой странице — свой файл на парсер.
    for page_type, parser_class in sorted(PARSER_BY_PAGE_TYPE.items()):
        snapshot = {
            name: parse_or_error(parser_class(), html)
            for name, html in html_by_name.items()
        }
        target = GOLDEN_DIR / f"parser_{page_type}.json"
        target.write_text(dumps(snapshot) + "\n", encoding="utf-8")
        print(f"{target.name}: {len(snapshot)} страниц")

    # 2. Определение вёрстки msudrf. None означает «это не карточка».
    detected = {name: detect_page_type(html) for name, html in html_by_name.items()}
    target = GOLDEN_DIR / "detect_page_type.json"
    target.write_text(dumps(detected) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for value in detected.values():
        key = value if value is not None else "не карточка"
        counts[key] = counts.get(key, 0) + 1
    print(f"{target.name}: {len(detected)} страниц, {counts}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
