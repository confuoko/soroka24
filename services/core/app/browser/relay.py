"""Локальный релей: HTTP-прокси без авторизации перед купленным прокси.

Зачем он нужен. Chromium не может ходить в наши прокси напрямую:

* по SOCKS5 он не умеет авторизацию логином/паролем вообще (ограничение движка);
* по HTTP наш прокси зависает, если в запросе CONNECT есть заголовок Host, —
  проверено на сокетах: без Host отдаёт «200 Connection established», с Host молчит
  до таймаута. А Chromium шлёт Host в CONNECT всегда, отключить это нельзя.

Поэтому между браузером и прокси ставится прослойка. Релей поднимается прямо в
процессе воркера на 127.0.0.1 со случайным портом, принимает от Chromium обычный
HTTP-прокси-трафик без авторизации, а наружу ходит уже сам: по SOCKS5 с логином и
паролем либо по HTTP запросом CONNECT без заголовка Host. Учётные данные при этом
остаются в БД и не уезжают ни в docker-compose, ни в командную строку браузера.
"""
import base64
import logging
import select
import socket
import socketserver
import struct
import threading
from urllib.parse import urlsplit

from app.browser.proxy import ProxySettings

logger = logging.getLogger(__name__)

# Размер куска при перекачке данных между браузером и прокси.
CHUNK = 65536
# Сколько ждать установки соединения с прокси.
CONNECT_TIMEOUT = 30
# Сколько ждать данных в простаивающем туннеле, прежде чем закрыть его.
IDLE_TIMEOUT = 300
# Ограничение на строку запроса и заголовок — защита от мусора в сокете.
MAX_LINE = 65536


class RelayError(RuntimeError):
    """Не удалось построить туннель через купленный прокси."""


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    """Прочитать ровно size байт (иначе — ошибка): у SOCKS5 ответы фиксированной длины."""
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RelayError("прокси закрыл соединение раньше времени")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _socks5_connect(sock: socket.socket, proxy: ProxySettings, host: str, port: int) -> None:
    """Попросить SOCKS5-прокси открыть туннель до host:port (RFC 1928 + 1929)."""
    # Приветствие: перечисляем поддерживаемые способы авторизации.
    if proxy.username:
        sock.sendall(b"\x05\x02\x00\x02")  # без авторизации или логин/пароль
    else:
        sock.sendall(b"\x05\x01\x00")      # только без авторизации
    _, method = _recv_exact(sock, 2)

    if method == 0x02:
        if not proxy.username:
            raise RelayError("прокси требует логин/пароль, а их нет в настройках")
        user = proxy.username.encode()
        password = (proxy.password or "").encode()
        sock.sendall(bytes([0x01, len(user)]) + user + bytes([len(password)]) + password)
        _, status = _recv_exact(sock, 2)
        if status != 0x00:
            raise RelayError("прокси не принял логин/пароль")
    elif method != 0x00:
        raise RelayError(f"прокси не принял ни один способ авторизации (код {method:#x})")

    # Запрос на соединение. Адрес отдаём доменом (тип 0x03) — пусть DNS резолвит
    # прокси, иначе имя суда разрешалось бы с нашего IP и выдавало нас.
    target = host.encode()
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(target)]) + target + struct.pack(">H", port))

    _, reply, _, address_type = _recv_exact(sock, 4)
    if reply != 0x00:
        raise RelayError(f"прокси отказал в соединении с {host}:{port} (код {reply:#x})")
    # Дочитываем адрес из ответа, чтобы в сокете не осталось лишних байт.
    if address_type == 0x01:
        _recv_exact(sock, 4 + 2)
    elif address_type == 0x04:
        _recv_exact(sock, 16 + 2)
    elif address_type == 0x03:
        length = _recv_exact(sock, 1)[0]
        _recv_exact(sock, length + 2)
    else:
        raise RelayError(f"прокси вернул неизвестный тип адреса {address_type:#x}")


def _http_connect(sock: socket.socket, proxy: ProxySettings, host: str, port: int) -> None:
    """Попросить HTTP-прокси открыть туннель до host:port методом CONNECT.

    Заголовок Host НЕ отправляем сознательно: именно на нём этот прокси зависает
    (см. докстринг модуля). По стандарту в CONNECT он и не обязателен.
    """
    request = f"CONNECT {host}:{port} HTTP/1.1\r\n"
    if proxy.username:
        credentials = f"{proxy.username}:{proxy.password or ''}".encode()
        token = base64.b64encode(credentials).decode()
        request += f"Proxy-Authorization: Basic {token}\r\n"
    request += "\r\n"
    sock.sendall(request.encode())

    # Ответ читаем побайтно до конца заголовков: тело за ними — уже данные туннеля.
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(1)
        if not chunk:
            raise RelayError("прокси закрыл соединение, не ответив на CONNECT")
        response += chunk
        if len(response) > MAX_LINE:
            raise RelayError("прокси прислал слишком длинный ответ на CONNECT")

    status_line = response.split(b"\r\n", 1)[0].decode(errors="replace")
    parts = status_line.split()
    if len(parts) < 2 or not parts[1].startswith("2"):
        raise RelayError(f"прокси отказал в CONNECT: {status_line}")


