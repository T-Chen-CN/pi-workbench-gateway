"""唯一接触 Hermes 插件 API 的文件。

保持极薄是刻意的：上游签名一变，改动只能落在这里。

职责只有三件：

1. 把平台声明给 gateway（`register_platform`），生命周期由 gateway 驱动；
2. 从环境变量组装配置（委托 `config.py`，并在读完后抹掉管理密钥）；
3. 解析持久化目录（`plugin_data_dir`）。

**import 期绝不抛异常**：插件坏了要退化成一条日志，不能把 gateway 主进程带下水。
"""

from __future__ import annotations

import dataclasses
import logging
import os

from .config import ADMIN_KEY_ENV, PLUGIN_NAME, HubConfig, load_config, take_admin_key

logger = logging.getLogger(__name__)

_log = "[pi-hub]"


def _deps_ok() -> bool:
    """被动探测：需要的标准库模块能不能导入。绝不安装任何东西。"""
    try:
        import http.client  # noqa: F401
        import http.server  # noqa: F401
        import sqlite3  # noqa: F401
    except Exception:  # pragma: no cover - 纯标准库，但绝不能打断 gateway
        logger.warning("%s stdlib probe failed", _log, exc_info=True)
        return False
    return True


def _data_dir():
    """持久化根目录：由 Hermes 按 profile 给出，不要自己拼路径。"""
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME)


def _current_config() -> HubConfig:
    config = load_config(data_dir=_data_dir())
    # 读完之后从环境里删掉管理密钥，避免被子进程继承
    admin_key = take_admin_key()
    if admin_key and admin_key != config.admin_key:
        config = dataclasses.replace(config, admin_key=admin_key)
    return config


def _make_adapter(config):
    from .adapter import PiWorkbenchAdapter

    return PiWorkbenchAdapter(config, hub_config=_current_config())


def register(ctx) -> None:
    """Hermes 插件加载时调用一次。"""
    hub_config = _current_config()
    if not hub_config.admin_key:
        logger.warning(
            "%s %s is not set: pairing and device management stay disabled. "
            "Put it in the gateway service environment (not in .env).",
            _log,
            ADMIN_KEY_ENV,
        )
    if not hub_config.upstream_key:
        logger.warning(
            "%s API_SERVER_KEY is not visible to the gateway process: "
            "proxied /v1/* calls will fail with 401",
            _log,
        )

    ctx.register_platform(
        name=PLUGIN_NAME,
        label="Pi Workbench Gateway",
        adapter_factory=_make_adapter,
        check_fn=_deps_ok,
        required_env=[ADMIN_KEY_ENV],
        install_hint=f"set {ADMIN_KEY_ENV} in the gateway service environment",
    )
    logger.info(
        "%s platform registered (listen=%s:%s pairing=%s)",
        _log,
        hub_config.host,
        hub_config.port,
        "enabled" if hub_config.pairing_enabled else "disabled",
    )


__all__ = ["register", "os"]
