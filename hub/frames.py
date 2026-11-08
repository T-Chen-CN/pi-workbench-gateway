"""中继帧：既提供「载荷字典」（进程内 `relay.publish` 用），也提供 SSE 编码。

两个调用方容易搞混，所以在这里一次说清：

- **载荷字典**（`task_payload` / `cancel_payload` / `answered_payload`）：给
  `Relay.publish` 用，由 SSE 线程统一编码。
- **SSE 字节**（`sse` / `task_frame` / `reconnect_frame`）：直接写进响应流时用。

多一条路径就要多一份格式，所以两者都在这个模块里，别的文件不许自己拼。
"""

from __future__ import annotations

import json


def sse(event: str, payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n".encode("utf-8")


def task_payload(task: dict) -> dict:
    """派活载荷。

    `attempt` 是**这次投递是第几次执行**（1 起）：库里存的是「已经被 claim 过几次」，
    入队时是 0、重投后是 1，所以这里 +1 才对得上设备看到的东西。
    """
    return {
        "type": "task.assigned",
        "taskId": task["taskId"],
        "prompt": task["prompt"],
        "deviceId": task["deviceId"],
        "workspaceId": task["workspaceId"],
        "contextId": task["contextId"],
        "deadlineAt": task["deadlineAt"],
        "attempt": int(task.get("attempt") or 0) + 1,
    }


def cancel_payload(task_id: str, reason: str, *, by: str = "cloud") -> dict:
    return {"type": "task.cancel", "taskId": task_id, "reason": reason, "by": by}


def answered_payload(task_id: str, answer: str) -> dict:
    return {"type": "task.answered", "taskId": task_id, "answer": answer}


def task_frame(task: dict) -> bytes:
    return sse("task.assigned", task_payload(task))


def reconnect_frame() -> bytes:
    return sse("relay.reconnect", {"type": "relay.reconnect"})
