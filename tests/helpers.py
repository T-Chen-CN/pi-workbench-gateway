"""测试脚手架：假上游 + 起一个真的 HubServer（端口 0）。"""

from __future__ import annotations

import http.client
import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from hub.server import HubServer

ADMIN_KEY = "test-admin-key"
UPSTREAM_KEY = "test-api-server-key"
DASHBOARD_KEY = "test-dashboard-token"


class FakeUpstream:
    """假的 api_server + dashboard，记录收到的请求供断言。"""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.config: dict[str, Any] = {
            "approvals": {"mode": "manual", "timeout": 300, "deny": ["rm -rf /"]},
            "agent": {"disabled_toolsets": ["code_execution"]},
            "terminal": {"backend": "local"},
            "model": {"default": "never-exposed"},
        }
        self.put_status = 200
        self.read_back_mode: str | None = None  # 用于模拟「写入没生效」
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="fake-upstream",
            daemon=True,
        )

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def base(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    # -- 请求处理 -----------------------------------------------------------

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                return

            def _record(self, body: bytes = b"") -> None:
                outer.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )

            def _json(self, status: int, payload: dict) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                self._record()
                path = self.path.split("?")[0]
                if path == "/health/detailed":
                    self._json(200, {"status": "ok", "version": "0.0.0-test"})
                elif path == "/v1/capabilities":
                    self._json(200, {"capabilities": ["runs"]})
                elif path == "/api/config":
                    self._json(200, outer.config)
                elif path == "/api/fs/read-data-url":
                    self._json(200, {"data_url": "data:image/png;base64,AAAA"})
                elif path == "/v1/runs/run_1/events":
                    self._stream()
                else:
                    self._json(404, {"error": {"code": "not_found"}})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                self._record(body)
                path = self.path.split("?")[0]
                if path == "/v1/runs":
                    self._json(202, {"run_id": "run_1", "status": "accepted"})
                elif path == "/api/chat/image-upload":
                    self._json(200, {"path": "/uploads/a.png"})
                else:
                    self._json(200, {"ok": True})

            def do_PUT(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                self._record(body)
                if outer.put_status != 200:
                    self._json(outer.put_status, {"error": {"code": "nope"}})
                    return
                try:
                    patch = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    patch = {}
                mode = ((patch.get("config") or {}).get("approvals") or {}).get("mode")
                if mode:
                    outer.config["approvals"]["mode"] = outer.read_back_mode or mode
                self._json(200, {"ok": True})

            def do_PATCH(self):  # noqa: N802
                self.do_POST()

            def do_DELETE(self):  # noqa: N802
                self.do_POST()

            def _stream(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                frames = (
                    b'id: 1\ndata: {"delta": "first"}\n\n',
                    b'id: 2\ndata: {"delta": "second"}\n\n',
                    b'id: 3\ndata: {"status": "completed"}\n\n',
                )
                for frame in frames:
                    try:
                        self.wfile.write(frame)
                        self.wfile.flush()
                    except OSError:
                        return
                    threading.Event().wait(0.25)
                self.close_connection = True

        return Handler


class HubTestCase(unittest.TestCase):
    """起一个真的网关（端口 0），上游是 `FakeUpstream`。"""

    rate_limit = 1000

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="pi-hub-test-")
        self.upstream = FakeUpstream()
        self.upstream.start()
        self.hub = HubServer(
            data_dir=self.tmp,
            host="127.0.0.1",
            port=0,
            admin_key=ADMIN_KEY,
            upstream_base=self.upstream.base,
            upstream_key=UPSTREAM_KEY,
            dashboard_base=self.upstream.base,
            dashboard_key=DASHBOARD_KEY,
            rate_limit=self.rate_limit,
            fs_roots=(self.tmp,),
            public_url="https://hub.test",
            public_dashboard_url="https://dash.test",
        )
        self.hub.start()

    def tearDown(self) -> None:
        self.hub.stop()
        self.upstream.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 客户端 -------------------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        token: str | None = None,
        admin: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any, dict[str, str]]:
        payload = b""
        send_headers = dict(headers or {})
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            send_headers.setdefault("Content-Type", "application/json")
        if token:
            send_headers["Authorization"] = f"Bearer {token}"
        if admin:
            send_headers["X-Pi-Admin-Key"] = admin

        connection = http.client.HTTPConnection("127.0.0.1", self.hub.port, timeout=10)
        try:
            connection.request(method, path, body=payload or None, headers=send_headers)
            response = connection.getresponse()
            raw = response.read()
            parsed = None
            if raw:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed = raw
            return response.status, parsed, {k.lower(): v for k, v in response.getheaders()}
        finally:
            connection.close()

    # -- 常用动作 -----------------------------------------------------------

    def make_pairing_code(self) -> str:
        status, payload, _ = self.call("POST", "/v1/devices/pairing", admin=ADMIN_KEY)
        self.assertEqual(status, 201, payload)
        return str(payload["code"])

    def pair(self, device_id: str = "device-1", name: str = "devbox") -> str:
        code = self.make_pairing_code()
        status, payload, _ = self.call(
            "POST",
            "/v1/devices/pair",
            body={"code": code, "deviceId": device_id, "name": name},
        )
        self.assertEqual(status, 201, payload)
        return str(payload["token"])

    def audit_lines(self) -> list[dict]:
        import glob
        import os

        lines: list[dict] = []
        for path in glob.glob(os.path.join(self.tmp, "audit-*.jsonl")):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        lines.append(json.loads(line))
        return lines
