"""应用状态：把 db / audit / 限流 / 上游客户端收在一处。

`server.py` 负责 HTTP 与人；`routes/*` 负责业务。两边只通过本对象交互，
这样路由可以在测试里单独构造。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .audit import Audit
from .db import Database
from .dispatch import DispatchStore
from .fsguard import resolve_roots  # noqa: F401  (供测试与外部调用复用)
from .ratelimit import RateLimiter
from .relay import Relay
from .upstream import Upstream

VERSION = "0.3.2"
CONTRACT_VERSION = 1
FEATURES = ("pairing", "heartbeat", "policy", "media", "dispatch", "a2a")
ONLINE_WINDOW_SECONDS = 90
UPSTREAM_PROBE_TTL_SECONDS = 5.0


@dataclass
class HubApp:
    data_dir: str
    admin_key: str = ""
    public_url: str = ""
    public_dashboard_url: str = ""
    upstream_base: str = "http://127.0.0.1:8642"
    upstream_key: str = ""
    dashboard_base: str = "http://127.0.0.1:9119"
    dashboard_key: str = ""
    rate_limit: int = 60
    fs_roots: tuple[str, ...] = ()
    version: str = VERSION
    db_path: str | None = None
    clock: Callable[[], float] = time.time

    # 运行期对象（__post_init__ 里建，允许测试注入数据库）
    db: Database = field(init=False)
    audit: Audit = field(init=False)
    dispatch: DispatchStore = field(init=False)
    relay: Relay = field(init=False)
    limiter: RateLimiter = field(init=False)
    upstream: Upstream = field(init=False)
    dashboard: Upstream = field(init=False)

    def __post_init__(self) -> None:
        path = self.db_path or os.path.join(self.data_dir, "hub.db")
        self.db = Database(path)
        self.audit = Audit(self.db, self.data_dir, clock=self.clock)
        self.dispatch = DispatchStore(self.db, clock=self.clock)
        self.relay = Relay()
        self.limiter = RateLimiter()
        self.upstream = Upstream(self.upstream_base, self.upstream_key)
        self.dashboard = Upstream(self.dashboard_base, self.dashboard_key)
        self._started_at = float(self.clock())
        self._upstream_state: tuple[float, bool, str] = (0.0, False, "")
        self._sse_slots: dict[str, int] = {}
        self._sse_lock = threading.Lock()

    # -- SSE 并发槽位（默认 2 / 设备） --------------------------------------

    def sse_acquire(self, device_id: str, limit: int = 2) -> None:
        from .errors import HubError

        with self._sse_lock:
            used = self._sse_slots.get(device_id, 0)
            if used >= limit:
                raise HubError("rate_limited", "too many concurrent event streams")
            self._sse_slots[device_id] = used + 1

    def sse_release(self, device_id: str) -> None:
        with self._sse_lock:
            used = self._sse_slots.get(device_id, 0)
            if used <= 1:
                self._sse_slots.pop(device_id, None)
            else:
                self._sse_slots[device_id] = used - 1

    # -- 时间 ---------------------------------------------------------------

    def now(self) -> int:
        return int(self.clock())

    def uptime(self) -> int:
        return max(0, int(float(self.clock()) - self._started_at))

    # -- 健康 ---------------------------------------------------------------

    def upstream_ok(self, *, force: bool = False) -> tuple[bool, str]:
        """探测 api_server 是否可用。结果缓存 5 秒，避免健康检查打穿上游。"""
        at = float(self.clock())
        cached_at, ok, version = self._upstream_state
        if not force and at - cached_at < UPSTREAM_PROBE_TTL_SECONDS:
            return ok, version
        try:
            response = self.upstream.open(
                "GET",
                "/health/detailed",
                headers=self.upstream.request_headers({"accept": "application/json"}),
            )
            body = response.read(64 * 1024)
            ok = 200 <= response.status < 300
            version = _extract_version(body) if ok else ""
        except Exception:
            ok, version = False, ""
        self._upstream_state = (at, ok, version)
        return ok, version

    def health_payload(self) -> dict[str, object]:
        """匿名端点：**只**回答「网关活着 / 上游活着」。

        设备计数、上游版本、内部端口、运行时长一律不放这里——那些是 `/v1/hub/status`
        的事（需要管理密钥）。旧版把整套诊断挂在匿名端点上，等于给扫描器送指纹。
        """
        ok, _ = self.upstream_ok()
        return {"status": "ok", "upstreamOk": ok}

    def status_payload(self) -> dict[str, object]:
        ok, version = self.upstream_ok(force=True)
        counts = self.db.device_counts()
        return {
            "status": "ok",
            "hub": self.version,
            "contractVersion": 1,
            "features": list(FEATURES),
            "upstream": self.upstream.base,
            "upstreamOk": ok,
            "upstreamVersion": version,
            "dashboard": self.public_dashboard_url,
            "devices": counts,
            "adminKeyConfigured": bool(self.admin_key),
            "pairingEnabled": bool(self.admin_key),
            "fsRoots": list(self.fs_roots),
            "uptimeSeconds": self.uptime(),
            "serverTime": self.now(),
        }

    def close(self) -> None:
        self.db.close()


def _extract_version(body: bytes) -> str:
    import json

    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return ""
    if isinstance(data, dict):
        for key in ("version", "hermes_version", "code_version"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return ""
