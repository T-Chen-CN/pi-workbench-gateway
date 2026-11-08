"""派活：任务 store + 状态机 + 设备工作区目录。

纪律（与 `hub/` 其余部分一致）：纯标准库 + 本模块自己的 sqlite 读写，
**不 import Hermes**（`gateway.*` / `plugins.*`），也不知道工具层存在。
工具层与 HTTP 层都通过这里改状态，状态机只有一份。

并发模型：HTTP 线程与工具线程都会写。sqlite 已经 WAL + busy_timeout，
本模块只额外保证**状态迁移的原子性**（claim 用条件 UPDATE，回报用状态守卫），
以及在状态变化时唤醒阻塞中的等待者（工具正在等这一轮的结果）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid

from .db import Database

TASK_STATES = (
    "queued",
    "running",
    "input_required",
    "completed",
    "failed",
    "canceled",
    "expired",
)
TERMINAL_STATES = ("completed", "failed", "canceled", "expired")
# 值得把控制权交回给调用方的状态：终态，或本地正在反问
NOTABLE_STATES = TERMINAL_STATES + ("input_required",)

KIND_TO_STATE = {
    "claimed": "running",
    "progress": "running",
    "question": "input_required",
    "done": "completed",
    "failed": "failed",
    "canceled": "canceled",
}

MAX_SUMMARY_CHARS = 4000
MAX_FILE_ENTRIES = 200
MAX_EVENTS_PER_TASK = 200
TASK_RETENTION_SECONDS = 7 * 24 * 3600


def _trim(text: object, limit: int) -> str:
    value = "" if text is None else str(text)
    return value[:limit]


def _files(raw: object) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        value = str(item or "").strip()
        if not value:
            continue
        out.append(value[:400])
        if len(out) >= MAX_FILE_ENTRIES:
            break
    return out


class DispatchStore:
    def __init__(self, db: Database, *, clock=time.time) -> None:
        self.db = db
        self.clock = clock
        self._lock = threading.Lock()
        self._waiters: dict[str, threading.Event] = {}

    # -- 工作区目录 ---------------------------------------------------------

    def replace_workspaces(self, device_id: str, workspaces: list[dict], *, dispatchable: bool) -> int:
        """整表替换某台设备的工作区清单。

        心跳每次都上报全量，所以用「删了再插」保持与设备一致：
        设备上被移除 / 关掉派活的工作区必须立刻从目录里消失，不能留残影。
        """
        now = int(self.clock())
        conn = self.db.connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM device_workspaces WHERE device_id = ?", (device_id,))
            inserted = 0
            for item in workspaces or []:
                if not isinstance(item, dict):
                    continue
                workspace_id = _trim(item.get("id"), 120).strip()
                if not workspace_id:
                    continue
                label = _trim(item.get("label"), 120).strip() or workspace_id
                description = _trim(item.get("description"), 400).strip()
                flag = bool(item.get("dispatchable", True)) and dispatchable
                conn.execute(
                    "INSERT OR REPLACE INTO device_workspaces "
                    "(device_id, workspace_id, label, description, dispatchable, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (device_id, workspace_id, label, description, 1 if flag else 0, now),
                )
                inserted += 1
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return inserted

    def list_workspaces(self, device_id: str | None = None) -> list[dict]:
        conn = self.db.connection()
        if device_id:
            rows = conn.execute(
                "SELECT * FROM device_workspaces WHERE device_id = ? ORDER BY label",
                (device_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM device_workspaces ORDER BY device_id, label"
            ).fetchall()
        return [self._workspace_row(row) for row in rows]

    def workspace(self, device_id: str, workspace_id: str) -> dict | None:
        row = self.db.connection().execute(
            "SELECT * FROM device_workspaces WHERE device_id = ? AND workspace_id = ?",
            (device_id, workspace_id),
        ).fetchone()
        return self._workspace_row(row) if row else None

    @staticmethod
    def _workspace_row(row) -> dict:
        return {
            "deviceId": str(row["device_id"]),
            "workspaceId": str(row["workspace_id"]),
            "label": str(row["label"]),
            "description": str(row["description"] or ""),
            "dispatchable": bool(row["dispatchable"]),
            "updatedAt": int(row["updated_at"]),
        }

    def drop_device(self, device_id: str) -> None:
        """设备被撤销时，它的工作区立刻从目录消失。"""
        self.db.connection().execute(
            "DELETE FROM device_workspaces WHERE device_id = ?", (device_id,)
        )

    # -- 任务 ---------------------------------------------------------------

    def enqueue(
        self,
        *,
        device_id: str,
        workspace_id: str,
        prompt: str,
        context_id: str = "",
        client_token: str = "",
        deadline_seconds: int = 600,
    ) -> tuple[dict, bool]:
        """入队。返回 `(task, created)`；`created=False` 表示幂等命中已有任务。"""
        token = _trim(client_token, 200).strip() or f"auto-{uuid.uuid4().hex}"
        now = int(self.clock())
        conn = self.db.connection()
        existing = conn.execute(
            "SELECT * FROM dispatch_tasks WHERE client_token = ?", (token,)
        ).fetchone()
        if existing is not None:
            return self._task_row(existing), False

        task_id = f"dt_{uuid.uuid4().hex[:16]}"
        conn.execute(
            "INSERT INTO dispatch_tasks "
            "(task_id, client_token, context_id, device_id, workspace_id, prompt, state, "
            " attempt, created_at, updated_at, deadline_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?)",
            (
                task_id,
                token,
                _trim(context_id, 200),
                device_id,
                workspace_id,
                _trim(prompt, 20000),
                now,
                now,
                now + max(30, int(deadline_seconds)),
            ),
        )
        self._event(task_id, "task.queued", {"deviceId": device_id, "workspaceId": workspace_id})
        return self.task(task_id) or {}, True

    def task(self, task_id: str) -> dict | None:
        row = self.db.connection().execute(
            "SELECT * FROM dispatch_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return self._task_row(row) if row else None

    def queued_for(self, device_id: str) -> list[dict]:
        rows = self.db.connection().execute(
            "SELECT * FROM dispatch_tasks WHERE device_id = ? AND state = 'queued' "
            "ORDER BY created_at",
            (device_id,),
        ).fetchall()
        return [self._task_row(row) for row in rows]

    def claim(self, task_id: str) -> bool:
        """原子领取：只有 `queued` 能被领走，两台设备抢同一个任务时只有一个成功。"""
        now = int(self.clock())
        cur = self.db.connection().execute(
            "UPDATE dispatch_tasks SET state = 'running', claimed_at = ?, started_at = ?, "
            "attempt = attempt + 1 WHERE task_id = ? AND state = 'queued'",
            (now, now, task_id),
        )
        if cur.rowcount != 1:
            return False
        self._event(task_id, "task.claimed", {})
        self._wake(task_id)
        return True

    def report(
        self,
        task_id: str,
        *,
        kind: str,
        summary: object = None,
        files: object = None,
        usage: object = None,
        error: object = None,
        question: object = None,
        local_session_id: object = None,
    ) -> tuple[bool, str]:
        """设备回报。返回 `(accepted, state)`。

        终态不可逆：重复 / 迟到的回报只记事件，不改状态 —— 否则一次网络重试
        就能把 `completed` 改回 `running`，云端会永远等下去。
        """
        target = KIND_TO_STATE.get(kind)
        if target is None:
            return False, ""
        now = int(self.clock())
        conn = self.db.connection()
        current = conn.execute(
            "SELECT state, attempt FROM dispatch_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if current is None:
            return False, ""
        state = str(current["state"])
        if state in TERMINAL_STATES:
            self._event(task_id, f"task.ignored.{kind}", {"state": state})
            return False, state
        if kind == "progress":
            # 进度只刷新时间戳：既不能把 `input_required` 打回 `running`（会丢掉问题），
            # 也不应该唤醒等待方
            conn.execute(
                "UPDATE dispatch_tasks SET updated_at = ? WHERE task_id = ?", (now, task_id)
            )
            return True, state

        fields: list[str] = ["state = ?", "updated_at = ?"]
        params: list[object] = [target, now]
        if kind in ("done", "failed", "question"):
            fields.append("finished_at = ?")
            params.append(now if kind != "question" else None)
        if summary is not None:
            fields.append("summary = ?")
            params.append(_trim(summary, MAX_SUMMARY_CHARS))
        if files is not None:
            fields.append("files_changed = ?")
            params.append(json.dumps(_files(files), ensure_ascii=False))
        if usage is not None:
            fields.append("usage = ?")
            params.append(json.dumps(usage, ensure_ascii=False)[:2000])
        if error is not None:
            fields.append("error = ?")
            params.append(_trim(error, 2000))
        if question is not None:
            fields.append("question = ?")
            params.append(_trim(question, 2000))
        if local_session_id is not None:
            fields.append("local_session_id = ?")
            params.append(_trim(local_session_id, 200))
        params.append(task_id)
        conn.execute(f"UPDATE dispatch_tasks SET {', '.join(fields)} WHERE task_id = ?", params)
        self._event(task_id, f"task.{kind}", {"state": target})
        self._wake(task_id)
        return True, target

    def answer(self, task_id: str, answer: object) -> bool:
        """云端回答本地反问：任务回到 `running`，本地同一轮继续。"""
        now = int(self.clock())
        cur = self.db.connection().execute(
            "UPDATE dispatch_tasks SET state = 'running', answer = ?, question = NULL, "
            "updated_at = ? WHERE task_id = ? AND state = 'input_required'",
            (_trim(answer, 4000), now, task_id),
        )
        if cur.rowcount != 1:
            return False
        self._event(task_id, "task.answered", {})
        self._wake(task_id)
        return True

    def cancel(self, task_id: str, *, reason: str = "", by: str = "cloud") -> dict | None:
        """取消：终态任务不动，其它一律变 `canceled`。返回取消后的任务（未改动则 None）。"""
        now = int(self.clock())
        row = self.db.connection().execute(
            "SELECT state FROM dispatch_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None or str(row["state"]) in TERMINAL_STATES:
            return None
        self.db.connection().execute(
            "UPDATE dispatch_tasks SET state = 'canceled', error = ?, updated_at = ?, "
            "finished_at = ? WHERE task_id = ?",
            (_trim(reason, 2000), now, now, task_id),
        )
        self._event(task_id, "task.canceled", {"by": by})
        self._wake(task_id)
        return self.task(task_id)

    def running_count(self, device_id: str) -> int:
        row = self.db.connection().execute(
            "SELECT COUNT(*) AS n FROM dispatch_tasks WHERE device_id = ? "
            "AND state IN ('running', 'input_required')",
            (device_id,),
        ).fetchone()
        return int(row["n"] if row else 0)

    def expire(self, now: int | None = None) -> list[str]:
        """超时收尾：返回被置为 `expired` 的任务 id 列表（调用方负责通知设备）。"""
        at = int(now if now is not None else self.clock())
        rows = self.db.connection().execute(
            "SELECT task_id FROM dispatch_tasks WHERE state IN ('queued', 'running', 'input_required') "
            "AND deadline_at <= ?",
            (at,),
        ).fetchall()
        expired: list[str] = []
        for row in rows:
            task_id = str(row["task_id"])
            cur = self.db.connection().execute(
                "UPDATE dispatch_tasks SET state = 'expired', finished_at = ?, updated_at = ? "
                "WHERE task_id = ? AND state IN ('queued', 'running', 'input_required')",
                (at, at, task_id),
            )
            if cur.rowcount == 1:
                self._event(task_id, "task.expired", {})
                self._wake(task_id)
                expired.append(task_id)
        if expired:
            self._event("", "dispatch.swept", {"count": len(expired)})
        return expired

    def purge(self, now: int | None = None) -> int:
        """清理超过保留期的终态任务（审计行不动）。"""
        at = int(now if now is not None else self.clock())
        cutoff = at - TASK_RETENTION_SECONDS
        conn = self.db.connection()
        cur = conn.execute(
            "DELETE FROM dispatch_tasks WHERE state IN ('completed', 'failed', 'canceled', 'expired') "
            "AND updated_at <= ?",
            (cutoff,),
        )
        removed = cur.rowcount or 0
        if removed:
            conn.execute(
                "DELETE FROM dispatch_events WHERE task_id NOT IN (SELECT task_id FROM dispatch_tasks)"
            )
        return removed

    def events(self, task_id: str, *, limit: int = 50) -> list[dict]:
        rows = self.db.connection().execute(
            "SELECT * FROM dispatch_events WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, max(1, min(int(limit), 200))),
        ).fetchall()
        return [
            {
                "at": int(row["at"]),
                "kind": str(row["kind"]),
                "payload": json.loads(row["payload"]) if row["payload"] else None,
            }
            for row in reversed(rows)
        ]

    # -- 等待 ---------------------------------------------------------------

    def wait(self, task_id: str, timeout: float) -> dict | None:
        """阻塞到「值得回到调用方」的状态（终态或反问），或超时。

        超时不是错误：调用方拿到当前状态后自己决定是继续等、轮询还是收尾。
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            task = self.task(task_id)
            if task is None or task["state"] in NOTABLE_STATES:
                return task
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return task
            event = self._waiter(task_id)
            event.wait(min(remaining, 1.0))
            event.clear()

    def _waiter(self, task_id: str) -> threading.Event:
        with self._lock:
            event = self._waiters.get(task_id)
            if event is None:
                event = threading.Event()
                self._waiters[task_id] = event
            return event

    def _wake(self, task_id: str) -> None:
        with self._lock:
            event = self._waiters.get(task_id)
        if event is not None:
            event.set()

    # -- 内部 ---------------------------------------------------------------

    def _event(self, task_id: str, kind: str, payload: dict) -> None:
        conn = self.db.connection()
        conn.execute(
            "INSERT INTO dispatch_events (task_id, at, kind, payload) VALUES (?, ?, ?, ?)",
            (task_id, int(self.clock()), kind, json.dumps(payload, ensure_ascii=False)),
        )
        if task_id:
            conn.execute(
                "DELETE FROM dispatch_events WHERE task_id = ? AND id NOT IN ("
                "SELECT id FROM dispatch_events WHERE task_id = ? ORDER BY id DESC LIMIT ?)",
                (task_id, task_id, MAX_EVENTS_PER_TASK),
            )

    @staticmethod
    def _task_row(row) -> dict:
        return {
            "taskId": str(row["task_id"]),
            "clientToken": str(row["client_token"]),
            "contextId": str(row["context_id"] or ""),
            "deviceId": str(row["device_id"]),
            "workspaceId": str(row["workspace_id"]),
            "prompt": str(row["prompt"]),
            "state": str(row["state"]),
            "question": row["question"],
            "answer": row["answer"],
            "summary": row["summary"],
            "filesChanged": json.loads(row["files_changed"]) if row["files_changed"] else [],
            "usage": json.loads(row["usage"]) if row["usage"] else None,
            "error": row["error"],
            "localSessionId": row["local_session_id"],
            "attempt": int(row["attempt"] or 0),
            "createdAt": int(row["created_at"]),
            "deadlineAt": int(row["deadline_at"]),
            "claimedAt": row["claimed_at"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
        }
