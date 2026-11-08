"""唯一接触 Hermes 插件 API 的文件。

保持极薄是刻意的：上游签名一变，改动只能落在这里。

职责只有三件：

1. 把平台声明给 gateway（`register_platform`），生命周期由 gateway 驱动；
2. 把派活工具注册进工具表（toolset `pi_workbench`），并做一次「工具集是否登记」的自检；
3. 从环境变量组装配置（委托 `config.py`，并在读完后抹掉管理密钥）；
4. 解析持久化目录（`plugin_data_dir`）。

**import 期绝不抛异常**：插件坏了要退化成一条日志，不能把 gateway 主进程带下水。
"""

from __future__ import annotations

import dataclasses
import logging

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


# 工具层与 hub 的粘合点：adapter 起完 hub 后把 app 放进来。
# 用可变持有者而不是 import 全局，是为了让 `register()`（早于 connect）只注册、不取依赖。
_STATE: dict = {"app": None}


def attach_app(app) -> None:
    """由 adapter 在 `connect()` 成功后调用。"""
    _STATE["app"] = app


def _toolset_registered(toolset: str) -> bool | None:
    """自检：插件工具集是否登记进了 api_server 的工具白名单。

    不登记时工具对模型**静默不可见**——插件装了、工具注册了、模型就是看不到。
    这种失败只能靠一条显式日志暴露，不能指望人记得。

    返回 None 表示查不了（比如上游没有这个配置文件），此时不告警。
    """
    try:
        from hermes_cli.config import load_config  # 只在自检时 import

        config = load_config()
        platform_toolsets = config.get("platform_toolsets") or {}
        names = platform_toolsets.get("api_server")
        if not isinstance(names, list):
            return False
        return toolset in names
    except Exception:  # pragma: no cover - 上游内部结构变了也不能把插件弄坏
        logger.debug("%s toolset self-check skipped", _log, exc_info=True)
        return None


def _register_tools(ctx) -> None:
    from .tools import TOOLSET, DispatchTools, tool_schemas

    tools = DispatchTools(_STATE)
    handlers = {
        "local_workspaces": tools.workspaces,
        "local_dispatch": tools.dispatch,
        "local_task_get": tools.task_get,
        "local_task_reply": tools.task_reply,
        "local_task_cancel": tools.task_cancel,
    }
    for schema in tool_schemas():
        name = schema["name"]
        try:
            ctx.register_tool(
                name=name,
                toolset=TOOLSET,
                schema=schema,
                handler=handlers[name],
                description=schema["description"],
            )
        except Exception:
            logger.warning("%s failed to register tool %s", _log, name, exc_info=True)

    registered = _toolset_registered(TOOLSET)
    if registered is False:
        logger.warning(
            "%s toolset %r is not enabled for the api_server platform: the dispatch tools "
            "are invisible to the model. Add it under platform_toolsets.api_server in "
            "config.yaml (or run: hermes tools enable %s --platform api_server) and restart "
            "the gateway.",
            _log,
            TOOLSET,
            TOOLSET,
        )


def _make_adapter(config):
    from .adapter import PiWorkbenchAdapter

    return PiWorkbenchAdapter(config, hub_config=_current_config())


def register(ctx) -> None:
    """Hermes 插件加载时调用一次。"""
    hub_config = _current_config()
    if not hub_config.admin_key:
        logger.warning(
            "%s %s is not set: pairing and device management stay disabled. "
            "Set it in the profile's .env (Hermes loads .env into the gateway env at startup).",
            _log,
            ADMIN_KEY_ENV,
        )
    if not hub_config.upstream_key:
        logger.warning(
            "%s API_SERVER_KEY is not visible to the gateway process: "
            "proxied /v1/* calls will fail with 401",
            _log,
        )

    _register_tools(ctx)

    ctx.register_platform(
        name=PLUGIN_NAME,
        label="Pi Workbench Gateway",
        adapter_factory=_make_adapter,
        check_fn=_deps_ok,
        required_env=[ADMIN_KEY_ENV],
        install_hint=f"set {ADMIN_KEY_ENV} in the profile's .env",
    )
    logger.info(
        "%s platform registered (listen=%s:%s pairing=%s)",
        _log,
        hub_config.host,
        hub_config.port,
        "enabled" if hub_config.pairing_enabled else "disabled",
    )


__all__ = ["register"]
