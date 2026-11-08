"""云端 Hermes 侧的派活工具。

这一层只做三件事：把模型的话翻译成 store 调用、等结果、把结果压缩成一段
模型读得懂的短文本。它**直接读写同进程内的 store**（不绕 HTTP）——
网关的 `hub/` 不知道 Hermes 存在，粘合只发生在这里和 `shim.py`。

纪律：

* 工具永远返回**字符串**（上游 handler 约定），错误也返回可读文本而不是抛异常 ——
  抛异常会让整轮失败，而派活失败本该是「换台设备再试」这种可恢复情况；
* `dispatchable=false` / 设备离线 / 队列满都要给出**模型能据此决策**的话；
* 每次调用都带上 `client_token` 语义，重放不会派两个活。
"""

from __future__ import annotations

import json
from typing import Any

TOOLSET = "pi_workbench"

DEFAULT_WAIT_SECONDS = 90.0
MAX_WAIT_SECONDS = 240.0
SUMMARY_LIMIT = 4000


class DispatchTools:
    """工具实现。`state` 是 `shim.py` 注入的持有者：`{"app": HubApp | None}`。"""

    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state

    # -- 依赖 ---------------------------------------------------------------

    def _app(self):
        app = self._state.get("app")
        if app is None:
            raise _NotReady(_DEFAULT_NOT_READY)
        return app

    def _store(self):
        return self._app().dispatch

    # -- 工具 ---------------------------------------------------------------

    def workspaces(self, args: dict, **_: Any) -> str:
        try:
            app = self._app()
        except _NotReady as error:
            return str(error)
        items = app.dispatch.list_workspaces()
        if not items:
            return (
                "没有任何可派活的工作区。用户在 Pi Workbench 的「云端 → 派活」里打开开关并勾选工作区后，"
                "这里才会出现目标。"
            )
        now = app.now()
        lines = []
        for item in items:
            row = app.db.device(item["deviceId"])
            online = bool(
                row
                and row["revoked_at"] is None
                and (app.relay.online(item["deviceId"]) or int(row["last_seen_at"] or 0) >= now - 90)
            )
            lines.append(
                json.dumps(
                    {
                        "workspace": f"{item['deviceId']}/{item['workspaceId']}",
                        "device": item["deviceId"],
                        "label": item["label"],
                        "description": item["description"],
                        "dispatchable": item["dispatchable"],
                        "online": online,
                        "running": app.dispatch.running_count(item["deviceId"]),
                    },
                    ensure_ascii=False,
                )
            )
        return "可派活工作区（每行一个）：\n" + "\n".join(lines)

    def dispatch(self, args: dict, **_: Any) -> str:
        try:
            app = self._app()
        except _NotReady as error:
            return str(error)
        store = app.dispatch

        target = str(args.get("workspace") or "").strip()
        prompt = str(args.get("prompt") or "").strip()
        if not target or not prompt:
            return "参数不全：需要 workspace（`device/workspace` 或工作区名）和 prompt。"
        resolved, problem = self._resolve(app, target)
        if problem:
            return problem
        device_id, workspace_id, workspace = resolved

        if not app.relay.online(device_id):
            row = app.db.device(device_id)
            recent = row and int(row["last_seen_at"] or 0) >= app.now() - 90
            if not recent:
                return (
                    f"设备 {device_id} 当前离线，任务没有派出去。请让用户先把那台机器上的 "
                    "Pi Workbench 打开（或稍后再试）。"
                )
        if store.running_count(device_id) >= 1:
            return f"设备 {device_id} 正忙（已有任务在跑）。等它结束，或换一台设备。"

        deadline_seconds = int(args.get("deadline_seconds") or 600)
        task, created = store.enqueue(
            device_id=device_id,
            workspace_id=workspace_id,
            prompt=prompt,
            context_id=str(args.get("context_id") or ""),
            client_token=str(args.get("client_token") or ""),
            deadline_seconds=deadline_seconds,
        )
        if created:
            app.relay.publish(device_id, _assigned_frame(task))
        wait_seconds = _clamp_wait(args.get("wait_seconds"))
        task = store.wait(task["taskId"], wait_seconds) or task
        return _render(task, workspace)

    def task_get(self, args: dict, **_: Any) -> str:
        try:
            app = self._app()
        except _NotReady as error:
            return str(error)
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return "缺少 task_id。"
        task = app.dispatch.task(task_id)
        if task is None:
            return f"没有 id 为 {task_id} 的任务（可能已被清理）。"
        workspace = app.dispatch.workspace(task["deviceId"], task["workspaceId"]) or {}
        wait_seconds = _clamp_wait(args.get("wait_seconds"), default=0.0)
        if wait_seconds > 0 and task["state"] not in ("completed", "failed", "canceled", "expired"):
            task = app.dispatch.wait(task_id, wait_seconds) or task
        return _render(task, workspace)

    def task_reply(self, args: dict, **_: Any) -> str:
        try:
            app = self._app()
        except _NotReady as error:
            return str(error)
        task_id = str(args.get("task_id") or "").strip()
        answer = str(args.get("answer") or "").strip()
        if not task_id or not answer:
            return "参数不全：需要 task_id 和 answer。"
        task = app.dispatch.task(task_id)
        if task is None:
            return f"没有 id 为 {task_id} 的任务（可能已被清理）。"
        if task["state"] != "input_required":
            return f"任务 {task_id} 现在不需要回答（状态 {task['state']}）。"
        if not app.dispatch.answer(task_id, answer):
            return f"回答没有送达：任务 {task_id} 已经不在等待回答的状态。"
        app.relay.publish(
            task["deviceId"],
            {
                "type": "task.answered",
                "taskId": task_id,
                "answer": answer[:4000],
            },
        )
        task = app.dispatch.wait(task_id, _clamp_wait(args.get("wait_seconds"))) or task
        workspace = app.dispatch.workspace(task["deviceId"], task["workspaceId"]) or {}
        return _render(task, workspace)

    def task_cancel(self, args: dict, **_: Any) -> str:
        try:
            app = self._app()
        except _NotReady as error:
            return str(error)
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return "缺少 task_id。"
        reason = str(args.get("reason") or "云端取消")
        task = app.dispatch.cancel(task_id, reason=reason, by="cloud")
        if task is None:
            current = app.dispatch.task(task_id)
            if current is None:
                return f"没有 id 为 {task_id} 的任务。"
            return f"任务 {task_id} 已经结束（状态 {current['state']}），没有取消。"
        app.relay.publish(
            task["deviceId"],
            {"type": "task.cancel", "taskId": task_id, "reason": reason[:400]},
        )
        return f"已取消任务 {task_id}（{reason}）。设备收到取消后会在下一个工具边界停下。"

    # -- 辅助 ---------------------------------------------------------------

    def _resolve(self, app, target: str) -> tuple[tuple[str, str, dict] | None, str]:
        items = app.dispatch.list_workspaces()
        if "/" in target:
            device_id, workspace_id = target.split("/", 1)
            for item in items:
                if item["deviceId"] == device_id and item["workspaceId"] == workspace_id:
                    if not item["dispatchable"]:
                        return None, f"工作区 {target} 没有打开派活开关。"
                    return (device_id, workspace_id, item), ""
            return None, f"目录里没有 {target}。可派活的工作区见 local_workspaces。"
        # 允许只给工作区名：同名多个就必须让模型自己挑
        matches = [item for item in items if item["workspaceId"] == target or item["label"] == target]
        if not matches:
            return None, f"目录里没有工作区 {target}。可派活的工作区见 local_workspaces。"
        if len(matches) > 1:
            options = ", ".join(f"{m['deviceId']}/{m['workspaceId']}" for m in matches)
            return None, f"有多个同名工作区（{options}），请用 `设备/工作区` 指定一个。"
        item = matches[0]
        if not item["dispatchable"]:
            return None, f"工作区 {target} 没有打开派活开关。"
        return (item["deviceId"], item["workspaceId"], item), ""


