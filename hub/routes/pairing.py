"""配对：生成连接码（Admin）与消费连接码（无凭据）。

规格见 CLOUD_AGENT §4.1 / §4.2。两条硬规则：

* `/pair` **不需要任何请求头凭据** —— 连接码本身就是这一次的授权；
* 返回的 `scopes` 固定 `chat` + `dispatch`，**永远不含 `admin`**，忽略客户端传什么。
"""

from __future__ import annotations

from .. import auth, pairing
from ..app import ONLINE_WINDOW_SECONDS, HubApp
from ..errors import HubError
from ..router import AUTH_ADMIN, AUTH_NONE, Request, Response, Router, json_response

PAIRING_IP_LIMIT = 5
PAIR_IP_LIMIT = 10
MAX_DEVICE_ID = 128
MAX_NAME = 64


def sanitize_name(raw: object) -> str:
    text = "".join(ch for ch in str(raw or "") if ch.isprintable()).strip()
    return text[:MAX_NAME] or "pi-workbench"


def register(router: Router, app: HubApp) -> None:
    def create_pairing(request: Request) -> Response:
        # 失败也计数：先限流再验管理密钥，避免用穷举管理密钥来绕过限流
        app.limiter.check(f"pairing:{request.client_ip}", PAIRING_IP_LIMIT)
        auth.require_admin(request.header("x-pi-admin-key"), app.admin_key)
        app.db.purge_pairing_codes(app.now())
        issued = pairing.issue(app.db, now=app.now())
        url = pairing.pairing_url(
            str(issued["code"]), app.public_url, base_url=request_base_url(request)
        )
        app.audit.log(
            "pairing.created",
            ip=request.client_ip,
            path=request.path,
            status=201,
            detail={"expiresAt": issued["expiresAt"]},
        )
        return json_response(
            {"code": issued["code"], "expiresAt": issued["expiresAt"], "pairingUrl": url},
            status=201,
        )

    def pair(request: Request) -> Response:
        app.limiter.check(f"pair:{request.client_ip}", PAIR_IP_LIMIT)
        data = request.json()
        code = str(data.get("code") or "")
        device_id = str(data.get("deviceId") or "").strip()
        if not device_id:
            raise HubError("invalid_params", "deviceId is required")
        if len(device_id) > MAX_DEVICE_ID:
            raise HubError("invalid_params", "deviceId is too long")
        name = sanitize_name(data.get("name"))

        existing = app.db.device(device_id)
        outcome = pairing.consume(app.db, code, device_id=device_id, now=app.now())
        if outcome != "ok":
            app.audit.log(
                "pairing.failed",
                ip=request.client_ip,
                path=request.path,
                status=400,
                detail={"reason": outcome},
            )
            raise HubError(outcome)

        token = auth.new_token()
        app.db.upsert_device(
            device_id=device_id,
            name=name,
            token_hash=auth.hash_token(token),
            token_prefix=auth.token_prefix(token),
            scopes=",".join(auth.DEFAULT_SCOPES),
            now=app.now(),
        )
        app.audit.log(
            "pairing.consumed",
            device_id=device_id,
            ip=request.client_ip,
            path=request.path,
            status=201,
        )
        if existing is not None:
            app.audit.log(
                "device.rotated",
                device_id=device_id,
                ip=request.client_ip,
                path=request.path,
                status=201,
            )
        return json_response(
            {
                "token": token,
                "tokenPrefix": auth.token_prefix(token),
                "deviceId": device_id,
                "name": name,
                "scopes": list(auth.DEFAULT_SCOPES),
                "dashboardUrl": app.public_dashboard_url,
            },
            status=201,
        )

    router.add("POST", "/v1/devices/pairing", create_pairing, auth=AUTH_ADMIN)
    router.add("POST", "/v1/devices/pair", pair, auth=AUTH_NONE)


def request_base_url(request: Request) -> str:
    """`pairingUrl` 的退化路径：`X-Forwarded-*` → `Host`。"""
    proto = request.header("x-forwarded-proto") or "http"
    host = request.header("x-forwarded-host") or request.header("host")
    if not host:
        return ""
    return f"{proto.split(',')[0].strip()}://{host.split(',')[0].strip()}"


__all__ = ["register", "sanitize_name", "request_base_url", "ONLINE_WINDOW_SECONDS"]
