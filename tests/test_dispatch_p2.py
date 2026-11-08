"""P2：本地反问（input_required）、租约重投、重连接回、后台维护。

P1 的语义是「一次请求等一个结果」；这里补的是**结果可能来回几次**、
以及**设备中途消失**的三种情况：

- 设备反问 → 云端答 → 同一轮继续（D8）
- 设备断网重连 → 正在跑的任务要接得回去（D9）
- claim 之后彻底失联 → 租约到期重投，最多两次（D10）
"""

from __future__ import annotations

import json
import unittest

from hub.dispatch import LEASE_SECONDS, DispatchStore, MAX_ATTEMPTS
from hub.maintenance import Maintenance
from tests.helpers import HubTestCase

# 真实时间戳 + 一年：比任何 deadline / lease 都大，用来模拟「时间过去了很久」
FUTURE = int(__import__("time").time()) + 365 * 24 * 3600


class StoreP2Tests(unittest.TestCase):
    """store 层的迁移规则：不经过 HTTP，直接看状态机。"""

    def setUp(self) -> None:
        from hub.db import Database
        import tempfile

        self.tmp = tempfile.mkdtemp(prefix="pi-dispatch-p2-")
        self.db = Database(f"{self.tmp}/hub.db")
        self.now = 1_000_000
        self.store = DispatchStore(self.db, clock=lambda: self.now)

    def tearDown(self) -> None:
        self.db.close()

    def _task(self, **kwargs) -> dict:
        task, _ = self.store.enqueue(
            device_id=kwargs.pop("device_id", "d1"),
            workspace_id="w1",
            prompt="做点事",
            **kwargs,
        )
        return task

    def test_question_moves_task_to_input_required_and_keeps_question(self) -> None:
        task = self._task()
        self.assertTrue(self.store.claim(task["taskId"]))
        accepted, state = self.store.report(task["taskId"], kind="question", question="删哪个分支？")
        self.assertTrue(accepted)
        self.assertEqual(state, "input_required")
        self.assertEqual(self.store.task(task["taskId"])["question"], "删哪个分支？")
        # 反问不是终态：finished_at 必须还是空的
        self.assertIsNone(self.store.task(task["taskId"])["finishedAt"])

    def test_answer_returns_to_running_and_clears_question(self) -> None:
        task = self._task()
        self.store.claim(task["taskId"])
        self.store.report(task["taskId"], kind="question", question="删哪个？")
        self.assertTrue(self.store.answer(task["taskId"], "删 feature 分支"))
        row = self.store.task(task["taskId"])
        self.assertEqual(row["state"], "running")
        self.assertEqual(row["answer"], "删 feature 分支")
        self.assertIsNone(row["question"])

    def test_answer_is_rejected_when_not_waiting(self) -> None:
        task = self._task()
        self.assertFalse(self.store.answer(task["taskId"], "随便答"))

    def test_progress_does_not_clobber_input_required(self) -> None:
        task = self._task()
        self.store.claim(task["taskId"])
        self.store.report(task["taskId"], kind="question", question="选哪个？")
        self.store.report(task["taskId"], kind="progress")
        row = self.store.task(task["taskId"])
        self.assertEqual(row["state"], "input_required")
        self.assertEqual(row["question"], "选哪个？")

    def test_active_for_includes_running_and_input_required(self) -> None:
        queued = self._task()
        running = self._task(device_id="d1")
        waiting = self._task(device_id="d1")
        self.store.claim(running["taskId"])
        self.store.claim(waiting["taskId"])
        self.store.report(waiting["taskId"], kind="question", question="？")
        ids = [item["taskId"] for item in self.store.active_for("d1")]
        self.assertIn(queued["taskId"], ids)
        self.assertIn(running["taskId"], ids)
        self.assertIn(waiting["taskId"], ids)
        self.assertEqual(self.store.active_for("d2"), [])

    def test_reclaim_requeues_when_lease_expires(self) -> None:
        task = self._task()
        self.store.claim(task["taskId"])
        # 租约内不动
        self.now += LEASE_SECONDS - 1
        self.assertEqual(self.store.reclaim(), [])
        self.now += 2
        reclaimed = self.store.reclaim()
        self.assertEqual([item["taskId"] for item in reclaimed], [task["taskId"]])
        row = self.store.task(task["taskId"])
        self.assertEqual(row["state"], "queued")
        self.assertIsNone(row["claimedAt"])
        # attempt 在 claim 时已经加过，重投不再加：它是「跑过几次」的计数
        self.assertEqual(row["attempt"], 1)

    def test_reclaim_fails_after_max_attempts(self) -> None:
        task = self._task()
        for _ in range(MAX_ATTEMPTS):
            self.assertTrue(self.store.claim(task["taskId"]))
            self.now += LEASE_SECONDS + 1
            self.store.reclaim()
        reclaimed = self.store.reclaim()
        row = self.store.task(task["taskId"])
        if reclaimed:
            self.assertEqual([item["state"] for item in reclaimed], ["failed"])
        self.assertEqual(row["state"], "failed")
        self.assertIn("租约", row["error"])

    def test_reclaim_skips_tasks_that_report_progress(self) -> None:
        task = self._task()
        self.store.claim(task["taskId"])
        self.now += LEASE_SECONDS - 10
        self.store.report(task["taskId"], kind="progress")
        self.now += 20
        self.assertEqual(self.store.reclaim(), [])
        self.assertEqual(self.store.task(task["taskId"])["state"], "running")


