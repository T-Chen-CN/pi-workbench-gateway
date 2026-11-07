"""配置面与媒体：`/api/config` 白名单、图片上传、图片读回。

* 设备 token 永远拿不到整份配置，也拿不到 dashboard 凭据；
* `PUT` 只允许 `approvals.mode` 一个键，写后读回复核；
* 媒体路径先过 `fsguard`，再转给控制台。

`mode` 只认 Hermes 原生三档：`manual` / `smart` / `off`（不做预设、不切工具集）。
"""

from __future__ import annotations

import json
from urllib.parse import quote

from .. import fsguard
from ..app import HubApp
from ..errors import HubError
from ..router import AUTH_DEVICE, JSON, Request, Response, Router, json_response

APPROVAL_MODES = ("manual", "smart", "off")
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_TERMINAL_BACKEND = "local"

_READ_HEADERS = {"accept": "application/json"}


def pick_policy(raw: object) -> dict[str, object]:
    """从整份配置里只抽白名单子集；认不出来就留空，不猜。"""
    data = raw if isinstance(raw, dict) else {}
    approvals = data.get("approvals") if isinstance(data.get("approvals"), dict) else {}
    agent = data.get("agent") if isinstance(data.get("agent"), dict) else {}
    terminal = data.get("terminal") if isinstance(data.get("terminal"), dict) else {}

    mode = approvals.get("mode")
    timeout = approvals.get("timeout")
    disabled = agent.get("disabled_toolsets")
    backend = terminal.get("backend")
    deny = approvals.get("deny")

    policy: dict[str, object] = {
        "approvals": {
            "mode": mode if mode in APPROVAL_MODES else None,
            "timeout": int(timeout)
            if isinstance(timeout, (int, float)) and timeout > 0
            else DEFAULT_TIMEOUT_SECONDS,
        },
        "agent": {
            "disabled_toolsets": [str(x) for x in disabled]
            if isinstance(disabled, list)
            else []
        },
        "terminal": {
            "backend": backend if isinstance(backend, str) and backend else DEFAULT_TERMINAL_BACKEND
        },
    }
    if isinstance(deny, list):
        policy["policy"] = {"denyCount": len(deny)}
    return policy


def register(router: Router, app: HubApp) -> None:
    def read_config(request: Request) -> Response:
        response = app.dashboard.open(
            "GET", "/api/config", headers=app.dashboard.request_headers(_READ_HEADERS)
        )
        try:
            payload = response.read()
        finally:
            response.close()
        if response.status >= 400:
            raise HubError(
                "upstream_unavailable", f"dashboard refused /api/config ({response.status})"
            )
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HubError("upstream_unavailable", "dashboard returned non-JSON config")
        app.audit.log(
            "config.read",
            device_id=request.device.device_id,
            ip=request.client_ip,
            path=request.path,
            status=200,
        )
        return json_response(pick_policy(data))

    def write_config(request: Request) -> Response:
        data = request.json()
        config = data.get("config") if isinstance(data.get("config"), dict) else {}
        approvals = config.get("approvals") if isinstance(config.get("approvals"), dict) else {}
        mode = approvals.get("mode")
        if mode not in APPROVAL_MODES:
            raise HubError("invalid_params", f"approvals.mode 只接受 {list(APPROVAL_MODES)}")
        if set(config.keys()) - {"approvals"} or set(approvals.keys()) - {"mode"}:
            raise HubError("invalid_params", "只允许写 approvals.mode 一个键")

        patch = json.dumps({"config": {"approvals": {"mode": mode}}}).encode("utf-8")
        response = app.dashboard.open(
            "PUT",
            "/api/config",
            headers=app.dashboard.request_headers(
                {"accept": "application/json", "content-type": "application/json"}
            ),
            body=patch,
        )
        try:
            response.read()
        finally:
            response.close()
        if response.status >= 400:
            raise HubError(
                "upstream_unavailable", f"dashboard refused the write ({response.status})"
            )

        after = read_config(request)
        try:
            written = json.loads(after.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HubError("internal", "read-back after write failed")
        actual = (written.get("approvals") or {}).get("mode")
        if actual != mode:
            raise HubError(
                "internal", f"approvals.mode 没有生效：写入 {mode}，读回 {actual}"
            )
        app.audit.log(
            "config.write",
            device_id=request.device.device_id,
            ip=request.client_ip,
            path=request.path,
            status=200,
            detail={"mode": mode},
        )
        return json_response(written)

    def upload_image(request: Request) -> Response:
        response = app.dashboard.open(
            "POST",
            "/api/chat/image-upload",
            headers=app.dashboard.request_headers(
                {
                    "accept": "application/json",
                    "content-type": request.header("content-type") or JSON,
                }
            ),
            body=request.body,
        )
        try:
            payload = response.read()
        finally:
            response.close()
        return Response(
            status=response.status,
            body=payload,
            content_type=response.getheader("content-type") or JSON,
        )

    def read_data_url(request: Request) -> Response:
        target = request.query.get("path", "")
        safe = fsguard.ensure_allowed(target, app.fs_roots)
        response = app.dashboard.open(
            "GET",
            f"/api/fs/read-data-url?path={quote(safe, safe='')}",
            headers=app.dashboard.request_headers(_READ_HEADERS),
        )
        try:
            payload = response.read()
        finally:
            response.close()
        return Response(
            status=response.status,
            body=payload,
            content_type=response.getheader("content-type") or JSON,
        )

    router.add("GET", "/api/config", read_config, auth=AUTH_DEVICE)
    router.add("PUT", "/api/config", write_config, auth=AUTH_DEVICE)
    router.add("POST", "/api/chat/image-upload", upload_image, auth=AUTH_DEVICE)
    router.add("GET", "/api/fs/read-data-url", read_data_url, auth=AUTH_DEVICE)
