"""Парсер карточки дела мировых судов Москвы (mos-sud.ru) — страница типа A.

Первая версия достаёт только ФИО судьи. Данные лежат в «карточке» —
блоке пар «метка/значение»: div.detail-cart div.row_card > (.left = метка, .right = значение).

ВАЖНО (грабли разметки портала):
- В метке «Cудья» первая буква — ЛАТИНСКАЯ «C» (U+0043), а не кириллическая «С».
  Поэтому матчим устойчиво по regex [СсCc]удья, иначе селектор молча не сработает.
- Значения обильно обложены пробелами/переводами строк — везде чистим текст.
"""
import re

from bs4 import BeautifulSoup

from app.parsers.base import CaseParser

# Пары «метка/значение» карточки.
ROW_SELECTOR = "div.detail-cart div.row_card"
LABEL_SELECTOR = "div.left"
VALUE_SELECTOR = "div.right"

# Метка судьи: латинская или кириллическая «С», затем «удья» (кириллица).
JUDGE_LABEL_RE = re.compile(r"^[СсCc]удья\b")


def _clean(text: str) -> str:
    """Схлопнуть любые пробелы/переводы строк в один пробел и обрезать края."""
    return " ".join(text.split())


class MoscowTypeAParser(CaseParser):
    """Парсер страниц типа A (мировые суды Москвы)."""

    page_type = "A"

    def parse(self, html: str) -> dict:
        """Разобрать карточку. Пока — только ФИО судей."""
        soup = BeautifulSoup(html, "lxml")

        judge_names: list[str] = []
        for row in soup.select(ROW_SELECTOR):
            label_el = row.select_one(LABEL_SELECTOR)
            value_el = row.select_one(VALUE_SELECTOR)
            if label_el is None or value_el is None:
                continue
            if not JUDGE_LABEL_RE.match(_clean(label_el.get_text())):
                continue
            name = _clean(value_el.get_text())
            if name:
                judge_names.append(name)

        return {"judge_names": judge_names}
