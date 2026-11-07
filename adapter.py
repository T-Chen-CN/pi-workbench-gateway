"""`BasePlatformAdapter` 实现：只借 gateway 的生命周期。

网关是一个 **web 入口**，不是聊天面：我们不接收入站消息、不建会话。
真正要的是 `connect()` 起服务、`disconnect()` 收服务。

三条纪律：

1. 端口被占用时返回 `False`，**不抛** —— 抛出去会让 gateway 认为整个平台崩了；
2. 服务跑在 daemon 线程（`HubServer` 内部），插件不自己管线程生命周期；
3. 只在 `connect()` 里读环境配置，`register()` 期不做重活。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .config import PLUGIN_NAME, HubConfig

logger = logging.getLogger(__name__)

_log = "[pi-hub]"


class PiWorkbenchAdapter(BasePlatformAdapter):
    def __init__(self, config, *, hub_config: HubConfig | None = None):
        super().__init__(config, Platform(PLUGIN_NAME))
        self._hub_config = hub_config or HubConfig()
        self._hub = None

    # -- 生命周期 -----------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._hub is not None and self._hub.is_running:
            logger.info("%s already listening on %s", _log, self._hub.address)
            return True

        from .hub.server import HubServer

        hub_config = self._hub_config
        if not hub_config.data_dir:
            logger.error("%s no plugin data dir was provided by Hermes", _log)
            return False
        if not hub_config.upstream_key:
            logger.warning(
                "%s API_SERVER_KEY is empty: proxied calls will be rejected upstream", _log
            )

        hub = HubServer(
            data_dir=hub_config.data_dir,
            host=hub_config.host,
            port=hub_config.port,
            admin_key=hub_config.admin_key,
            upstream_base=hub_config.upstream_base,
            upstream_key=hub_config.upstream_key,
            dashboard_base=hub_config.dashboard_base,
            dashboard_key=hub_config.dashboard_key,
            rate_limit=hub_config.rate_limit,
            fs_roots=hub_config.fs_roots,
            public_url=hub_config.public_url,
            public_dashboard_url=hub_config.public_dashboard_url,
        )
        try:
            hub.start()
        except OSError as exc:
            logger.error("%s failed to listen on %s:%s: %s", _log, hub_config.host, hub_config.port, exc)
            return False
        self._hub = hub
        logger.info(
            "%s listening on %s -> %s (pairing %s)",
            _log,
            hub.address,
            hub.upstream_base,
            "enabled" if hub_config.pairing_enabled else "disabled",
        )
        return True

    async def disconnect(self) -> None:
        hub, self._hub = self._hub, None
        if hub is None:
            return
        hub.stop()
        logger.info("%s stopped", _log)

    # -- 聊天面：刻意不支持 -------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> SendResult:
        return SendResult(
            success=False,
            error="pi-workbench-gateway is an HTTP surface; talk to it over /v1/*",
        )

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": PLUGIN_NAME, "type": "channel"}
