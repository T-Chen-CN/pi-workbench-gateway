"""上游客户端：只走 loopback HTTP，不 import Hermes 任何模块。

三件事：

* header 重写（删客户端凭据、注入 `API_SERVER_KEY`）；
* 请求体上限 25MB；
* SSE：不压缩、不缓冲、透传 `Last-Event-ID`。
"""

from __future__ import annotations

import http.client
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import HubError

MAX_BODY_BYTES = 25 * 1024 * 1024

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "host",
    }
)

# 只透传这些「语义」头；其余（尤其是任何凭据）一律不传
FORWARD_HEADERS = frozenset(
    {
        "content-type",
        "accept",
        "accept-language",
        "idempotency-key",
        "last-event-id",
        "x-request-id",
    }
)


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    base_path: str

    @classmethod
    def parse(cls, base_url: str) -> "Target":
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"invalid upstream base url: {base_url!r}")
        return cls(
            scheme=parts.scheme,
            host=parts.hostname,
            port=parts.port or (443 if parts.scheme == "https" else 80),
            base_path=parts.path.rstrip("/"),
        )


class Upstream:
    """一次请求一个连接：并发安全，SSE 也不会被连接复用干扰。"""

    def __init__(
        self,
        base_url: str,
        bearer: str = "",
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        stream_timeout: float = 900.0,
    ):
        self.base_url = base_url
        self._target = Target.parse(base_url)
        self._bearer = bearer
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._stream_timeout = stream_timeout

    @property
    def base(self) -> str:
        return self.base_url

    def request_headers(self, request_headers: dict[str, str]) -> dict[str, str]:
        headers: dict[str, str] = {}
        for name, value in request_headers.items():
            lowered = name.lower()
            if lowered in HOP_BY_HOP:
                continue
            if lowered in FORWARD_HEADERS and value:
                headers[lowered] = value
        headers["accept-encoding"] = "identity"
        connection = "close" if "text/event-stream" in headers.get("accept", "") else "keep-alive"
        headers["connection"] = connection
        if self._bearer:
            headers["authorization"] = f"Bearer {self._bearer}"
        return headers

    def open(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes = b"",
        stream: bool = False,
    ) -> http.client.HTTPResponse:
        timeout = self._stream_timeout if stream else self._read_timeout
        connection_cls = (
            http.client.HTTPSConnection if self._target.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_cls(
            self._target.host, self._target.port, timeout=self._connect_timeout
        )
        try:
            connection.connect()
            connection.sock.settimeout(timeout)
            connection.putrequest(method, self._target.base_path + path, skip_host=True)
            connection.putheader("Host", self._host_header())
            for name, value in headers.items():
                connection.putheader(name, value)
            if body:
                connection.putheader("Content-Length", str(len(body)))
            elif method.upper() in ("POST", "PUT", "PATCH"):
                connection.putheader("Content-Length", "0")
            connection.endheaders()
            if body:
                connection.send(body)
            return connection.getresponse()
        except socket.timeout:
            connection.close()
            raise HubError("upstream_timeout", "api_server did not answer in time")
        except (ConnectionError, OSError) as exc:
            connection.close()
            raise HubError("upstream_unavailable", f"api_server is not reachable: {exc}")

    def _host_header(self) -> str:
        default = 443 if self._target.scheme == "https" else 80
        if self._target.port == default:
            return self._target.host
        return f"{self._target.host}:{self._target.port}"


def check_content_length(raw: str) -> int:
    """请求体上限：超了直接 413，不读正文。"""
    if not raw:
        return 0
    try:
        length = int(str(raw).strip())
    except (TypeError, ValueError):
        raise HubError("invalid_params", "Content-Length is not a number")
    if length < 0:
        raise HubError("invalid_params", "Content-Length is negative")
    if length > MAX_BODY_BYTES:
        raise HubError("payload_too_large", f"request body exceeds {MAX_BODY_BYTES} bytes")
    return length
