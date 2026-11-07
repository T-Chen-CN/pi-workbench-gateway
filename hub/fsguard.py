"""媒体路径白名单。

`PI_HUB_FS_ROOTS` 用 `os.pathsep` 分隔（Linux `:` / Windows `;`）。
**刻意不用 `:`**：Windows 路径自带盘符冒号，`C:\\a:C:\\b` 会被切碎。

规则：`realpath` 解析后再判包含，拒绝符号链接逃逸与 `..` 穿越。
"""

from __future__ import annotations

import os

from .errors import HubError

DEFAULT_SUBDIRS = ("uploads", "cache")


def resolve_roots(
    raw: str | None, profile_home: str, *, separator: str | None = None
) -> tuple[str, ...]:
    """把环境变量解析成绝对路径元组。

    * 有 `PI_HUB_FS_ROOTS` → 按 `os.pathsep` 切；
    * 没有 → 用 profile 下的 `uploads/`、`cache/`（两个默认媒体落点）。
    """
    sep = os.pathsep if separator is None else separator
    if raw and str(raw).strip():
        parts = [p.strip() for p in str(raw).split(sep) if p.strip()]
        return tuple(os.path.realpath(os.path.abspath(p)) for p in parts)
    if not profile_home:
        return ()
    return tuple(
        os.path.realpath(os.path.abspath(os.path.join(profile_home, name)))
        for name in DEFAULT_SUBDIRS
    )


def _is_within(target: str, root: str) -> bool:
    try:
        return os.path.commonpath([target, root]) == root
    except ValueError:  # 不同盘符 / 不同挂载点
        return False


def is_allowed(path: str, roots: tuple[str, ...]) -> bool:
    if not path or not roots:
        return False
    resolved = os.path.realpath(os.path.abspath(path))
    return any(_is_within(resolved, root) for root in roots)


def ensure_allowed(path: str, roots: tuple[str, ...]) -> str:
    if not path:
        raise HubError("invalid_params", "path is required")
    if not is_allowed(path, roots):
        raise HubError("path_not_allowed", "path is outside the allowed media roots")
    return os.path.realpath(os.path.abspath(path))
