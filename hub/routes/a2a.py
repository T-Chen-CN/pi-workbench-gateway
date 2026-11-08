"""对外 A2A 端点：Agent Card + `POST /a2a` JSON-RPC（H3 P3）。

为什么现在才做：P1/P2 的内部工具（`local_*`）已经能跑通全链路，对外信封
只是「给别的 A2A 客户端一个入口」，不影响状态机。**语义照 A2A v1 写**，
所以这里只是把 `DispatchStore` 的任务投影成 A2A 的 `Task`，不做第二套状态。

鉴权：管理密钥（本机 / 运维）或设备 token（带 `dispatch` scope）。卡片与
RPC 都是同一个入口，没有匿名路径 —— 卡片里带的是用户的工作区名字，
不对外公开。
"""

from __future__ import annotations

import json
import time

from ..app import HubApp
from ..errors import HubError, not_found
from ..frames import answered_payload, cancel_payload, task_payload
from ..router import (
    AUTH_DEVICE_OR_ADMIN,
    Request,
    Response,
    Router,
    StreamResponse,
    json_response,
)

CARD_PATH = "/.well-known/agent-card.json"
A2A_PATH = "/a2a"

PROTOCOL_VERSION = "1.0"
# A2A 的 `message/send` 里 `configuration.waitSeconds` 的上限：与工具一样的 90s
MAX_WAIT_SECONDS = 90.0
# `tasks/resubscribe` 的流最长活多久：超过就让客户端重订阅
RESUBSCRIBE_MAX_SECONDS = 900

# 内部状态 → A2A `TaskState`。`expired` 在 A2A 里没有对应值，按 failed 表达
STATE_TO_A2A = {
    "queued": "submitted",
    "running": "working",
    "input_required": "input-required",
    "completed": "completed",
    "failed": "failed",
    "canceled": "canceled",
    "expired": "failed",
}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _rpc_error(rpc_id, code: int, message: str) -> Response:
    return json_response(
        {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
    )


def task_object(task: dict, workspace: dict | None, include: tuple[str, ...] = ()) -> dict:
    """内部任务 → A2A `Task`。`metadata` 保留设备 / 工作区等内部锚点。

    `include` 里有 `"trace"` 时才带执行轨迹（`metadata.trace`）：轨迹默认不推送，
    要看就显式要，免得每次轮询把几十 KB 的步骤都拖回去。
    """
    works = workspace or {}
    # `_task_row` 没有 updatedAt：用「最后发生的事」当时间戳
    stamp = (
        task.get("finishedAt")
        or task.get("startedAt")
        or task.get("claimedAt")
        or task.get("createdAt")
        or 0
    )
    status = {
        "state": STATE_TO_A2A.get(str(task["state"]), "unknown"),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(stamp))),
    }
    if task.get("question"):
        status["message"] = {
            "role": "agent",
            "parts": [{"kind": "text", "text": str(task["question"])}],
        }
    if task.get("error"):
        status["message"] = {
            "role": "agent",
            "parts": [{"kind": "text", "text": str(task["error"])}],
        }
    artifacts = []
    if task.get("summary"):
        artifacts.append(
            {
                "artifactId": f"{task['taskId']}-summary",
                "name": "summary",
                "parts": [{"kind": "text", "text": str(task["summary"])}],
            }
        )
    metadata: dict = {
        "deviceId": task["deviceId"],
        "workspaceId": task["workspaceId"],
        "workspaceLabel": works.get("label") or task["workspaceId"],
        "attempt": task["attempt"],
        "filesChanged": task.get("filesChanged") or [],
        "usage": task.get("usage") or {},
        "localSessionId": task.get("localSessionId"),
        "createdAt": task["createdAt"],
        "deadlineAt": task["deadlineAt"],
    }
    if "trace" in include and task.get("trace"):
        metadata["trace"] = task["trace"]
    return {
        "kind": "task",
        "id": task["taskId"],
        "contextId": task["contextId"] or "",
        "status": status,
        "artifacts": artifacts,
        "metadata": metadata,
    }


def _include(params: dict) -> tuple[str, ...]:
    raw = params.get("include")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(item) for item in raw if isinstance(item, str))