class _NotReady(RuntimeError):
    """网关服务还没起来（工具注册早于 platform connect）。"""


_DEFAULT_NOT_READY = "派活还没就绪：网关服务尚未启动，请稍后再试。"


def _clamp_wait(raw: object, *, default: float = DEFAULT_WAIT_SECONDS) -> float:
    try:
        value = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return max(0.0, min(value, MAX_WAIT_SECONDS))


def _assigned_frame(task: dict) -> dict:
    return {
        "type": "task.assigned",
        "taskId": task["taskId"],
        "prompt": task["prompt"],
        "deviceId": task["deviceId"],
        "workspaceId": task["workspaceId"],
        "contextId": task["contextId"],
        "deadlineAt": task["deadlineAt"],
        "attempt": task["attempt"],
    }


def _render(task: dict, workspace: dict) -> str:
    """把任务压成模型能读的一段话：足够做判断，又不至于把上下文撑爆。"""
    state = task["state"]
    label = workspace.get("label") or task["workspaceId"]
    device = task["deviceId"]
    head = f"任务 {task['taskId']}（{device} / {label}）：{state}"
    if state == "queued":
        return head + "。设备还没接走这个任务——它可能刚醒，稍后可以用 local_task_get 再查。"
    if state == "running":
        return head + "。还没有结果，用 local_task_get 继续查，不要重复派活。"
    if state == "input_required":
        return (
            head
            + f"\n本地 Agent 需要你决定：{task.get('question') or '(没有说明)'}\n"
            + f"能用你自己的判断就调 local_task_reply(task_id=\"{task['taskId']}\", answer=...)，"
            + "需要人来定就先问用户。"
        )
    if state == "expired":
        return head + "。超过截止时间仍未完成，可以换台设备重派。"
    if state == "canceled":
        return head + "。" + (task.get("error") or "")
    if state == "failed":
        return head + "。失败原因：" + (task.get("error") or "设备没有说明") + (
            "。可以修正后重派。"
        )
    # completed
    summary = (task.get("summary") or "").strip()[:SUMMARY_LIMIT]
    files = task.get("filesChanged") or []
    parts = [f"{head}。"]
    if summary:
        parts.append(summary)
    if files:
        parts.append("改动的文件：" + "、".join(files[:40]) + ("…" if len(files) > 40 else ""))
    usage = task.get("usage") or {}
    if isinstance(usage, dict) and usage.get("totalTokens"):
        parts.append(f"本地用量：{usage.get('totalTokens')} tokens")
    attempt = task.get("attempt") or 0
    if attempt > 1:
        parts.append(f"（重投过 {attempt} 次）")
    return "\n".join(parts)


