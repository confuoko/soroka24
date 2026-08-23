"""Релей между Chromium и купленным прокси: туннель, авторизация и заголовок Host.

Настоящий прокси тут не нужен — вместо него поднимаются заглушки на 127.0.0.1,
которые записывают, что именно им прислал релей. Проверяем ровно то, из-за чего релей
и появился: Chromium не может ни авторизоваться по SOCKS5, ни отправить CONNECT без
заголовка Host, а наш прокси на Host зависает.
"""
import socket
import socketserver
import struct
import threading

import pytest

from app.browser.proxy import ProxySettings
from app.browser.relay import ProxyRelay

TARGET = "mos-sud.ru:443"


class _FakeUpstream(socketserver.ThreadingTCPServer):
    """Заглушка купленного прокси: пишет полученное в recorded и отвечает 'pong'."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, handler_cls) -> None:
        self.recorded: list = []
        super().__init__(("127.0.0.1", 0), handler_cls)

    @property
    def port(self) -> int:
        return self.server_address[1]


class _HttpProxyHandler(socketserver.StreamRequestHandler):
    """Заглушка HTTP-прокси: принимает CONNECT и становится другим концом туннеля."""

    def handle(self) -> None:
        request_line = self.rfile.readline()
        headers = []
        while True:
            line = self.rfile.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            headers.append(line)
        self.server.recorded.append({"request_line": request_line, "headers": headers})

        self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        self.wfile.flush()
        # Дальше внутри туннеля идут данные клиента — отвечаем на них.
        self.server.recorded.append({"tunnelled": self.rfile.readline()})
        self.wfile.write(b"pong\n")
        self.wfile.flush()


class _Socks5Handler(socketserver.StreamRequestHandler):
    """Заглушка SOCKS5-прокси, требующая авторизацию логином и паролем."""

    def handle(self) -> None:
        version, method_count = self.rfile.read(2)
        methods = self.rfile.read(method_count)
        self.server.recorded.append({"version": version, "methods": methods})
        self.wfile.write(b"\x05\x02")  # выбираем авторизацию логином/паролем
        self.wfile.flush()

        self.rfile.read(1)  # версия подпротокола авторизации
        user = self.rfile.read(self.rfile.read(1)[0])
        password = self.rfile.read(self.rfile.read(1)[0])
        self.server.recorded.append({"user": user, "password": password})
        self.wfile.write(b"\x01\x00")  # авторизация принята
        self.wfile.flush()

        self.rfile.read(4)  # версия, команда, резерв, тип адреса (всегда домен)
        host = self.rfile.read(self.rfile.read(1)[0])
        port = struct.unpack(">H", self.rfile.read(2))[0]
        self.server.recorded.append({"host": host, "port": port})
        self.wfile.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")  # успех
        self.wfile.flush()

        self.server.recorded.append({"tunnelled": self.rfile.readline()})
        self.wfile.write(b"pong\n")
        self.wfile.flush()


@pytest.fixture
def upstream():
    """Поднять заглушку прокси. Возвращает настройщик: указываешь обработчик и схему."""
    servers = []

    def _start(handler_cls, scheme: str) -> tuple[_FakeUpstream, ProxySettings]:
        server = _FakeUpstream(handler_cls)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        settings = ProxySettings(scheme, "127.0.0.1", server.port, "user430658", "84kwmy")
        return server, settings

    yield _start

    for server in servers:
        server.shutdown()
        server.server_close()


def _connect_status(relay: ProxyRelay) -> bytes:
    """Только строка ответа релея на CONNECT — в туннель ничего не пишем.

    Отдельный хелпер нужен для проверки ОТКАЗОВ. При отказе релей отвечает 502 и сразу
    закрывает соединение; если после этого попытаться что-то в него отправить, как делает
    _through_relay, запись прилетит в уже закрытый сокет. На Windows это даёт
    ConnectionAbortedError — и не всегда, а в зависимости от того, дошёл ли RST. Именно
    из-за этой гонки тест на 502 в старом core вёл себя недетерминированно: шесть
    прогонов подряд на одном и том же коде давали четыре падения.

    Писать в туннель после отказа и незачем: туннеля нет.
    """
    client = socket.create_connection(("127.0.0.1", relay.port), 10)
    client.settimeout(10)
    try:
        request = "CONNECT {t} HTTP/1.1\r\nHost: {t}\r\n\r\n".format(t=TARGET)
        client.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = client.recv(256)
            if not chunk:
                break
            response += chunk
        return response.split(b"\r\n", 1)[0]
    finally:
        client.close()


def _through_relay(relay: ProxyRelay, payload: bytes = b"ping\n") -> tuple[bytes, bytes]:
    """Сходить через релей так, как это делает Chromium: CONNECT с заголовком Host."""
    client = socket.create_connection(("127.0.0.1", relay.port), 10)
    client.settimeout(10)
    try:
        client.sendall(
            f"CONNECT {TARGET} HTTP/1.1\r\nHost: {TARGET}\r\n\r\n".encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = client.recv(256)
            if not chunk:
                break
            response += chunk
        status_line = response.split(b"\r\n", 1)[0]

        client.sendall(payload)
        return status_line, client.recv(256)
    finally:
        client.close()


# ------------------------------------------------------------------ HTTP-прокси
def test_http_upstream_connect_has_no_host_header(upstream) -> None:
    """Главный регресс: в CONNECT к прокси не должно быть заголовка Host.

    Именно на нём купленный прокси зависает намертво — проверено на сокетах. Chromium
    Host шлёт всегда, поэтому релей обязан его не пропускать.
    """
    server, settings = upstream(_HttpProxyHandler, "http")

    with ProxyRelay(settings) as relay:
        status_line, _ = _through_relay(relay)

    assert status_line.startswith(b"HTTP/1.1 200")
    request = server.recorded[0]
    assert request["request_line"].startswith(f"CONNECT {TARGET}".encode())
    assert not any(h.lower().startswith(b"host:") for h in request["headers"])


def test_http_upstream_gets_credentials(upstream) -> None:
    """Логин и пароль уходят прокси заголовком Proxy-Authorization, а не браузером."""
    server, settings = upstream(_HttpProxyHandler, "http")

    with ProxyRelay(settings) as relay:
        _through_relay(relay)

    headers = server.recorded[0]["headers"]
    assert any(h.lower().startswith(b"proxy-authorization: basic ") for h in headers)


# ---------------------------------------------------------------- SOCKS5-прокси
def test_socks5_upstream_authenticates(upstream) -> None:
    """По SOCKS5 релей проходит авторизацию логином/паролем — Chromium так не умеет."""
    server, settings = upstream(_Socks5Handler, "socks5")

    with ProxyRelay(settings) as relay:
        status_line, _ = _through_relay(relay)

    assert status_line.startswith(b"HTTP/1.1 200")
    assert server.recorded[1] == {"user": b"user430658", "password": b"84kwmy"}


def test_socks5_upstream_resolves_domain_remotely(upstream) -> None:
    """Адрес уходит доменом, а не IP: DNS резолвит прокси, иначе мы бы себя выдали."""
    server, settings = upstream(_Socks5Handler, "socks5")

    with ProxyRelay(settings) as relay:
        _through_relay(relay)

    assert server.recorded[2] == {"host": b"mos-sud.ru", "port": 443}


# ------------------------------------------------------------- сквозной туннель
@pytest.mark.parametrize(
    "handler_cls, scheme",
    [(_HttpProxyHandler, "http"), (_Socks5Handler, "socks5")],
    ids=["http", "socks5"],
)
def test_data_flows_both_ways(upstream, handler_cls, scheme) -> None:
    """Туннель прозрачен: что отправил браузер — дошло, что ответил сайт — вернулось."""
    server, settings = upstream(handler_cls, scheme)

    with ProxyRelay(settings) as relay:
        _, answer = _through_relay(relay, payload=b"ping\n")

    assert answer == b"pong\n"
    assert {"tunnelled": b"ping\n"} in server.recorded


# ------------------------------------------------------------------- отказы
def test_relay_answers_502_when_upstream_is_dead() -> None:
    """Прокси не отвечает → релей отдаёт браузеру 502, а не молчит до таймаута."""
    # Порт, на котором заведомо никто не слушает: занимаем и сразу освобождаем.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    with ProxyRelay(ProxySettings("http", "127.0.0.1", dead_port)) as relay:
        status_line = _connect_status(relay)

    assert status_line.startswith(b"HTTP/1.1 502")


def test_relay_stops_after_exit() -> None:
    """На выходе из контекста релей закрывается — порт не остаётся висеть."""
    with ProxyRelay(ProxySettings("http", "127.0.0.1", 1)) as relay:
        port = relay.port

    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), 2).close()
