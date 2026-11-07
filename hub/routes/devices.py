"""设备侧：身份、心跳、注销、break-glass 登记。

字段名对齐桌面端 `electron/cloud/gateway.ts` 的 `GatewayDevice` / `HeartbeatResult`，
少一个字段 UI 就显示不出来，所以这里是**契约**，不是随手写的。
"""

from __future__ import annotations

import uuid

from .. import auth
from ..app import ONLINE_WINDOW_SECONDS, HubApp
from ..errors import HubError
from ..router import AUTH_ADMIN, AUTH_DEVICE, AUTH_SELF_OR_ADMIN, Request, Response, Router, json_response
from .pairing import sanitize_name


def _online(last_seen_at: int | None, now: int) -> bool:
    return last_seen_at is not None and now - last_seen_at <= ONLINE_WINDOW_SECONDS


def register(router: Router, app: HubApp) -> None:
    def me(request: Request) -> Response:
        device = request.device
        now = app.now()
        return json_response(
            {
                "deviceId": device.device_id,
                "name": device.name,
                "scopes": list(device.scopes),
                "hub": app.version,
                "createdAt": device.created_at,
                "lastSeenAt": device.last_seen_at,
                "revokedAt": device.revoked_at,
                "online": _online(device.last_seen_at, now),
                "onlineWindow": ONLINE_WINDOW_SECONDS,
                "serverTime": now,
                "tokenPrefix": device.token_prefix,
            }
        )

    def heartbeat(request: Request) -> Response:
        now = app.now()
        app.db.touch_device(request.device.device_id, now)
        return json_response(
            {
                "ok": True,
                "deviceId": request.device.device_id,
                "serverTime": now,
                "lastSeenAt": now,
                "onlineWindow": ONLINE_WINDOW_SECONDS,
            }
        )

    def revoke(request: Request) -> Response:
        target = request.params.get("device_id", "")
        if not target:
            raise HubError("invalid_params", "device id is required")
        changed = app.db.revoke_device(target, app.now())
        app.audit.log(
            "device.revoked",
            device_id=target,
            ip=request.client_ip,
            path=request.path,
            status=200,
            detail={"changed": changed},
        )
        return json_response({"revoked": True, "deviceId": target, "changed": changed})

    def register_device(request: Request) -> Response:
        """break-glass：服务器侧直接签发一个设备，桌面端不调用。"""
        data = request.json()
        device_id = str(data.get("deviceId") or "").strip() or str(uuid.uuid4())
        name = sanitize_name(data.get("name"))
        token = auth.new_token()
        existing = app.db.device(device_id)
        app.db.upsert_device(
            device_id=device_id,
            name=name,
            token_hash=auth.hash_token(token),
            token_prefix=auth.token_prefix(token),
            scopes=",".join(auth.DEFAULT_SCOPES),
            now=app.now(),
        )
        app.audit.log(
            "device.registered",
            device_id=device_id,
            ip=request.client_ip,
            path=request.path,
            status=201,
            detail={"rotated": existing is not None},
        )
        return json_response(
            {
                "token": token,
                "tokenPrefix": auth.token_prefix(token),
                "deviceId": device_id,
                "name": name,
                "scopes": list(auth.DEFAULT_SCOPES),
            },
            status=201,
        )

    router.add("GET", "/v1/devices/me", me, auth=AUTH_DEVICE)
    router.add("POST", "/v1/devices/me/heartbeat", heartbeat, auth=AUTH_DEVICE)
    router.add("POST", "/v1/devices/{device_id}/revoke", revoke, auth=AUTH_SELF_OR_ADMIN)
    router.add("POST", "/v1/devices/register", register_device, auth=AUTH_ADMIN)
