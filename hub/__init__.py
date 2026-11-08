"""网关主体：纯标准库 HTTP 服务，刻意不知道 Hermes 存在。

本包不读环境变量（配置由 `config.py` 组装后传进来），也不 import 任何
`gateway.*` / `plugins.*`（那是 `shim.py` / `adapter.py` 的事）。
"""

from __future__ import annotations

__all__ = ["VERSION", "HubApp", "HubServer"]

VERSION = "0.1.2"


def __getattr__(name: str):  # 惰性导出：`import hub` 不应该连带起服务器模块
    if name == "HubServer":
        from .server import HubServer

        return HubServer
    if name == "HubApp":
        from .app import HubApp

        return HubApp
    raise AttributeError(name)
