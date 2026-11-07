"""设备鉴权：token → deviceId。

处理顺序（CLOUD_AGENT §5.6）：

1. 取 `Authorization: Bearer <token>`，没有就 401；
2. `sha256(token)` 命中 `devices`，否则 401；
3. `revoked_at` 非空 → 401 `device_revoked`；
4. 检查 scope，不足 → 403 `scope_required`；
5. 刷新 `last_seen_at`；
6. 由调用方注入 `X-Pi-Device: <deviceId>`。

管理密钥只用于 `pairing` / `register` / 撤销别人的设备，比较用常量时间。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass

from .db import Database
from .errors import HubError

TOKEN_PREFIX = "pi_"
TOKEN_BYTES = 32

SCOPE_CHAT = "chat"
SCOPE_DISPATCH = "dispatch"
DEFAULT_SCOPES = (SCOPE_CHAT, SCOPE_DISPATCH)
KNOWN_SCOPES = frozenset(DEFAULT_SCOPES)

# `last_seen_at` 每次请求都写会让 SQLite 变成热点；5 秒一次够了（onlineWindow 是 90 秒）
_TOUCH_INTERVAL_SECONDS = 5


@dataclass(frozen=True)
class Device:
    device_id: str
    name: str
    scopes: tuple[str, ...]
    token_prefix: str
    created_at: int
    last_seen_at: int | None
    revoked_at: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Device":
        return cls(
            device_id=str(row["device_id"]),
            name=str(row["name"]),
            scopes=tuple(s for s in str(row["scopes"] or "").split(",") if s),
            token_prefix=str(row["token_prefix"]),
            created_at=int(row["created_at"]),
            last_seen_at=int(row["last_seen_at"]) if row["last_seen_at"] is not None else None,
            revoked_at=int(row["revoked_at"]) if row["revoked_at"] is not None else None,
        )

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def has_scope(self, scope: str | None) -> bool:
        return scope is None or scope in self.scopes


def new_token() -> str:
    raw = base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).decode("ascii")
    return TOKEN_PREFIX + raw.rstrip("=")


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    return str(token or "")[:7]


def parse_bearer(header_value: str) -> str:
    value = (header_value or "").strip()
    if not value:
        return ""
    scheme, _, rest = value.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    token = rest.strip()
    return token if token.startswith(TOKEN_PREFIX) else ""


def admin_ok(provided: str, expected: str) -> bool:
    """常量时间比较；任一为空都算失败（没配管理密钥 = 管理面关闭）。"""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


def authenticate(db: Database, authorization: str, *, now: int | None = None) -> Device:
    token = parse_bearer(authorization)
    if not token:
        raise HubError("unauthorized", "missing or invalid device token")
    row = db.device_by_token_hash(hash_token(token))
    if row is None:
        raise HubError("unauthorized", "missing or invalid device token")
    device = Device.from_row(row)
    if device.is_revoked:
        raise HubError("device_revoked", "device token has been revoked")
    at = int(now if now is not None else time.time())
    if device.last_seen_at is None or at - device.last_seen_at >= _TOUCH_INTERVAL_SECONDS:
        db.touch_device(device.device_id, at)
    return device


def require_scope(device: Device, scope: str | None) -> None:
    if not device.has_scope(scope):
        raise HubError("scope_required", f"scope '{scope}' is required")


def require_admin(provided: str, expected: str) -> None:
    if not expected:
        raise HubError("pairing_disabled", "admin key is not configured on this hub")
    if not admin_ok(provided, expected):
        raise HubError("admin_required", "missing or invalid admin key")


def online_window_seconds() -> int:
    return 90
