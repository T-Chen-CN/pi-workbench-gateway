"""错误形状：`{"error": {"code", "message", "type"}}`。

code 表来自 CLOUD_AGENT §4.4；桌面端按 `code` 与 HTTP 状态码分支，两者都不能飘。
"""

from __future__ import annotations

ERROR_TYPE = "hub_error"

# code -> (status, 默认中文提示)
_TABLE: dict[str, tuple[int, str]] = {
    "unauthorized": (401, "缺少或无效的设备凭据"),
    "device_revoked": (401, "这台设备已被注销，请重新配对"),
    "scope_required": (403, "设备权限不足"),
    "admin_required": (403, "缺少或无效的管理密钥"),
    "invalid_params": (400, "请求参数缺失或格式错误"),
    "pairing_not_found": (404, "连接码不存在"),
    "pairing_expired": (410, "连接码已过期，请在服务器重新生成"),
    "pairing_used": (409, "连接码已被使用"),
    "pairing_disabled": (503, "服务器未配置管理密钥，配对已关闭"),
    "rate_limited": (429, "请求过于频繁"),
    "path_not_allowed": (403, "路径不在允许范围内"),
    "payload_too_large": (413, "请求体过大"),
    "upstream_unavailable": (502, "api_server 不可用"),
    "upstream_timeout": (504, "api_server 超时"),
    "not_found": (404, "未知路由"),
    "method_not_allowed": (405, "方法不允许"),
    "internal": (500, "网关内部错误"),
}

CODES = frozenset(_TABLE)


class HubError(Exception):
    """所有可预期的失败都走这里，由 server 统一转成 JSON。"""

    def __init__(self, code: str, message: str | None = None, *, headers: dict[str, str] | None = None):
        status, default = _TABLE.get(code, (500, "网关内部错误"))
        super().__init__(message or default)
        self.code = code if code in _TABLE else "internal"
        self.status = status
        self.message = message or default
        self.headers = dict(headers or {})


def not_found(path: str, method: str) -> HubError:
    """deny-by-default 的默认出口：不在白名单里的路径就是这个。"""
    return HubError("not_found", f"no hub route for {method} {path}")
