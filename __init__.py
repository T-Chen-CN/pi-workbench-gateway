"""Pi Workbench Gateway —— Hermes `kind: platform` 插件。

`register()` 是 Hermes 唯一会调用的入口，其余代码都在 `hub/` 里，
而 `hub/` 刻意不知道 Hermes 存在（纯标准库 HTTP 服务）。
"""

from __future__ import annotations

from .shim import register

__all__ = ["register"]
