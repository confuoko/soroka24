"""Доступ к документам дела (Document) в БД: детерминированный uid + сверка со страницей.

В БД идут только метаданные документа — дата и вид. Ни текст, ни файл, ни ссылку на файл мы
не храним (третью колонку таблицы портала парсер не читает вовсе).
"""
import uuid
from collections import defaultdict
from datetime import date

from sqlalchemy.orm import Session

from app.models.database import Case, Document

# Фиксированный namespace для uid документов (задан один раз, менять нельзя — иначе uid
# всех документов «поедут» и повторный парсинг перестанет их узнавать).
# Свой, отдельный от остальных сущностей: иначе строки разных сущностей с одинаковым
# текстом и датой получили бы один и тот же uid.
DOCUMENT_UID_NAMESPACE = uuid.UUID("2f7b91c4-6d3e-5a08-9c1f-7b45e0a2d836")


def document_uid(
    card_key: str, document_date: date, document_type: str, occurrence: int
) -> uuid.UUID:
    """
    Детерминированный uid документа из обязательных (identity) полей.

    identity = карточка + дата + вид документа + номер повторения.

    КАРТОЧКА, а не дело: card_key — это «УИД | код суда | номер дела»
    (Case.card_key). По одному УИД карточек бывает несколько, а uid здесь уникален
    глобально — считай мы его от УИД, строки соседних карточек столкнулись бы.

    occurrence — сколько таких же строк встретилось ВЫШЕ на этой же странице. Портал
    отдаёт по несколько одинаковых строк за одну дату (у дела 77MS0002-01-2026-001597-10 —
    21 «Приложение» за 17.07.2026), различить их в разметке нечем: ни id, ни номера. Но
    терять их нельзя, поэтому в ключ идёт позиция в группе одинаковых.

    Номер повторения внутри группы, а не номер строки во всей таблице: появление документа
    другого вида выше не должно менять uid соседей, иначе повторный парсинг посчитал бы их
    удалёнными и создал заново.
    """
    key = "|".join(
        [card_key, document_date.isoformat(), document_type, str(occurrence)]
    )
    return uuid.uuid5(DOCUMENT_UID_NAMESPACE, key)


class DocumentRepository:
    """Чтение и запись документов дела. Работает в рамках переданной сессии."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def sync_documents(
        self, case: Case, documents_data: list[dict]
    ) -> tuple[list[Document], list[Document]]:
        """Привести документы дела к тому, что сейчас на странице.

        Возвращает (new_documents, removed_documents):
        - uid новый                            → создаём документ  → new_documents;
        - uid документа больше нет на странице → удаляем           → removed_documents.

        Ветки updated здесь нет: изменяемых полей у документа не осталось — дата и вид
        входят в identity, а текст мы не храним.

        Страница — источник истины. Порядок важен: сначала одним проходом по documents_data
        наполняем desired_uids и добавляем новое, и только потом удаляем то, чей uid не
        встретился на странице.
        """
        existing = {d.uid: d for d in case.documents}
        desired_uids: set[uuid.UUID] = set()

        new_documents: list[Document] = []
        # Сколько строк с такой парой (дата, вид) уже встретилось выше на странице.
        seen: dict[tuple[date, str], int] = defaultdict(int)

        # 1. Проход по документам, которые есть на актуальной странице
        for item in documents_data:
            group = (item["document_date"], item["document_type"])
            uid = document_uid(case.card_key, *group, occurrence=seen[group])
            seen[group] += 1
            # Номер повторения делает uid разными по построению, поэтому дубля здесь быть
            # не должно. Проверка — страховка от повторной вставки того же uid: она уронила
            # бы commit на ix_document_uid вместе со всей транзакцией дела.
            if uid in desired_uids:
                continue
            # Добавляем uid в список документов, которые мы хотим увидеть в БД
            desired_uids.add(uid)
            # Если документа с таким uid ещё нет - создаём новый
            if existing.get(uid) is None:
                document = Document(
                    uid=uid,
                    document_date=item["document_date"],
                    document_type=item["document_type"],
                )
                case.documents.append(document)
                new_documents.append(document)

        # Удаляем документы, пропавшие со страницы (cascade delete-orphan уберёт их из БД).
        removed_documents = [d for d in case.documents if d.uid not in desired_uids]
        for document in removed_documents:
            case.documents.remove(document)

        return new_documents, removed_documents
