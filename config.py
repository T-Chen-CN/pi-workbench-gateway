"""唯一读取环境变量的模块。

`hub/` 不读环境变量：它的所有配置都由这里组装好之后传进去。
这样 `hub` 才能在没有 Hermes 的机器上单独跑测试。

管理密钥的处理按 CLOUD_AGENT §5.3：

* 来源是 Hermes 的 `<profile>/.env`：`hermes plugins install` 装完会提示填写
  `PI_HUB_ADMIN_KEY` 并写进那里，gateway 启动时把 `.env` 装载进 `os.environ`
  （`hermes_cli/env_loader.load_hermes_dotenv`，`gateway/run.py` 导入）。本模块只读
  进程环境，**自己不解析 `.env`**；
* 读完之后从 `os.environ` 删掉，缩小被子进程顺走的面积——但这不是保密手段
  （Hermes 的子进程会自行重载 `.env`）；保密靠 `.env` 的 600 权限与永不回显；
* 比较用常量时间，永不进日志。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


PLUGIN_NAME = "pi-workbench-gateway"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8484
DEFAULT_RATE_LIMIT = 60
DEFAULT_API_BASE = "http://127.0.0.1:8642"
DEFAULT_DASHBOARD_BASE = "http://127.0.0.1:9119"

ADMIN_KEY_ENV = "PI_HUB_ADMIN_KEY"


@dataclass(frozen=True)
class HubConfig:
    """一份不可变的运行期配置。"""

    admin_key: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    public_url: str = ""
    public_dashboard_url: str = ""
    upstream_base: str = DEFAULT_API_BASE
    upstream_key: str = ""
    dashboard_base: str = DEFAULT_DASHBOARD_BASE
    dashboard_key: str = ""
    rate_limit: int = DEFAULT_RATE_LIMIT
    fs_roots: tuple[str, ...] = field(default_factory=tuple)
    data_dir: str = ""

    @property
    def pairing_enabled(self) -> bool:
        return bool(self.admin_key)


def _int_env(environ: dict[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if not raw:
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _derive_dashboard_url(public_url: str) -> str:
    """`https://hub.example.com` → `https://dash.example.com`；推不出来就返回空串。"""
    if not public_url:
        return ""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(public_url)
    host = parts.hostname or ""
    if not host.startswith("hub."):
        return ""
    netloc = "dash." + host[len("hub.") :]
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme or "https", netloc, "", "", ""))


def _resolve_roots(raw: str | None, profile_home: str) -> tuple[str, ...]:
    """惰性导入：`config.py` 既被 Hermes 当作包内模块，也被测试当作顶层模块导入。"""
    try:
        from .hub.fsguard import resolve_roots
    except ImportError:  # pragma: no cover - 仅在作为顶层模块导入时
        from hub.fsguard import resolve_roots
    return resolve_roots(raw, profile_home)


def load_config(environ: dict[str, str] | None = None, *, data_dir: str = "") -> HubConfig:
    """从环境变量组装配置。**不**修改 `environ`（由 `take_admin_key` 负责抹除）。"""
    env = os.environ if environ is None else environ
    profile_home = env.get("HERMES_HOME", "")
    public_url = (env.get("PI_HUB_PUBLIC_URL", "") or "").strip().rstrip("/")
    public_dashboard = (env.get("PI_HUB_PUBLIC_DASHBOARD_URL", "") or "").strip().rstrip("/")
    if not public_dashboard:
        public_dashboard = _derive_dashboard_url(public_url)

    return HubConfig(
        admin_key=env.get(ADMIN_KEY_ENV, "") or "",
        host=(env.get("PI_HUB_HOST", "") or DEFAULT_HOST).strip(),
        port=_int_env(env, "PI_HUB_PORT", DEFAULT_PORT),
        public_url=public_url,
        public_dashboard_url=public_dashboard,
        upstream_base=(env.get("HERMES_API_BASE", "") or DEFAULT_API_BASE).strip().rstrip("/"),
        upstream_key=env.get("API_SERVER_KEY", "") or "",
        dashboard_base=(env.get("PI_HUB_DASHBOARD_BASE", "") or DEFAULT_DASHBOARD_BASE)
        .strip()
        .rstrip("/"),
        dashboard_key=env.get("HERMES_DASHBOARD_SESSION_TOKEN", "") or "",
        rate_limit=_int_env(env, "PI_HUB_RATE_LIMIT", DEFAULT_RATE_LIMIT),
        fs_roots=_resolve_roots(env.get("PI_HUB_FS_ROOTS"), profile_home),
        data_dir=data_dir,
    )


def take_admin_key(environ: dict[str, str] | None = None) -> str:
    """读出管理密钥并从环境里删除它，避免被子进程继承。

    `config.load_config` 读过的值仍然有效，只是 `os.environ` 里不再有它。
    """
    env = os.environ if environ is None else environ
    value = env.pop(ADMIN_KEY_ENV, "") or ""
    return value