def open_tunnel(proxy: ProxySettings, host: str, port: int) -> socket.socket:
    """Открыть через купленный прокси туннель до host:port и вернуть сокет."""
    sock = socket.create_connection((proxy.host, proxy.port), CONNECT_TIMEOUT)
    try:
        sock.settimeout(CONNECT_TIMEOUT)
        if proxy.scheme == "socks5":
            _socks5_connect(sock, proxy, host, port)
        else:
            _http_connect(sock, proxy, host, port)
    except Exception:
        sock.close()
        raise
    sock.settimeout(None)
    return sock


def _pump(client: socket.socket, upstream: socket.socket) -> None:
    """Перекачивать данные в обе стороны, пока кто-нибудь не закроет соединение."""
    sockets = [client, upstream]
    while True:
        readable, _, broken = select.select(sockets, [], sockets, IDLE_TIMEOUT)
        if broken or not readable:
            return  # ошибка сокета или туннель простаивает — закрываем
        for source in readable:
            target = upstream if source is client else client
            try:
                data = source.recv(CHUNK)
            except OSError:
                return
            if not data:
                return
            try:
                target.sendall(data)
            except OSError:
                return


def _split_host_port(authority: str, default_port: int) -> tuple[str, int]:
    """Разобрать «host:port» (порт необязателен) в пару значений."""
    host, separator, port = authority.rpartition(":")
    if separator and port.isdigit():
        return host, int(port)
    return authority, default_port


class _Handler(socketserver.StreamRequestHandler):
    """Одно соединение от Chromium: разбираем запрос и уводим его в купленный прокси."""

    # Без буферизации на чтении: заголовки читаем построчно, а дальше работаем с
    # сокетом напрямую — иначе часть данных застряла бы в буфере rfile.
    rbufsize = 0
    disable_nagle_algorithm = True

    def handle(self) -> None:
        request_line = self.rfile.readline(MAX_LINE)
        if not request_line:
            return
        parts = request_line.split()
        if len(parts) < 3:
            self._reject(400, "Bad Request")
            return
        method, target = parts[0].upper(), parts[1].decode(errors="replace")

        headers = []
        while True:
            line = self.rfile.readline(MAX_LINE)
            if not line or line in (b"\r\n", b"\n"):
                break
            headers.append(line)

        proxy: ProxySettings = self.server.upstream
        try:
            if method == b"CONNECT":
                # HTTPS: браузер просит туннель, дальше внутри идёт TLS.
                host, port = _split_host_port(target, 443)
                upstream = open_tunnel(proxy, host, port)
                self.connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:
                # Обычный HTTP: строка запроса в absolute-form (http://host/path).
                url = urlsplit(target)
                if not url.hostname:
                    self._reject(400, "Bad Request")
                    return
                host, port = url.hostname, url.port or 80
                upstream = open_tunnel(proxy, host, port)
                path = url.path or "/"
                if url.query:
                    path = f"{path}?{url.query}"
                # Внутри туннеля прокси уже не участвует — переписываем строку
                # запроса в обычный вид и выкидываем прокси-заголовки.
                rebuilt = f"{method.decode()} {path} {parts[2].decode()}\r\n".encode()
                kept = [h for h in headers if not h.lower().startswith(b"proxy-")]
                upstream.sendall(rebuilt + b"".join(kept) + b"\r\n")
        except Exception as exc:
            logger.warning("Релей: не удалось открыть туннель до %s: %s", target, exc)
            self._reject(502, "Bad Gateway")
            return

        try:
            _pump(self.connection, upstream)
        finally:
            upstream.close()

    def _reject(self, code: int, reason: str) -> None:
        try:
            self.connection.sendall(f"HTTP/1.1 {code} {reason}\r\n\r\n".encode())
        except OSError:
            pass

    def handle_error(self, *args) -> None:
        # Разрыв соединения браузером — обычное дело, трейс в логи не тащим.
        logger.debug("Релей: соединение оборвалось", exc_info=True)


class _RelayServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, upstream: ProxySettings) -> None:
        self.upstream = upstream
        # Порт 0 — операционная система выдаст свободный сама.
        super().__init__(("127.0.0.1", 0), _Handler)

    def handle_error(self, request, client_address) -> None:
        logger.debug("Релей: ошибка обработки соединения", exc_info=True)


class ProxyRelay:
    """Локальный HTTP-прокси без авторизации, ведущий в купленный прокси.

    Контекст-менеджер: живёт ровно столько, сколько открыт браузер.

        with ProxyRelay(proxy) as relay:
            ... chromium.launch(proxy=relay.to_playwright()) ...
    """

    def __init__(self, upstream: ProxySettings) -> None:
        self._upstream = upstream
        self._server: _RelayServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "ProxyRelay":
        self._server = _RelayServer(self._upstream)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="proxy-relay", daemon=True
        )
        self._thread.start()
        logger.debug("Релей поднят на %s -> %s", self.address, self._upstream)
        return self

    def __exit__(self, *exc) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("Релей ещё не запущен")
        return self._server.server_address[1]

    @property
    def address(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def to_playwright(self) -> dict:
        """Настройки для chromium.launch(proxy=...): адрес релея, без учётных данных."""
        return {"server": self.address}
