"""Hermes 与本插件之间的唯一缝：纯标准库 HTTP 服务，不知道 Hermes 存在。

生命周期由 adapter 驱动：`start()` 起 daemon 线程，`stop()` 收尾。
端口被占用时 `start()` 抛 `OSError`，由 adapter 转成「未连接」，绝不打穿 gateway。
"""

from __future__ import annotations

import logging
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import auth
from .app import VERSION, HubApp
from .errors import ERROR_TYPE, HubError
from .maintenance import Maintenance
from .router import (
    AUTH_ADMIN,
    AUTH_DEVICE,
    AUTH_DEVICE_OR_ADMIN,
    AUTH_NONE,
    AUTH_SELF_OR_ADMIN,
    JSON,
    Request,
    Response,
    Router,
    StreamResponse,
    parse_query,
)
from .routes import register_all
from .upstream import check_content_length

logger = logging.getLogger(__name__)

SERVER_SOFTWARE = f"pi-workbench-gateway/{VERSION}"


class _HubHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, app, router):
        self.app = app
        self.router = router
        super().__init__(address, handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = SERVER_SOFTWARE
    sys_version = ""

    # -- 日志：只记方法与路径，永不记 header（那里有 Authorization） ----------

    def log_message(self, fmt: str, *args) -> None:
        logger.debug("[hub] %s", fmt % args)

    def version_string(self) -> str:
        return SERVER_SOFTWARE

    # -- 方法 ---------------------------------------------------------------

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_HEAD(self) -> None:
        self._handle("GET", body=False)

    # -- 主流程 -------------------------------------------------------------

    def _handle(self, method: str, *, body: bool = True) -> None:
        app: HubApp = self.server.app
        router: Router = self.server.router
        parts = urlsplit(self.path)
        path = parts.path or "/"
        try:
            length = check_content_length(self.headers.get("Content-Length", ""))
            payload = self.rfile.read(length) if (length and body) else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            request = Request(
                method=method,
                path=path,
                query=parse_query(parts.query),
                query_string=parts.query,
                headers=headers,
                body=payload,
                client_ip=self.client_address[0],
            )
            route, params = router.match(method, path)
            request.params = params
            self._authorize(route, request, app)
            result = route.handler(request)
            self._send(result)
        except HubError as error:
            if error.code in ("unauthorized", "admin_required", "device_revoked", "scope_required"):
                self._audit_auth_failure(app, error, method, path)
            self._send_error(error)
        except Exception:
            logger.error("[hub] unhandled error on %s %s\n%s", method, path, traceback.format_exc())
            self._send_error(HubError("internal"))

    def _authorize(self, route, request: Request, app: HubApp) -> None:
        kind = route.auth
        if kind == AUTH_NONE:
            return
        if kind in (AUTH_ADMIN, AUTH_DEVICE_OR_ADMIN):
            try:
                auth.require_admin(request.header("x-pi-admin-key"), app.admin_key)
                return
            except HubError:
                if kind == AUTH_ADMIN:
                    raise
        if kind == AUTH_SELF_OR_ADMIN:
            try:
                auth.require_admin(request.header("x-pi-admin-key"), app.admin_key)
                return
            except HubError:
                device = auth.authenticate(app.db, request.header("authorization"))
                if request.params.get("device_id") != device.device_id:
                    raise HubError("admin_required", "revoking another device needs the admin key")
                request.device = device
                return

        device = auth.authenticate(app.db, request.header("authorization"))
        auth.require_scope(device, route.scope)
        app.limiter.check(f"dev:{device.device_id}", app.rate_limit)
        request.device = device

    def _send(self, response: Response | StreamResponse) -> None:
        if isinstance(response, StreamResponse):
            self._send_stream(response)
            return
        self.send_response(response.status)
        for name, value in response.header_list():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and response.body:
            self.wfile.write(response.body)
            self.wfile.flush()

    def _send_stream(self, response: StreamResponse) -> None:
        # 不设 Content-Length；连接在收尾后关闭，客户端据此知道流结束
        self.close_connection = True
        self.send_response(response.status)
        if response.content_type:
            self.send_header("Content-Type", response.content_type)
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.flush()
        try:
            for chunk in response.chunks:
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 客户端先断开：收尾由生成器的 finally 负责
            return

    def _send_error(self, error: HubError) -> None:
        body = _error_body(error)
        self.close_connection = True
        try:
            self.send_response(error.status)
            self.send_header("Content-Type", JSON)
            self.send_header("Content-Length", str(len(body)))
            for name, value in error.headers.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _audit_auth_failure(self, app: HubApp, error: HubError, method: str, path: str) -> None:
        try:
            app.audit.log(
                "auth.failed",
                ip=self.client_address[0],
                path=path,
                status=error.status,
                detail={"code": error.code, "method": method},
            )
        except Exception:
            logger.debug("[hub] audit write failed", exc_info=True)


def _error_body(error: HubError) -> bytes:
    import json

    return json.dumps(
        {"error": {"code": error.code, "message": error.message, "type": ERROR_TYPE}},
        ensure_ascii=False,
    ).encode("utf-8")


class HubServer:
    """给 adapter 用的门面：只管起停与地址。"""

    def __init__(
        self,
        *,
        data_dir: str,
        host: str = "127.0.0.1",
        port: int = 8484,
        admin_key: str = "",
        upstream_base: str = "http://127.0.0.1:8642",
        upstream_key: str = "",
        dashboard_base: str = "http://127.0.0.1:9119",
        dashboard_key: str = "",
        rate_limit: int = 60,
        fs_roots: tuple[str, ...] = (),
        public_url: str = "",
        public_dashboard_url: str = "",
        version: str = VERSION,
    ):
        self.app = HubApp(
            data_dir=data_dir,
            admin_key=admin_key,
            public_url=public_url,
            public_dashboard_url=public_dashboard_url,
            upstream_base=upstream_base,
            upstream_key=upstream_key,
            dashboard_base=dashboard_base,
            dashboard_key=dashboard_key,
            rate_limit=rate_limit,
            fs_roots=tuple(fs_roots),
            version=version,
        )
        self.router = Router()
        register_all(self.router, self.app)
        self.host = host
        self._port = int(port)
        self._httpd: _HubHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._maintenance: Maintenance | None = None

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> None:
        """绑定端口并在 daemon 线程里服务。绑定失败抛 `OSError`（交给 adapter 处理）。"""
        if self._httpd is not None:
            return
        httpd = _HubHTTPServer((self.host, self._port), _Handler, self.app, self.router)
        self._port = httpd.server_address[1]
        # poll_interval 默认 0.5s：`shutdown()` 要等它转一圈，测试里会白等半秒
        thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="pi-hub-server",
            daemon=True,
        )
        thread.start()
        self._httpd = httpd
        self._thread = thread
        # 超时收尾 / 租约重投 / 7 天清理都是时间驱动的，没人调就永远不跑
        self._maintenance = Maintenance(self.app)
        self._maintenance.start()

    def stop(self) -> None:
        maintenance, self._maintenance = self._maintenance, None
        if maintenance is not None:
            maintenance.stop()
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self.app.close()

    # -- 只读属性 -----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._httpd is not None

    @property
    def port(self) -> int:
        return self._port

    @property
    def address(self) -> str:
        return f"http://{self.host}:{self._port}"

    @property
    def upstream_base(self) -> str:
        return self.app.upstream.base

    @property
    def db(self):
        return self.app.db