def tool_schemas() -> list[dict]:
    """`register_tool` 用的 schema 列表。描述里写清触发词，模型靠它选工具。"""

    return [
        {
            "name": "local_workspaces",
            "description": (
                "列出可以派活的本地工作区（Pi Workbench 桌面端）。每行给出 "
                "`设备/工作区`、显示名、描述、是否在线、是否正忙。派活前先看一眼。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "local_dispatch",
            "description": (
                "把一个任务派给某台机器上的某个本地工作区，由那里的编码 Agent 执行，"
                "并等待结果（默认最多等 90 秒）。适合：改代码、跑测试、看本地文件、"
                "在用户的机器上做只有本地环境才能做的事。拿到结果后用自然语言向用户汇报。"
                "同一个 client_token 重复调用只会派一次。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace": {
                        "type": "string",
                        "description": "`设备/工作区`，或唯一的工作区名（先用 local_workspaces 查）",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "给本地 Agent 的完整指令：要做什么、验收标准、别做什么",
                    },
                    "client_token": {
                        "type": "string",
                        "description": "幂等键；重试时传同一个值可以避免派两次活",
                    },
                    "context_id": {
                        "type": "string",
                        "description": "把同一件事的多次派活串起来（可选）",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": f"最多等多久（默认 {int(DEFAULT_WAIT_SECONDS)}，上限 {int(MAX_WAIT_SECONDS)}）",
                    },
                    "deadline_seconds": {
                        "type": "number",
                        "description": "任务总时限，超时作废（默认 600）",
                    },
                },
                "required": ["workspace", "prompt"],
            },
        },
        {
            "name": "local_task_get",
            "description": "查一个派活任务的状态与结果。超时后用它续查；不要因为没结果就重复派活。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "local_dispatch 返回的任务 id"},
                    "wait_seconds": {
                        "type": "number",
                        "description": "最多再等多久（默认 0，只查一次）",
                    },
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "local_task_reply",
            "description": (
                "回答本地 Agent 的反问（任务处于 input_required）。回答会让本地那一轮继续；"
                "你自己能定就直接定，拿不准就先问用户。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "answer": {"type": "string", "description": "给本地 Agent 的决定"},
                    "wait_seconds": {"type": "number", "description": "回答后最多等多久（默认 90）"},
                },
                "required": ["task_id", "answer"],
            },
        },
        {
            "name": "local_task_cancel",
            "description": "取消一个派活任务。设备在下一个工具边界停下；已经结束的任务取消无效。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "reason": {"type": "string", "description": "取消原因，会展示给用户"},
                },
                "required": ["task_id"],
            },
        },
    ]


__all__ = ["TOOLSET", "DispatchTools", "tool_schemas"]
