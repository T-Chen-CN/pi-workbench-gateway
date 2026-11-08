"""路由注册入口。顺序无关：白名单是显式表，没有「兜底转发」。"""

from __future__ import annotations

from ..app import HubApp
from ..router import Router
from . import devices, dispatch, health, pairing, policy, proxy


def register_all(router: Router, app: HubApp) -> None:
    health.register(router, app)
    pairing.register(router, app)
    devices.register(router, app)
    policy.register(router, app)
    dispatch.register(router, app)
    proxy.register(router, app)


__all__ = ["register_all", "devices", "dispatch", "health", "pairing", "policy", "proxy"]