def _text_of(message: dict) -> str:
    parts = message.get("parts") if isinstance(message, dict) else None
    for part in parts or []:
        if isinstance(part, dict) and part.get("kind") == "text":
            text = str(part.get("text") or "").strip()
            if text:
                return text
    return ""


def _owner(request: Request) -> str | None:
    """设备 token 只能看/派自己的东西；admin 密钥（request.device 为 None）不受限。

    不这么做，任何一台设备拿自己的 token 就能读别台设备的任务、往别台设备派活 ——
    `/v1/hub/tasks/{id}` 早就按 device 校验，A2A 不能是个例外。
    """
    device = getattr(request, "device", None)
    return getattr(device, "device_id", None) if device else None


def _visible(task: dict | None, owner: str | None) -> dict | None:
    if task is None:
        return None
    if owner and task.get("deviceId") != owner:
        return None
    return task


def _resolve(app: HubApp, skill: str, owner: str | None = None) -> tuple[str, str, dict] | str:
    """`device/workspace` 或工作区名 → 目标；返回字符串表示错误信息。"""
    items = [i for i in app.dispatch.list_workspaces() if not owner or i["deviceId"] == owner]
    target = (skill or "").strip()
    if not target:
        return "缺少 skill：要指定 `设备/工作区` 或工作区名。"
    for item in items:
        key = f"{item['deviceId']}/{item['workspaceId']}"
        # 只认「设备/工作区」「workspaceId」「label」三种精确写法：
        # 允许后缀匹配会让 "bfc" 这类片段命中意外的工作区
        if target in (key, item["workspaceId"], item["label"]):
            if not item["dispatchable"]:
                return f"工作区 {key} 没有打开派活开关。"
            return item["deviceId"], item["workspaceId"], item
    names = "、".join(f"{i['deviceId']}/{i['workspaceId']}" for i in items) or "（没有可派活的工作区）"
    return f"没有找到目标工作区 {target}。可用：{names}"


