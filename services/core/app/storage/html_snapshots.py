"""Снапшоты HTML страниц суда: складываем в S3 то, что реально распарсили.

Зачем: раньше HTML жил только внутри Celery-таска и выбрасывался, поэтому по факту
нельзя было ни перепроверить diff, ни отладить парсер на настоящей разметке.

Раскладка в бакете:
    <бакет>/html_snapshots/<уид>/<код суда>-<номер дела>/<уид>_<время>.html.gz
    <бакет>/html_snapshots/<уид>/failed/<уид>_<время>.html.gz
Папка на дело + папка на карточку + УИД в имени файла: скачанный поодиночке объект
остаётся понятным. gzip — карточка дела весит ~500 КБ текста и сжимается примерно в 8 раз.

Средний уровень (карточка) нужен потому, что УИД сквозной: по одному УИД бывает несколько
карточек — в разных судах (дело пошло по инстанциям) и в одном суде с разными номерами
(приказное производство, затем исковое). Без него их разметка ложилась бы вперемешку в
одну папку, а по ключу нельзя было бы понять, к какой карточке относится снапшот.

Страницы отказа лежат в failed/ без уровня карточки: в момент отказа ни суда, ни номера
дела мы обычно ещё не знаем — до таблицы результатов дело не дошло.

Подпапка failed/ — страницы, на которых парсинг упал (капча, блокировка, изменившаяся
разметка). Они лежат внутри папки дела, чтобы всё по делу было в одном префиксе, но
отдельно от карточек: карточки — данные, отказы — материал для разбора инцидента, и
путать их нельзя. Кто перебирает снапшоты дела, должен отфильтровать их через
is_failure_key() — иначе капча приедет в парсер как карточка.

В БД пишется ключ объекта и имя бакета (см. app/monitoring/parse_history.py).
"""
import gzip
import hashlib
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from app.config import HTML_SNAPSHOT_PREFIX, S3_BUCKET
from app.storage.s3 import get_object, put_object

# Формат времени в имени объекта: сортируемый и без двоеточий, чтобы файл можно было
# без переименования сохранить на диск (в Windows двоеточие в имени запрещено).
# Общий для всех объектов в бакете — им же именуются картинки капчи.
TS_FORMAT = "%Y-%m-%dT%H-%M-%SZ"

# Подпапка внутри папки дела для страниц, на которых упали.
FAILURE_SUBDIR = "failed"


def case_id_from_url(source_url: str) -> str:
    """Идентификатор дела из ссылки на карточку (параметр case_id).

    Если параметра нет — короткий хэш адреса: объект всё равно ляжет предсказуемо
    и не смешается с чужими.
    """
    case_id = parse_qs(urlsplit(source_url).query).get("case_id", [None])[0]
    if case_id:
        return case_id
    return "url-" + hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:12]


def url_label(source_url: str) -> str:
    """Имя папки в бакете для дела, УИД которого ещё неизвестен.

    Дела с порталов без поиска по УИД приходят ссылкой, и если поход упал до того,
    как мы прочитали страницу, класть страницу отказа под УИД просто некуда.
    Приставка url- сразу отличает такую папку от настоящего УИД.
    """
    host = urlsplit(source_url).hostname or "unknown-host"
    return f"url-{host}_{case_id_from_url(source_url)}"


def snapshot_sha256(html: str) -> str:
    """Хэш содержимого страницы — по нему видно, изменилась ли разметка с прошлого раза."""
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def card_folder(court_code: str, case_code: str) -> str:
    """Имя папки карточки внутри папки дела: «<код суда>-<номер дела>».

    Слэши из номера дела («05-0444/1/2026») заменяем на дефисы: в ключе объекта слэш —
    разделитель уровней, и без замены одна карточка расползлась бы на три папки.
    """
    slug = case_code.strip().replace("\\", "/").replace("/", "-")
    return f"{court_code}-{slug}"


def snapshot_key(
    uid: str, fetched_at: datetime, failed: bool = False, card: str | None = None
) -> str:
    """Ключ объекта в бакете для этой карточки и момента получения страницы.

    card — имя папки карточки из card_folder(). Без него объект ложится прямо в папку
    дела: так лежат старые снапшоты, снятые до появления уровня карточки.

    failed=True — страница, на которой парсинг упал: кладём в подпапку failed/. Уровень
    карточки там не используется, её в момент отказа обычно ещё не знают.
    """
    if failed:
        folder = f"{uid}/{FAILURE_SUBDIR}"
    elif card:
        folder = f"{uid}/{card}"
    else:
        folder = uid
    return f"{HTML_SNAPSHOT_PREFIX}/{folder}/{uid}_{fetched_at.strftime(TS_FORMAT)}.html.gz"


def is_failure_key(key: str) -> bool:
    """Это ключ страницы-отказа (лежит в подпапке failed/), а не карточки дела?"""
    return f"/{FAILURE_SUBDIR}/" in key


def save_snapshot(
    uid: str,
    html: str,
    fetched_at: datetime,
    failed: bool = False,
    card: str | None = None,
) -> dict:
    """Сжать HTML и положить в S3.

    failed=True — страница отказа, card — папка карточки (см. snapshot_key).

    Возвращает {"html_bucket", "html_key", "html_sha256", "html_size"}: бакет, ключ
    объекта, хэш исходного (несжатого) текста и его размер в байтах.
    """
    raw = html.encode("utf-8")
    key = snapshot_key(uid, fetched_at, failed=failed, card=card)

    # mtime=0 — чтобы одинаковый HTML давал побайтово одинаковый архив (иначе gzip
    # подмешивает в заголовок текущее время и объекты нельзя сравнивать напрямую).
    put_object(key, gzip.compress(raw, mtime=0))

    return {
        "html_bucket": S3_BUCKET,
        "html_key": key,
        "html_sha256": hashlib.sha256(raw).hexdigest(),
        "html_size": len(raw),
    }


def read_snapshot(key: str) -> str:
    """Прочитать сохранённый снапшот из S3 и распаковать в текст (для отладки)."""
    return gzip.decompress(get_object(key)).decode("utf-8")
