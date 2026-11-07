"""健康与诊断。

**刻意偏离 CLOUD_AGENT §5.5**：匿名端点不再吐整套诊断。

旧插件（线上 v0.1.0）的 `/v1/hub/health` 对任何陌生人返回插件版本、上游版本、
内部端口、dashboard 地址、设备总数、管理密钥是否已配、进程运行时长 —— 这些都是
免费的指纹信息，而桌面端真正需要的只有「网关活着 / 上游活着」。

所以：

* `GET /v1/hub/health`（匿名）→ `status` + `upstreamOk` + 版本与契约握手字段；
* `GET /v1/hub/status`（设备 token 或管理密钥）→ 完整诊断。
"""

from __future__ import annotations

from ..app import CONTRACT_VERSION, FEATURES, HubApp
from ..router import AUTH_DEVICE_OR_ADMIN, AUTH_NONE, Request, Response, Router, json_response


def register(router: Router, app: HubApp) -> None:
    def health(_request: Request) -> Response:
        payload = app.health_payload()
        payload["hub"] = app.version
        payload["contractVersion"] = CONTRACT_VERSION
        payload["features"] = list(FEATURES)
        return json_response(payload)

    def status(_request: Request) -> Response:
        return json_response(app.status_payload())

    router.add("GET", "/v1/hub/health", health, auth=AUTH_NONE)
    router.add("GET", "/v1/hub/status", status, auth=AUTH_DEVICE_OR_ADMIN)