def register(router: Router, app: HubApp) -> None:
    def agent_card(request: Request) -> Response:
        owner = _owner(request)
        items = [
            item
            for item in app.dispatch.list_workspaces()
            if item["dispatchable"] and (not owner or item["deviceId"] == owner)
        ]
        now = int(app.now())
        skills = []
        for item in items:
            device = item["deviceId"]
            row = app.db.device(device)
            online = bool(
                row
                and row["revoked_at"] is None
                and (app.relay.online(device) or int(row["last_seen_at"] or 0) >= now - 90)
            )
            skills.append(
                {
                    "id": f"{device}/{item['workspaceId']}",
                    "name": f"{device} / {item['label']}",
                    # description 必须带触发词：小模型靠它选目标，只写名字会选错
                    "description": item["description"]
                    or f"派活到 {device} 的 {item['label']} 工作区执行编码任务。",
                    "tags": ["code"] if not item["description"] else ["code", "dispatch"],
                    "metadata": {"online": online, "dispatchable": item["dispatchable"]},
                }
            )
        return json_response(
            {
                "name": "pi-workbench",
                "description": "Pi Workbench 本地工作区集群：把任务派给某台机器的某个工作区，由该工作区的编码 Agent 执行。",
                "protocolVersion": PROTOCOL_VERSION,
                "url": f"{app.public_url or ''}{A2A_PATH}",
                "capabilities": {"streaming": True, "pushNotifications": False},
                "defaultInputModes": ["text/plain"],
                "defaultOutputModes": ["text/plain"],
                "skills": skills,
            }
        )

    def rpc(request: Request) -> Response | StreamResponse:
        raw = request.body.decode("utf-8", "replace").strip()
        if not raw:
            return _rpc_error(None, PARSE_ERROR, "请求体是空的")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return _rpc_error(None, PARSE_ERROR, "请求体不是合法 JSON")
        if not isinstance(body, dict):
            return _rpc_error(None, INVALID_REQUEST, "请求体必须是 JSON-RPC 对象")
        rpc_id = body.get("id")
        method = str(body.get("method") or "")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}

        if method == "message/send":
            return _send(request, rpc_id, params)
        if method == "tasks/get":
            return _get(request, rpc_id, params)
        if method == "tasks/cancel":
            return _cancel(request, rpc_id, params)
        if method == "tasks/resubscribe":
            return _resubscribe(request, rpc_id, params)
        return _rpc_error(rpc_id, METHOD_NOT_FOUND, f"不支持的方法 {method}")

    def _send(request: Request, rpc_id, params: dict) -> Response:
        message = params.get("message") if isinstance(params.get("message"), dict) else {}
        prompt = _text_of(message)
        if not prompt:
            return _rpc_error(rpc_id, INVALID_PARAMS, "message.parts 里没有文本")
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        skill = (
            str(params.get("skill") or "")
            or str(metadata.get("skillId") or "")
            or str(metadata.get("skill") or "")
        )
        # 回答 `input_required`：A2A 的语义是「给同一个 task 再发一条 message」，
        # 不是新起一个任务。不做这一步，外部客户端就没法回答本地 Agent 的反问。
        reply_to = str(params.get("taskId") or message.get("taskId") or "").strip()
        if reply_to:
            return _reply(request, rpc_id, reply_to, prompt, params)

        target = _resolve(app, skill, _owner(request))
        if isinstance(target, str):
            return _rpc_error(rpc_id, INVALID_PARAMS, target)
        device_id, workspace_id, workspace = target

        if not app.relay.online(device_id):
            row = app.db.device(device_id)
            if not (row and int(row["last_seen_at"] or 0) >= app.now() - 90):
                return _rpc_error(rpc_id, INVALID_PARAMS, f"设备 {device_id} 当前离线。")
        if app.dispatch.running_count(device_id) >= 1:
            return _rpc_error(rpc_id, INVALID_PARAMS, f"设备 {device_id} 正忙。")

        configuration = (
            params.get("configuration") if isinstance(params.get("configuration"), dict) else {}
        )
        wait_seconds = 0.0
        if configuration.get("blocking"):
            wait_seconds = float(configuration.get("waitSeconds") or 90)
        deadline_seconds = int(configuration.get("deadlineSeconds") or 600)

        task, created = app.dispatch.enqueue(
            device_id=device_id,
            workspace_id=workspace_id,
            prompt=prompt,
            context_id=str(params.get("contextId") or message.get("contextId") or ""),
            # A2A 的 messageId 天然稳定：同一个 message 重投只会派一个活
            client_token=str(metadata.get("clientToken") or message.get("messageId") or ""),
            deadline_seconds=deadline_seconds,
        )
        if created:
            app.relay.publish(device_id, task_payload(task))
        if wait_seconds > 0:
            task = (
                app.dispatch.wait(task["taskId"], min(wait_seconds, MAX_WAIT_SECONDS)) or task
            )
        app.audit.log(
            "a2a.send",
            device_id=device_id,
            ip=request.client_ip,
            path=request.path,
            status=200,
            detail={"task": task["taskId"][:24], "created": created},
        )
        return json_response(
            {"jsonrpc": "2.0", "id": rpc_id, "result": task_object(task, workspace)}
        )

    def _reply(request: Request, rpc_id, task_id: str, answer: str, params: dict) -> Response:
        task = _visible(app.dispatch.task(task_id), _owner(request))
        if task is None:
            return _rpc_error(rpc_id, INVALID_PARAMS, f"没有 id 为 {task_id} 的任务")
        if task["state"] != "input_required":
            return _rpc_error(
                rpc_id,
                INVALID_PARAMS,
                f"任务 {task_id} 现在不需要回答（状态 {task['state']}）",
            )
        if not app.dispatch.answer(task_id, answer):
            return _rpc_error(rpc_id, INVALID_PARAMS, f"任务 {task_id} 已经不在等待回答的状态")
        app.relay.publish(task["deviceId"], answered_payload(task_id, answer[:4000]))
        app.audit.log(
            "a2a.reply",
            device_id=task["deviceId"],
            ip=request.client_ip,
            path=request.path,
            status=200,
            detail={"task": task_id[:24]},
        )
        configuration = (
            params.get("configuration") if isinstance(params.get("configuration"), dict) else {}
        )
        if configuration.get("blocking"):
            wait_seconds = float(configuration.get("waitSeconds") or 90)
            app.dispatch.wait(task_id, min(wait_seconds, MAX_WAIT_SECONDS))
        current = app.dispatch.task(task_id) or task
        workspace = app.dispatch.workspace(current["deviceId"], current["workspaceId"])
        return json_response(
            {"jsonrpc": "2.0", "id": rpc_id, "result": task_object(current, workspace)}
        )

    def _get(request: Request, rpc_id, params: dict) -> Response:
        task_id = str(params.get("id") or "").strip()
        if not task_id:
            return _rpc_error(rpc_id, INVALID_PARAMS, "缺少 id")
        task = _visible(app.dispatch.task(task_id), _owner(request))
        if task is None:
            return _rpc_error(rpc_id, INVALID_PARAMS, f"没有 id 为 {task_id} 的任务")
        workspace = app.dispatch.workspace(task["deviceId"], task["workspaceId"])
        return json_response(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": task_object(task, workspace, _include(params)),
            }
        )

    def _cancel(request: Request, rpc_id, params: dict) -> Response:
        task_id = str(params.get("id") or "").strip()
        if not task_id:
            return _rpc_error(rpc_id, INVALID_PARAMS, "缺少 id")
        reason = str(params.get("reason") or "A2A 客户端取消")
        mine = _visible(app.dispatch.task(task_id), _owner(request))
        if mine is None:
            return _rpc_error(rpc_id, INVALID_PARAMS, f"没有 id 为 {task_id} 的任务")
        task = app.dispatch.cancel(task_id, reason=reason, by="a2a")
        if task is None:
            current = app.dispatch.task(task_id)
            if current is None:
                return _rpc_error(rpc_id, INVALID_PARAMS, f"没有 id 为 {task_id} 的任务")
            return json_response(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": task_object(
                        current, app.dispatch.workspace(current["deviceId"], current["workspaceId"])
                    ),
                }
            )
        app.relay.publish(task["deviceId"], cancel_payload(task_id, reason, by="a2a"))
        app.audit.log(
            "a2a.cancel",
            device_id=task["deviceId"],
            ip=request.client_ip,
            path=request.path,
            status=200,
            detail={"task": task_id[:24]},
        )
        workspace = app.dispatch.workspace(task["deviceId"], task["workspaceId"])
        return json_response(
            {"jsonrpc": "2.0", "id": rpc_id, "result": task_object(task, workspace)}
        )

    def _resubscribe(request: Request, rpc_id, params: dict) -> Response | StreamResponse:
        task_id = str(params.get("id") or "").strip()
        if not task_id:
            return _rpc_error(rpc_id, INVALID_PARAMS, "缺少 id")
        if _visible(app.dispatch.task(task_id), _owner(request)) is None:
            return _rpc_error(rpc_id, INVALID_PARAMS, f"没有 id 为 {task_id} 的任务")
        workspace = None
        first = app.dispatch.task(task_id)
        if first is not None:
            workspace = app.dispatch.workspace(first["deviceId"], first["workspaceId"])

        def chunks():
            started = time.monotonic()
            last_state = ""
            while time.monotonic() - started < RESUBSCRIBE_MAX_SECONDS:
                task = app.dispatch.task(task_id)
                if task is None:
                    return
                state = STATE_TO_A2A.get(str(task["state"]), "unknown")
                if state != last_state:
                    last_state = state
                    event = {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "result": {
                            "kind": "status-update",
                            "taskId": task_id,
                            "contextId": task["contextId"] or "",
                            "status": task_object(task, workspace)["status"],
                            "final": state in ("completed", "failed", "canceled"),
                        },
                    }
                    if state == "completed" and task.get("summary"):
                        event["result"]["artifacts"] = task_object(task, workspace)["artifacts"]
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
                    if event["result"]["final"]:
                        return
                app.dispatch.wait(task_id, 1.0)

        return StreamResponse(chunks(), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    router.add("GET", CARD_PATH, agent_card, auth=AUTH_DEVICE_OR_ADMIN, scope="dispatch")
    router.add("POST", A2A_PATH, rpc, auth=AUTH_DEVICE_OR_ADMIN, scope="dispatch")


__all__ = ["register", "CARD_PATH", "A2A_PATH", "task_object", "not_found"]
