"""后台维护：超时收尾、租约重投、过期清理。

为什么必须有：这三件事都是**时间驱动**的，没有调用方会主动来问。

- 设备掉线 / 桌面休眠 → 任务停在 `running`，云端那条 `local_dispatch` 会一直等；
  没有租约重投就只能等 10 分钟后的 `deadlineAt`。
- 任务 7 天后要清掉（§5.1），没人扫就永远留着。
- 超时的任务要主动给设备发 `task.cancel`，否则本地还在白干。

跑在一个 daemon 线程里，`HubServer.start()` 起来、`stop()` 结束。间隔取 15s：
比租约（120s）小一个数量级，误差可以忽略，又不会把 sqlite 打热。
"""

from __future__ import annotations

import logging
import threading

from .app import HubApp
from .frames import cancel_payload, task_payload

logger = logging.getLogger(__name__)

SWEEP_SECONDS = 15.0
PURGE_SECONDS = 600.0


class Maintenance:
    def __init__(self, app: HubApp, *, interval: float = SWEEP_SECONDS,
                 purge_interval: float = PURGE_SECONDS) -> None:
        self.app = app
        self.interval = max(1.0, float(interval))
        self.purge_interval = max(self.interval, float(purge_interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pi-hub-maintenance", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)

    # -- 一轮 ---------------------------------------------------------------

    def sweep_once(self, *, purge: bool = False) -> dict[str, int]:
        """跑一轮，返回计数（测试直接调它，不用等线程）。"""
        counts = {"expired": 0, "requeued": 0, "failed": 0, "purged": 0}
        app = self.app
        dispatch = app.dispatch
        if dispatch is None:
            return counts

        for task in dispatch.reclaim():
            if task["state"] == "queued":
                counts["requeued"] += 1
                app.relay.publish(task["deviceId"], task_payload(task))
                app.audit.log(
                    "dispatch.requeued",
                    device_id=task["deviceId"],
                    detail={"task": task["taskId"][:24], "attempt": task.get("attempt")},
                )
            else:
                counts["failed"] += 1
                app.audit.log(
                    "dispatch.lease_failed",
                    device_id=task["deviceId"],
                    detail={"task": task["taskId"][:24]},
                )

        for task_id in dispatch.expire():
            counts["expired"] += 1
            task = dispatch.task(task_id)
            if task is None:
                continue
            # 设备可能还在跑：明确告诉它停，别让本地白干到 deadline 之后
            app.relay.publish(
                task["deviceId"],
                cancel_payload(task_id, "任务超过截止时间（网关收尾）", by="gateway"),
            )
            app.audit.log(
                "dispatch.expired",
                device_id=task["deviceId"],
                detail={"task": task_id[:24]},
            )

        if purge:
            counts["purged"] = dispatch.purge()
        return counts

    def _loop(self) -> None:
        purge_due = 0.0
        while not self._stop.wait(self.interval):
            try:
                purge_due -= self.interval
                do_purge = purge_due <= 0
                if do_purge:
                    purge_due = self.purge_interval
                self.sweep_once(purge=do_purge)
            except Exception as exc:  # 维护线程不许把宿主拖下水
                logger.warning("[pi-hub] maintenance sweep failed: %s", exc)
