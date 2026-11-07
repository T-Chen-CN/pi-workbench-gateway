"""审计：SQLite + `audit-YYYYMMDD.jsonl` 双写。

**永不记录**：连接码、设备 token、Authorization、请求正文、管理密钥、配置密钥。
调用方只传白名单字段；本模块再做一层兜底过滤。
"""

from __future__ import annotations

import json
import os
import threading
import time

from .db import Database

# 一旦这些键出现在 detail 里，直接丢弃（防止调用方手滑把凭据塞进来）
FORBIDDEN_KEYS = frozenset(
    {
        "code",
        "token",
        "token_hash",
        "device_token",
        "authorization",
        "admin_key",
        "api_key",
        "password",
        "secret",
        "body",
    }
)

EVENTS = frozenset(
    {
        "pairing.created",
        "pairing.consumed",
        "pairing.failed",
        "device.registered",
        "device.rotated",
        "device.revoked",
        "auth.failed",
        "config.read",
        "config.write",
        "proxy.completed",
        "proxy.upstream_error",
    }
)


def sanitize(detail: dict[str, object] | None) -> str | None:
    if not detail:
        return None
    kept = {
        str(k): v
        for k, v in detail.items()
        if str(k).lower() not in FORBIDDEN_KEYS and v is not None
    }
    if not kept:
        return None
    return json.dumps(kept, ensure_ascii=False, sort_keys=True)[:2000]


class Audit:
    def __init__(self, db: Database, data_dir: str, *, clock=time.time):
        self._db = db
        self._dir = data_dir
        self._clock = clock
        self._lock = threading.Lock()

    def log(
        self,
        event: str,
        *,
        device_id: str | None = None,
        ip: str | None = None,
        path: str | None = None,
        status: int | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        at = int(self._clock())
        payload = {
            "at": at,
            "event": event if event in EVENTS else "internal",
            "deviceId": device_id,
            "ip": ip,
            "path": path,
            "status": status,
            "detail": sanitize(detail),
        }
        self._db.insert_audit(
            at=at,
            event=payload["event"],
            device_id=device_id,
            ip=ip,
            path=path,
            status=status,
            detail=payload["detail"],
        )
        self._append_jsonl(payload, at)

    def _append_jsonl(self, payload: dict[str, object], at: int) -> None:
        if not self._dir:
            return
        name = time.strftime("audit-%Y%m%d.jsonl", time.gmtime(at))
        target = os.path.join(self._dir, name)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            try:
                with open(target, "a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                # 审计写失败不能影响请求：SQLite 那份已经落了
                return
