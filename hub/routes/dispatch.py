"""派活的两条腿：设备中继（SSE + 回报）与目录查询。

鉴权复用网关现成的设备 token + scope：中继与回报都要 `dispatch` scope，
所以一个只有 `chat` 权限的设备不会被派活。审计只记任务生命周期，不记正文。
"""

from __future__ import annotations

import json
import queue
import time

from ..app import HubApp
from ..errors import HubError
from ..router import (
    AUTH_DEVICE,
    AUTH_DEVICE_OR_ADMIN,
    Request,
    Response,
    Router,
    StreamResponse,
    json_response,
)

# 中继心跳：设备侧靠它判断「这条连接还活着」
RELAY_PING_SECONDS = 25
# 单条中继连接的最长寿命：到期让客户端重连，顺带把内存与代理层的超时都洗一遍
RELAY_MAX_AGE_SECONDS = 3600

REPORT_KINDS = ("claimed", "progress", "question", "done", "failed", "canceled")


def _frame(event: str, payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n".encode("utf-8")


def task_frame(task: dict) -> bytes:
    return _frame(
        "task.assigned",
        {
            "type": "task.assigned",
            "taskId": task["taskId"],
            "prompt": task["prompt"],
            "deviceId": task["deviceId"],
            "workspaceId": task["workspaceId"],
            "contextId": task["contextId"],
            "deadlineAt": task["deadlineAt"],
            "attempt": task["attempt"],
        },
    )


def register(router: Router, app: HubApp) -> None:
    def relay_stream(request: Request) -> StreamResponse:
        device = request.device
        if not app.relay or not app.dispatch:
            raise HubError("internal", "dispatch is not available")
        channel = app.relay.subscribe(device.device_id)
        # 连上先补课：断线期间入队的任务不能靠「下次推送」才被发现
        snapshot = app.dispatch.queued_for(device.device_id)
        subscribers = app.relay.subscribers(device.device_id)
        app.audit.log(
            "dispatch.stream_opened",
            device_id=device.device_id,
            ip=request.client_ip,
            path=request.path,
            status=200,
            detail={"pending": len(snapshot), "streams": subscribers},
        )

        def chunks():
            started = time.monotonic()
            try:
                yield b": open\n\n"
                for task in snapshot:
                    yield task_frame(task)
                while True:
                    if time.monotonic() - started > RELAY_MAX_AGE_SECONDS:
                        yield _frame("relay.reconnect", {"type": "relay.reconnect"})
                        return
                    try:
                        frame = channel.get(timeout=RELAY_PING_SECONDS)
                    except queue.Empty:
                        yield b": ping\n\n"
                        continue
                    yield _frame(str(frame.get("type") or "message"), frame)
            finally:
                app.relay.unsubscribe(device.device_id, channel)

        return StreamResponse(
            chunks(),
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def relay_report(request: Request) -> Response:
        device = request.device
        body = request.json()
        task_id = str(body.get("taskId") or "").strip()
        kind = str(body.get("kind") or "").strip()
        if not task_id:
            raise HubError("invalid_params", "taskId is required")
        if kind not in REPORT_KINDS:
            raise HubError("invalid_params", f"kind must be one of {', '.join(REPORT_KINDS)}")
        task = app.dispatch.task(task_id)
        if task is None:
            raise HubError("not_found", "unknown task")
        if task["deviceId"] != device.device_id:
            # 不是自己的任务一律当作不存在，不泄漏别的设备在跑什么
            raise HubError("not_found", "unknown task")

        if kind == "claimed":
            accepted = app.dispatch.claim(task_id)
            state = app.dispatch.task(task_id)["state"]
        else:
            accepted, state = app.dispatch.report(
                task_id,
                kind=kind,
                summary=body.get("summary"),
                files=body.get("files"),
                usage=body.get("usage"),
                error=body.get("error"),
                question=body.get("question"),
                local_session_id=body.get("localSessionId"),
            )
        if accepted and kind in ("done", "failed", "question", "canceled"):
            app.audit.log(
                f"dispatch.{kind}",
                device_id=device.device_id,
                ip=request.client_ip,
                path=request.path,
                status=200,
                detail={"task": task_id[:24], "state": state},
            )
        return json_response({"accepted": accepted, "state": state})

    def put_workspaces(request: Request) -> Response:
        device = request.device
        body = request.json()
        workspaces = body.get("workspaces")
        if workspaces is not None and not isinstance(workspaces, list):
            raise HubError("invalid_params", "workspaces must be a list")
        stored = app.dispatch.replace_workspaces(
            device.device_id,
            workspaces or [],
            dispatchable=bool(body.get("dispatchable", True)),
        )
        app.audit.log(
            "dispatch.catalog",
            device_id=device.device_id,
            ip=request.client_ip,
            path=request.path,
            status=200,
            detail={"workspaces": stored},
        )
        return json_response({"workspaces": stored})

    def list_workspaces(request: Request) -> Response:
        items = app.dispatch.list_workspaces()
        now = int(app.now())
        out = []
        for item in items:
            device_id = item["deviceId"]
            row = app.db.device(device_id)
            online = bool(
                row
                and row["revoked_at"] is None
                and (app.relay.online(device_id)
                     or int(row["last_seen_at"] or 0) >= now - 90)
            )
            out.append(
                {
                    **item,
                    "online": online,
                    "relayStreams": app.relay.subscribers(device_id),
                    "running": app.dispatch.running_count(device_id),
                }
            )
        return json_response({"workspaces": out})

    router.add("GET", "/v1/hub/relay/stream", relay_stream, auth=AUTH_DEVICE, scope="dispatch")
    router.add("POST", "/v1/hub/relay/report", relay_report, auth=AUTH_DEVICE, scope="dispatch")
    router.add("POST", "/v1/hub/workspaces", put_workspaces, auth=AUTH_DEVICE, scope="dispatch")
    router.add(
        "GET", "/v1/hub/dispatch/workspaces", list_workspaces, auth=AUTH_DEVICE_OR_ADMIN
    )
