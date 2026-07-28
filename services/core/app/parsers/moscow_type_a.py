"""Парсер карточки дела мировых судов Москвы (mos-sud.ru) — страница типа A.

Достаёт ФИО судьи, стороны по делу и события «Истории состояний».
- Судья и стороны лежат в «карточке» — блоке пар «метка/значение»:
  div.detail-cart div.row_card > (.left = метка, .right = значение).
- События — в таблице под заголовком <h3>История состояний</h3>
  (3 колонки: Дата / Состояние / Документ-основание).

ВАЖНО (грабли разметки портала):
- В метке «Cудья» первая буква — ЛАТИНСКАЯ «C» (U+0043), а не кириллическая «С».
  Поэтому матчим устойчиво по regex [СсCc]удья, иначе селектор молча не сработает.
  Для «Стороны» на всякий случай матчим так же терпимо к латинской «C».
- Значения обильно обложены пробелами/переводами строк — везде чистим текст.
- Стороны лежат одной строкой в .right в виде «<strong>Роль: </strong>ФИО<br>» —
  ролей может быть несколько (истец, ответчик, привлекаемое лицо и т.п.).
- В том же контейнере, что «История состояний», ниже идёт таблица «История
  местонахождения» с тем же классом mainTable — поэтому таблицу событий ищем
  по тексту заголовка <h3>, а не по классу.
"""
import re
from datetime import date, datetime

from bs4 import BeautifulSoup, Tag

from app.parsers.base import CaseParser

# Пары «метка/значение» карточки.
ROW_SELECTOR = "div.detail-cart div.row_card"
LABEL_SELECTOR = "div.left"
VALUE_SELECTOR = "div.right"

# Метка судьи: латинская или кириллическая «С», затем «удья» (кириллица).
JUDGE_LABEL_RE = re.compile(r"^[СсCc]удья\b")
# Метка блока сторон: латинская или кириллическая «С», затем «тороны».
SIDES_LABEL_RE = re.compile(r"^[СсCc]тороны\b")

# Заголовок таблицы событий (движение дела).
STATE_HISTORY_HEADING = "История состояний"
# Формат дат на портале.
DATE_FORMAT = "%d.%m.%Y"


def _clean(text: str) -> str:
    """Схлопнуть любые пробелы/переводы строк в один пробел и обрезать края."""
    return " ".join(text.split())


def _parse_date(text: str) -> date | None:
    """Разобрать дату формата ДД.ММ.ГГГГ; пустое/некорректное значение → None."""
    text = _clean(text)
    if not text:
        return None
    try:
        return datetime.strptime(text, DATE_FORMAT).date()
    except ValueError:
        return None


def _parse_state_history(soup: BeautifulSoup) -> list[dict]:
    """Разобрать таблицу «История состояний» в список событий.

    Возвращает {"event_date": date, "state_description": str, "document_str": str|None}.
    Обязательны дата и описание состояния (образуют identity события) — строки без
    любого из них пропускаем. Пустое «Документ-основание» → document_str = None.
    """
    heading = soup.find(
        "h3", string=lambda s: s is not None and _clean(s) == STATE_HISTORY_HEADING
    )
    if heading is None:
        return []
    table = heading.find_next("table")
    if table is None:
        return []

    events: list[dict] = []
    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        event_date = _parse_date(cells[0].get_text())
        state_description = _clean(cells[1].get_text())
        document_str = _clean(cells[2].get_text()) or None

        # Дата + описание обязательны — иначе событие не может участвовать в детекте.
        if event_date is None or not state_description:
            continue

        events.append(
            {
                "event_date": event_date,
                "state_description": state_description,
                "document_str": document_str,
            }
        )
    return events


def _parse_sides(value_el: Tag) -> list[dict]:
    """Разобрать .right блока «Стороны» в список {"role", "full_name"}.

    Формат значения: <strong>Роль: </strong>ФИО<br> — повторяется для каждой стороны.
    Роль берём из <strong> (без хвостового двоеточия), ФИО — текст между этим <strong>
    и следующим <br>/<strong>. Порядок сохраняем; сопоставление роли с типом стороны —
    задача слоя БД (SideRepository), парсер отдаёт сырую роль строкой.
    """
    sides: list[dict] = []
    for strong in value_el.find_all("strong"):
        role = _clean(strong.get_text()).rstrip(":").strip()

        parts: list[str] = []
        for sib in strong.next_siblings:
            if isinstance(sib, Tag):
                if sib.name in ("strong", "br"):
                    break  # конец значения текущей роли
                parts.append(sib.get_text())
            else:  # NavigableString
                parts.append(str(sib))
        full_name = _clean(" ".join(parts))

        if role and full_name:
            sides.append({"role": role, "full_name": full_name})
    return sides


class MoscowTypeAParser(CaseParser):
    """Парсер страниц типа A (мировые суды Москвы)."""

    page_type = "A"

    def parse(self, html: str) -> dict:
        """Разобрать карточку: ФИО судей, стороны и события «Истории состояний»."""
        soup = BeautifulSoup(html, "lxml")

        # === КАРТОЧКА: судьи и стороны =====================================
        # Идём по строкам «метка/значение» и раскладываем по типу метки:
        # «Cудья» → judge_names, «Cтороны» → sides (роль + ФИО).
        judge_names: list[str] = []
        sides: list[dict] = []
        for row in soup.select(ROW_SELECTOR):
            label_el = row.select_one(LABEL_SELECTOR)
            value_el = row.select_one(VALUE_SELECTOR)
            if label_el is None or value_el is None:
                continue

            label = _clean(label_el.get_text())
            if JUDGE_LABEL_RE.match(label):
                name = _clean(value_el.get_text())
                if name:
                    judge_names.append(name)
            elif SIDES_LABEL_RE.match(label):
                sides.extend(_parse_sides(value_el))

        # === ИСТОРИЯ СОСТОЯНИЙ: события =====================================
        # Отдельная таблица под <h3>История состояний</h3> — разбираем в события.
        events = _parse_state_history(soup)

        return {"judge_names": judge_names, "sides": sides, "events": events}
