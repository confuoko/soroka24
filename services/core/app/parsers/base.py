"""Базовый интерфейс парсера карточки дела (стратегия под тип HTML-страницы).

Парсер — чистое преобразование HTML -> данные: без БД, без сети. Под каждый тип
страницы (см. CourtClient.page_type) — свой класс-наследник; выбор — в registry.py.
"""
from abc import ABC, abstractmethod


class CaseParser(ABC):
    """Интерфейс парсера.

    page_type — тип страницы, по которому реестр выбирает нужную стратегию
    (совпадает с CourtClient.page_type у соответствующего клиента суда).
    """

    page_type: str

    @abstractmethod
    def parse(self, html: str) -> dict:
        """Разобрать HTML карточки в данные дела.

        Возвращает словарь (любой список пуст, если данных нет; скалярное поле —
        None, если соответствующей метки на странице нет: набор меток различается
        по типам дел, см. MoscowTypeAParser):
            {
              # Скалярные поля Case — их подхватывает CaseRepository.upsert_by_uid.
              "application_number": "М-2342/463/2026" | None,
              "incoming_number": "02609/2026" | None,
              "code": "02-0634/2/2026" | None,          # «Номер дела [~ материала]»
              "receipt_date": date | None,              # «Дата поступления»
              "registration_date": date | None,         # «Дата регистрации»
              "first_instance_date": date | None,
              "first_instance_decision": "Удовлетворено, 21.05.2026" | None,
              "decision_effective_date": date | None,
              "superior_case_number": "10-0014/2025" | None,  # номер ДРУГОГО дела
              "category": "124 - О взыскании платы за жилую площадь..." | None,
              "status": "Зарегистрировано (10.07.2026)" | None,

              "judge_names": ["Каурова Д.С.", ...],
              # Роль — ровно как на портале: «Истец», «Взыскатель», «Должник»,
              # «Подсудимый», «Обвиняемый»… Подсудимый и обвиняемый лежат отдельными
              # метками карточки, но попадают сюда же.
              "sides": [{"role": "Истец", "full_name": "..."}, ...],
              "events": [
                {"event_date": date, "state_description": "...",
                 "document_str": "..." | None},
                ...
              ],
              "place_history": [
                {"place_date": date, "place_description": "В канцелярии",
                 "comment": "..." | None},
                ...
              ],
              # Судебные заседания. session_date — дата И время: портал отдаёт их одной
              # колонкой, и время входит в identity заседания (см. court_session_uid).
              # У приказных дел вкладки заседаний нет — список пустой.
              "court_sessions": [
                {"session_date": datetime, "place": "2 - 124489, Зеленоград..." | None,
                 "stage": "Судебное заседание", "result": "Отложено" | None,
                 "basis": "Неявка подсудимого" | None},
                ...
              ],
              # Документы по делу — ТОЛЬКО метаданные: ни текст документа, ни ссылку на
              # файл не отдаём и не храним. Порядок строк сохраняется: из него считается
              # номер повторения в identity (см. document_uid) — портал отдаёт до 21
              # одинаковой строки «Приложение» за одну дату.
              "documents": [
                {"document_date": date, "document_type": "Судебный приказ"},
                ...
              ],
            }
        """