class MaintenanceTests(HubTestCase):
    """维护线程的三件事：重投推帧、超时发取消、清理过期行。"""

    def setUp(self) -> None:
        super().setUp()
        self.token = self.pair()
        self.device_id = "device-1"
        self.lease = LEASE_SECONDS
        self.maintenance = Maintenance(self.hub.app, interval=1.0)

    def _queue(self, device_id: str = "device-1"):
        return self.hub.app.relay.subscribe(device_id)

    def test_sweep_requeues_and_republishes_task(self) -> None:
        channel = self._queue()
        task, _ = self.hub.app.dispatch.enqueue(
            device_id=self.device_id, workspace_id="w1", prompt="慢任务"
        )
        self.hub.app.dispatch.claim(task["taskId"])
        # 让租约过期
        self.hub.app.dispatch.clock = lambda: FUTURE
        counts = self.maintenance.sweep_once()
        self.assertEqual(counts["requeued"], 1)
        frame = channel.get_nowait()
        self.assertEqual(frame["type"], "task.assigned")
        self.assertEqual(frame["taskId"], task["taskId"])

    def test_sweep_cancels_expired_task_on_device(self) -> None:
        channel = self._queue()
        task, _ = self.hub.app.dispatch.enqueue(
            device_id=self.device_id, workspace_id="w1", prompt="超时任务", deadline_seconds=30
        )
        self.hub.app.dispatch.clock = lambda: FUTURE
        counts = self.maintenance.sweep_once()
        self.assertEqual(counts["expired"], 1)
        # 可能先收到重投帧：一路读到 task.cancel
        kinds = []
        for _ in range(4):
            try:
                kinds.append(channel.get_nowait()["type"])
            except Exception:
                break
        self.assertIn("task.cancel", kinds)

    def test_sweep_purges_only_when_asked(self) -> None:
        self.hub.app.dispatch.enqueue(device_id=self.device_id, workspace_id="w1", prompt="x")
        task, _ = self.hub.app.dispatch.enqueue(
            device_id=self.device_id, workspace_id="w1", prompt="y"
        )
        self.hub.app.dispatch.report(task["taskId"], kind="done", summary="ok")
        self.hub.app.dispatch.clock = lambda: FUTURE
        self.assertEqual(self.maintenance.sweep_once()["purged"], 0)
        self.assertGreaterEqual(self.maintenance.sweep_once(purge=True)["purged"], 1)


class ReconnectTests(HubTestCase):
    """D9：断网重连后设备要能接回正在跑的任务。"""

    def setUp(self) -> None:
        super().setUp()
        self.token = self.pair()
        self.device_id = "device-1"

    def _report(self, task_id: str, **body) -> tuple[int, dict]:
        status, payload, _ = self.call(
            "POST",
            "/v1/hub/relay/report",
            body={"taskId": task_id, "kind": "claimed", **body},
            token=self.token,
        )
        return status, payload

    def test_relay_replays_running_and_waiting_tasks(self) -> None:
        task, _ = self.hub.app.dispatch.enqueue(
            device_id=self.device_id, workspace_id="w1", prompt="长任务"
        )
        self.assertEqual(self._report(task["taskId"])[0], 200)
        self.hub.app.dispatch.report(task["taskId"], kind="question", question="继续吗？")

        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.hub.port, timeout=10)
        connection.request(
            "GET",
            "/v1/hub/relay/stream",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "text/event-stream"},
        )
        response = connection.getresponse()
        try:
            payloads = []
            for _ in range(6):
                line = response.readline().decode().rstrip("\n")
                if line.startswith("data: "):
                    payloads.append(json.loads(line[6:]))
                if payloads:
                    break
            self.assertEqual(payloads[0]["type"], "task.assigned")
            self.assertEqual(payloads[0]["taskId"], task["taskId"])
        finally:
            connection.close()

    def test_question_and_answer_roundtrip_over_http(self) -> None:
        task, _ = self.hub.app.dispatch.enqueue(
            device_id=self.device_id, workspace_id="w1", prompt="含糊指令"
        )
        self._report(task["taskId"])
        status, payload = self._report(
            task["taskId"], kind="question", question="要删哪个分支？"
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["state"], "input_required")
        self.assertEqual(
            self.hub.app.dispatch.task(task["taskId"])["question"], "要删哪个分支？"
        )
        # 云端回答（工具层走 store.answer + 推 task.answered）
        self.assertTrue(self.hub.app.dispatch.answer(task["taskId"], "删 feature/x"))
        self.assertEqual(self.hub.app.dispatch.task(task["taskId"])["state"], "running")

    def test_unknown_task_report_is_404(self) -> None:
        status, _ = self._report("dt_missing")
        self.assertEqual(status, 404)

    def test_audit_records_question(self) -> None:
        task, _ = self.hub.app.dispatch.enqueue(
            device_id=self.device_id, workspace_id="w1", prompt="含糊指令"
        )
        self._report(task["taskId"])
        self._report(task["taskId"], kind="question", question="？")
        events = [line["event"] for line in self.audit_lines()]
        self.assertIn("dispatch.question", events)
