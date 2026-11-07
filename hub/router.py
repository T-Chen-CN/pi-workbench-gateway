"""极简路由：路径模板 + 方法 + 鉴权级别。

刻意不用第三方框架。`hub/router.py` 只做匹配与请求/响应容器，
鉴权与代理由 `server.py` 按路由声明的 `auth` 级别统一执行。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlsplit

from .errors import HubError, not_found

JSON = "application/json; charset=utf-8"

AUTH_NONE = "none"
AUTH_DEVICE = "device"
AUTH_ADMIN = "admin"
AUTH_SELF_OR_ADMIN = "self_or_admin"
AUTH_DEVICE_OR_ADMIN = "device_or_admin"


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]  # key 已小写
    body: bytes
    client_ip: str
    query_string: str = ""
    params: dict[str, str] = field(default_factory=dict)
    device: Any = None  # auth.Device | None

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def json(self) -> dict[str, Any]:
        return _parse_json(self.body, self.header("content-type"))


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = JSON

    def header_list(self) -> list[tuple[str, str]]:
        out = [(k, v) for k, v in self.headers.items()]
        if self.content_type:
            out.append(("Content-Type", self.content_type))
        out.append(("Content-Length", str(len(self.body))))
        return out


class StreamResponse:
    """SSE：不设 Content-Length，逐块 flush，收尾关闭连接。"""

    def __init__(
        self,
        chunks: Iterable[bytes],
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        content_type: str = "text/event-stream; charset=utf-8",
    ):
        self.status = status
        self.headers = dict(headers or {})
        self.content_type = content_type
        self.chunks = chunks


Handler = Callable[[Request], Response | StreamResponse]


@dataclass
class Route:
    methods: tuple[str, ...]
    pattern: re.Pattern[str]
    handler: Handler
    auth: str = AUTH_DEVICE
    scope: str | None = None
    name: str = ""


def _compile(template: str) -> re.Pattern[str]:
    """`/v1/runs/{run_id}/events` → 正则；参数名进 groupdict。"""
    parts: list[str] = []
    for segment in template.strip("/").split("/"):
        if segment.startswith("{") and segment.endswith("}"):
            parts.append(f"(?P<{segment[1:-1]}>[^/]+)")
        else:
            parts.append(re.escape(segment))
    return re.compile("^/" + "/".join(parts) + "$")


class Router:
    def __init__(self) -> None:
        self._routes: list[Route] = []

    def add(
        self,
        methods: str | Iterable[str],
        template: str,
        handler: Handler,
        *,
        auth: str = AUTH_DEVICE,
        scope: str | None = None,
    ) -> None:
        if isinstance(methods, str):
            methods = (methods,)
        self._routes.append(
            Route(
                methods=tuple(m.upper() for m in methods),
                pattern=_compile(template),
                handler=handler,
                auth=auth,
                scope=scope,
                name=template,
            )
        )

    def match(self, method: str, path: str) -> tuple[Route, dict[str, str]]:
        path_only = urlsplit(path).path or "/"
        allowed = False
        for route in self._routes:
            found = route.pattern.match(path_only)
            if not found:
                continue
            allowed = True
            if method.upper() in route.methods:
                return route, dict(found.groupdict())
        if allowed:
            raise HubError("method_not_allowed", f"{method} is not allowed on {path_only}")
        raise not_found(path_only, method)


def parse_query(raw: str) -> dict[str, str]:
    parsed = parse_qs(raw, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items() if v}


def json_response(payload: Any, *, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(
        status=status,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=dict(headers or {}),
    )


def _parse_json(body: bytes, content_type: str) -> dict[str, Any]:
    if not body:
        return {}
    if "application/json" not in (content_type or "").lower():
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HubError("invalid_params", "请求体不是合法 JSON")
    if not isinstance(data, dict):
        raise HubError("invalid_params", "请求体必须是 JSON 对象")
    return data
