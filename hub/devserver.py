"""本机开发入口：假上游 + 真网关，不需要 Hermes、不需要 Linux。

```bash
export PI_HUB_ADMIN_KEY=dev-admin-key        # Windows: $env:PI_HUB_ADMIN_KEY="dev-admin-key"
python -m hub.devserver --port 8484
```

它会打印一个连接码，然后可以：

```bash
curl -s localhost:8484/v1/hub/health
curl -s -X POST localhost:8484/v1/hub/status -H "X-Pi-Admin-Key: dev-admin-key"
curl -s -X POST localhost:8484/v1/devices/pair \
  -H 'Content-Type: application/json' \
  -d '{"code":"<上面的码>","deviceId":"dev-1","name":"devbox"}'
curl -sN localhost:8484/v1/runs/run_dev/events -H "Authorization: Bearer <token>"
```
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

from . import pairing
from .server import HubServer

DEV_ADMIN_KEY = "dev-admin-key"


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/health/detailed":
            self._json(200, {"status": "ok", "version": "0.0.0-dev"})
        elif path == "/v1/capabilities":
            self._json(200, {"capabilities": ["runs", "sessions", "jobs"]})
        elif path == "/api/config":
            self._json(
                200,
                {
                    "approvals": {"mode": "manual", "timeout": 300, "deny": []},
                    "agent": {"disabled_toolsets": ["code_execution"]},
                    "terminal": {"backend": "local"},
                },
            )
        elif "/events" in path:
            self._stream()
        else:
            self._json(404, {"error": {"code": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if path == "/v1/runs":
            self._json(202, {"run_id": "run_dev", "status": "accepted", "echo": body.decode("utf-8", "replace")})
        else:
            self._json(200, {"ok": True})

    def do_PUT(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._json(200, {"ok": True})

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        frames = (
            b'id: 1\nevent: message.delta\ndata: {"delta": "hello "}\n\n',
            b'id: 2\nevent: message.delta\ndata: {"delta": "world"}\n\n',
            b'id: 3\nevent: run.completed\ndata: {"status": "completed"}\n\n',
        )
        for frame in frames:
            try:
                self.wfile.write(frame)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            threading.Event().wait(0.05)
        self.close_connection = True


def _serve_upstream(port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _UpstreamHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="fake-upstream", daemon=True).start()
    return httpd, httpd.server_address[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pi Workbench Gateway dev server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PI_HUB_PORT", "8484")))
    parser.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), ".devdata"))
    args = parser.parse_args(argv)

    admin_key = os.environ.get("PI_HUB_ADMIN_KEY") or DEV_ADMIN_KEY
    upstream, upstream_port = _serve_upstream()
    hub = HubServer(
        data_dir=args.data_dir,
        host="127.0.0.1",
        port=args.port,
        admin_key=admin_key,
        upstream_base=f"http://127.0.0.1:{upstream_port}",
        upstream_key="dev-upstream-key",
        dashboard_base=f"http://127.0.0.1:{upstream_port}",
        dashboard_key="dev-dashboard-token",
        fs_roots=(args.data_dir,),
        public_url=f"http://127.0.0.1:{args.port}",
        public_dashboard_url="http://dash.localhost",
    )
    hub.start()
    issued = pairing.issue(hub.db)
    print(f"hub         {hub.address}")
    print(f"upstream    {hub.upstream_base} (fake)")
    print(f"admin key   {admin_key}")
    print(f"pair code   {issued['code']}  (expires at {issued['expiresAt']})")
    print("Ctrl+C 退出")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
        upstream.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
