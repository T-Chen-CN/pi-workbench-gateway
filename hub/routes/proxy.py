"""上游反向代理：deny-by-default 白名单。

只有下面列出的路径会被转发；其余 `/v1/*`、`/api/*` 一律 `404 not_found`
（`/api/config` 与媒体两条由 `policy.py` 单独处理，不在这里）。

SSE 的四条硬要求（CLOUD_AGENT §5.8）：

* 不设 `Content-Length`；
* 每个 chunk 立即 flush；
* `X-Accel-Buffering: no` + `Cache-Control: no-cache`；
* 上游 / 客户端断开都要正确收尾，并释放并发槽位。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..app import HubApp
from ..errors import HubError
from ..router import AUTH_DEVICE, JSON, Request, Response, Router, StreamResponse
from ..upstream import HOP_BY_HOP

CHUNK_SIZE = 8192
PASSTHROUGH_HEADERS = frozenset({"retry-after", "x-request-id", "cache-control"})


@dataclass(frozen=True)
class Rule:
    methods: tuple[str, ...]
    path: str
    stream: bool = False
    transform: str = ""


WHITELIST: tuple[Rule, ...] = (
    Rule(("GET",), "/v1/capabilities"),
    Rule(("GET",), "/health/detailed"),
    Rule(("GET",), "/v1/toolsets"),
    Rule(("GET",), "/v1/skills"),
    Rule(("GET",), "/v1/models"),
    Rule(("POST",), "/v1/runs", transform="runs_create"),
    Rule(("GET",), "/v1/runs/{run_id}"),
    Rule(("GET",), "/v1/runs/{run_id}/events", stream=True),
    Rule(("POST",), "/v1/runs/{run_id}/stop"),
    Rule(("POST",), "/v1/runs/{run_id}/steer"),
    Rule(("POST",), "/v1/runs/{run_id}/approval"),
    Rule(("POST",), "/v1/runs/{run_id}/clarify"),
    Rule(("POST",), "/v1/runs/{run_id}/resolve-unknown"),
    Rule(("GET",), "/api/sessions"),
    Rule(("POST",), "/api/sessions"),
    Rule(("GET",), "/api/sessions/{session_id}"),
    Rule(("PATCH",), "/api/sessions/{session_id}"),
    Rule(("DELETE",), "/api/sessions/{session_id}"),
    Rule(("GET",), "/api/sessions/{session_id}/messages"),
    # 不放行 `/api/sessions/{id}/fork`：它会把源会话置为 branched 结束并整段复制历史，
    # 桌面端没有对应入口，放行等于留一条没人用的写路径。
    Rule(("GET",), "/api/jobs"),
    Rule(("POST",), "/api/jobs"),
    Rule(("GET",), "/api/jobs/{job_id}"),
    Rule(("POST",), "/api/jobs/{job_id}/run"),
    Rule(("POST",), "/api/jobs/{job_id}/pause"),
    Rule(("POST",), "/api/jobs/{job_id}/resume"),
    Rule(("PATCH",), "/api/jobs/{job_id}"),
    Rule(("DELETE",), "/api/jobs/{job_id}"),
)


def register(router: Router, app: HubApp) -> None:
    for rule in WHITELIST:
        router.add(rule.methods, rule.path, _make_handler(rule, app), auth=AUTH_DEVICE)


def _make_handler(rule: Rule, app: HubApp):
    def handler(request: Request) -> Response | StreamResponse:
        return _forward(rule, app, request)

    return handler


def _forward(rule: Rule, app: HubApp, request: Request) -> Response | StreamResponse:
    target = request.path + (f"?{request.query_string}" if request.query_string else "")
    body = request.body
    headers = app.upstream.request_headers(request.headers)
    # 注入归属头（客户端自带的 `X-Pi-Device` 已在 request_headers 里被白名单滤掉）
    headers["x-pi-device"] = request.device.device_id

    if rule.transform == "runs_create":
        body, headers = _transform_runs_create(request, app, headers)

    response = app.upstream.open(
        request.method, target, headers=headers, body=body, stream=rule.stream
    )

    if rule.stream:
        app.sse_acquire(request.device.device_id)
        return _stream(response, app, request)

    try:
        payload = response.read()
    finally:
        response.close()
    app.audit.log(
        "proxy.completed",
        device_id=request.device.device_id,
        ip=request.client_ip,
        path=request.path,
        status=response.status,
    )
    return Response(
        status=response.status,
        body=payload,
        headers=_passthrough(response),
        content_type=response.getheader("content-type") or JSON,
    )


def _transform_runs_create(
    request: Request, app: HubApp, headers: dict[str, str]
) -> tuple[bytes, dict[str, str]]:
    """`author` 覆盖为网关视角的 `pi-workbench/<deviceId>`；幂等键加设备前缀。

    `author` 只做记忆归属，不进 prompt（CLOUD_AGENT §3.7）。
    """
    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HubError("invalid_params", "请求体不是合法 JSON")
    if not isinstance(data, dict):
        raise HubError("invalid_params", "请求体必须是 JSON 对象")

    device = request.device
    data["author"] = {
        "id": f"pi-workbench/{device.device_id}",
        "name": device.name,
        "is_bot": False,
    }
    key = headers.get("idempotency-key")
    if key:
        headers["idempotency-key"] = f"pi:{device.device_id}:{key}"
    headers["content-type"] = "application/json"
    return json.dumps(data, ensure_ascii=False).encode("utf-8"), headers


def _passthrough(response) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in response.getheaders():
        lowered = name.lower()
        if lowered in HOP_BY_HOP or lowered == "content-type":
            continue
        if lowered in PASSTHROUGH_HEADERS:
            headers[name] = value
    return headers


def _stream(response, app: HubApp, request: Request) -> StreamResponse:
    def chunks():
        status = response.status
        try:
            # 逐行读：SSE 帧以换行分隔，`read(n)` 会一直阻塞到 n 字节或 EOF
            # （那正是「对话卡住、最后一次性出全文」的症状）
            while True:
                line = response.readline()
                if not line:
                    break
                yield line
        except Exception:
            # 上游中途断开：直接把连接关掉，让客户端看到断流（不当成功）
            return
        finally:
            response.close()
            app.sse_release(request.device.device_id)
            app.audit.log(
                "proxy.completed",
                device_id=request.device.device_id,
                ip=request.client_ip,
                path=request.path,
                status=status,
            )

    headers = {
        "X-Accel-Buffering": "no",
        "Cache-Control": "no-cache",
        "Connection": "close",
    }
    return StreamResponse(
        chunks(),
        status=response.status,
        headers=headers,
        content_type="text/event-stream; charset=utf-8",
    )
