"""Перечисления домена.

Отдельный модуль, потому что от enum'ов зависят почти все модели, а они — ни от чего.
Так граф импортов внутри app/models остаётся деревом.

Каждое перечисление используется ровно одной моделью, но лежат они вместе: искать
«какие вообще бывают значения» удобнее в одном месте.
"""
import enum


# Тип стороны по делу (аналог Django choices).
class SideType(str, enum.Enum):
    PLAINTIFF = "Истец"
    DEFENDANT = "Ответчик"
    OTHER = "Другое"


# Статус задачи поиска/синхронизации дела по УИД.
class SearchStatus(str, enum.Enum):
    PENDING = "pending"    # создана, ждёт обработки
    RUNNING = "running"    # выполняется
    SUCCESS = "success"    # дело найдено и сохранено
    FAILED = "failed"      # не удалось (после всех попыток)


# Уровень (звено) суда — по нему различаем справочники судов разных инстанций.
class CourtLevel(str, enum.Enum):
    MIRSUD = "mirsud"    # мировой суд
    GENERAL = "general"  # суд общей юрисдикции (районный/городской)
    APPEAL = "appeal"    # апелляционный
    KAS = "kas"          # кассационный


# Тип доменного изменения по делу — по одному значению на каждую ветку CaseChanges.
# Словарь закрытый (его задаёт сама сверка, а не портал), поэтому это enum, а не строка.
class OutboxEventType(str, enum.Enum):
    CASE_FIELD_CHANGED = "case_field_changed"  # изменилось скалярное поле дела
    EVENT_NEW = "event_new"                    # новая строка «Истории состояний»
    EVENT_UPDATED = "event_updated"            # у события поменялся документ/дата публикации
    EVENT_REMOVED = "event_removed"            # событие пропало со страницы
    PLACE_NEW = "place_new"                    # новая строка «Истории местонахождения»
    PLACE_UPDATED = "place_updated"            # у местонахождения поменялся комментарий
    PLACE_REMOVED = "place_removed"            # местонахождение пропало со страницы
    SESSION_NEW = "session_new"                # назначено судебное заседание
    SESSION_UPDATED = "session_updated"        # у заседания поменялись место/результат/основание
    SESSION_REMOVED = "session_removed"        # заседание снято со страницы
    DOCUMENT_NEW = "document_new"              # новый документ по делу
    DOCUMENT_REMOVED = "document_removed"      # документ пропал со страницы
    JUDGE_ADDED = "judge_added"                # к делу привязан судья
    JUDGE_REMOVED = "judge_removed"            # судья отвязан от дела
    SIDE_ADDED = "side_added"                  # к делу привязана сторона
    SIDE_REMOVED = "side_removed"              # сторона отвязана от дела

