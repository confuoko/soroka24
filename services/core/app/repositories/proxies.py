"""Доступ к пулу прокси (Proxy) в БД."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import String, cast, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from app.models.database import Proxy


class ProxyRepository:
    """Чтение и выдача прокси из пула. Работает в рамках переданной сессии."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lease(self, portal: Optional[str] = None) -> Optional[Proxy]:
        """Взять прокси из пула: самый давно не использованный, и сразу пометить занятым.

        portal — ключ портала, на который собираемся идти (mos-sud / msudrf / spb).
        Годность у адресов разная: провайдер режет CONNECT выборочно, и прокси, берущий
        mos-sud, до msudrf может не дойти вовсе. Раньше это лечили закреплением одного
        адреса за движком msudrf прямо в коде клиента; теперь годность лежит в
        Proxy.portals, и выбирает по ней пул.

        Непроверенные адреса (portals пуст) из выдачи НЕ исключаются, но идут последними:
        пустой список значит «не проверяли», а не «не годится», и молча прятать такой
        прокси нельзя — иначе заведённый без --sites адрес никогда бы не использовался.
        А вот проверенные и НЕ подходящие порталу не выдаются никогда: поход через них
        гарантированно сгорит на туннеле.

        portal=None — фильтра нет (портал не определён, все прокси равны).

        Ротация по last_used_at: NULL (им ещё не ходили) идёт первым, дальше — по
        возрастанию времени последнего использования. Так нагрузка размазывается ровно
        и каждый прокси успевает «отдохнуть».

        FOR UPDATE SKIP LOCKED: если два воркера арендуют прокси одновременно, второй
        не встанет в очередь за той же строкой, а пропустит её и возьмёт следующую —
        то есть получит ДРУГОЙ прокси. Блокировка снимается на коммите session_scope(),
        поэтому держится миллисекунды, а не всё время работы браузера.

        None — пул пуст, все выключены или ни один адрес не годится для этого портала.
        """
        query = select(Proxy).where(Proxy.enabled.is_(True))
        if portal is not None:
            suits = Proxy.portals.contains([portal])
            unchecked = Proxy.portals == cast([], ARRAY(String))
            query = query.where(or_(suits, unchecked)).order_by(suits.desc())
        proxy = self._session.scalar(
            query
            .order_by(Proxy.last_used_at.asc().nullsfirst(), Proxy.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if proxy is not None:
            proxy.last_used_at = datetime.now(timezone.utc)
            # Сбрасываем отметку в БД сразу: сессия открыта с autoflush=False, и без
            # flush следующий SELECT в этой же сессии увидел бы старое last_used_at
            # и выдал тот же прокси — ротации бы не было.
            self._session.flush()
        return proxy

    def list_enabled(self) -> list[Proxy]:
        """Все включённые прокси (для проверки пула скриптом check_proxy.py)."""
        return list(
            self._session.scalars(
                select(Proxy).where(Proxy.enabled.is_(True)).order_by(Proxy.id)
            )
        )

    def set_enabled(self, proxy_ids: list[int], enabled: bool) -> int:
        """Включить или выключить прокси пачкой. Возвращает число изменённых строк.

        Нужно кнопкам в списке админки: включать и выключать прокси приходится часто
        (один портал пускает одни адреса, другой — другие), и открывать ради галки
        карточку каждого прокси неудобно.
        """
        if not proxy_ids:
            return 0
        result = self._session.execute(
            update(Proxy).where(Proxy.id.in_(proxy_ids)).values(enabled=enabled)
        )
        return result.rowcount

    def get_by_host_port(self, host: str, port: int) -> Optional[Proxy]:
        """Прокси по паре host+port (естественный ключ) — или None."""
        return self._session.scalar(
            select(Proxy).where(Proxy.host == host, Proxy.port == port)
        )
