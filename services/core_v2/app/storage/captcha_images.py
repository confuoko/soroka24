"""Картинки капчи в S3: складываем то, что показал портал, прежде чем разгадывать.

Зачем хранить: по картинке видно, что именно нам подсунули, когда разгадка не подошла
или страница так и не открылась. Без неё в логах остаётся только «не прошли проверку».

Раскладка:
    <бакет>/captcha/<хост участка>/<case_id>/<время>.png

Почему по ссылке, а не по УИД: капча показывается ДО карточки дела, то есть в этот
момент УИД ещё неизвестен — за ним мы, собственно, и шли. Хост с номером участка и
case_id из ссылки однозначно указывают на дело и доступны сразу.
"""
import logging
from datetime import datetime
from urllib.parse import urlsplit

from app.config import CAPTCHA_PREFIX, S3_BUCKET
from app.storage.html_snapshots import TS_FORMAT, case_id_from_url
from app.storage.s3 import put_object

logger = logging.getLogger(__name__)


def _case_folder(source_url: str) -> str:
    """Папка дела внутри префикса: «<хост>/<case_id>».

    Если case_id в ссылке нет (формат портала поменялся или пришла ссылка другого
    вида) — подставляем короткий хэш адреса. Так объект всё равно ляжет предсказуемо
    и не смешается с чужими.
    """
    host = urlsplit(source_url).hostname or "unknown-host"
    return f"{host}/{case_id_from_url(source_url)}"


def captcha_key(source_url: str, taken_at: datetime) -> str:
    """Ключ объекта в бакете для картинки капчи с этой страницы."""
    return f"{CAPTCHA_PREFIX}/{_case_folder(source_url)}/{taken_at.strftime(TS_FORMAT)}.png"


def save_captcha(source_url: str, png: bytes, taken_at: datetime) -> dict | None:
    """Положить картинку капчи в S3.

    Возвращает {"captcha_bucket", "captcha_key", "captcha_size"} либо None, если
    сохранить не вышло. Ошибку хранилища намеренно глотаем: не смогли отложить
    картинку — не повод отказываться от разгадывания, ради которого всё и затевалось.
    """
    key = captcha_key(source_url, taken_at)
    try:
        put_object(key, png, content_type="image/png")
    except Exception as exc:
        logger.warning("Не удалось сохранить картинку капчи %s: %s", key, exc)
        return None
    return {"captcha_bucket": S3_BUCKET, "captcha_key": key, "captcha_size": len(png)}
