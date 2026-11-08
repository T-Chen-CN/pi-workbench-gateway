"""派活：任务 store 的状态机、幂等、原子领取、等待与清理。"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest

from hub.db import Database
from hub.dispatch import DispatchStore


class DispatchStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="pi-dispatch-")
        self.db = Database(f"{self.tmp}/hub.db")
        self.store = DispatchStore(self.db)

    def tearDown(self) -> None:
        self.db.close()

    # -- 目录 ---------------------------------------------------------------

    def test_workspaces_are_replaced_not_merged(self) -> None:
        self.store.replace_workspaces(
            "d1",
            [{"id": "w1", "label": "主仓库"}, {"id": "w2", "label": "另一个"}],
            dispatchable=True,
        )
        self.assertEqual(len(self.store.list_workspaces("d1")), 2)
        # 心跳上报的是全量：少报一个，目录里就必须少一个
        self.store.replace_workspaces("d1", [{"id": "w1", "label": "主仓库"}], dispatchable=True)
        items = self.store.list_workspaces("d1")
        self.assertEqual([i["workspaceId"] for i in items], ["w1"])

    def test_global_switch_turns_every_workspace_off(self) -> None:
        self.store.replace_workspaces("d1", [{"id": "w1", "label": "x"}], dispatchable=False)
        self.assertFalse(self.store.workspace("d1", "w1")["dispatchable"])

    def test_drop_device_removes_catalog(self) -> None:
        self.store.replace_workspaces("d1", [{"id": "w1", "label": "x"}], dispatchable=True)
        self.store.drop_device("d1")
        self.assertEqual(self.store.list_workspaces("d1"), [])

    # -- 幂等 ---------------------------------------------------------------

    def test_same_client_token_returns_the_same_task(self) -> None:
        first, created_first = self.store.enqueue(
            device_id="d1", workspace_id="w1", prompt="跑测试", client_token="ctx:1"
        )
        second, created_second = self.store.enqueue(
            device_id="d1", workspace_id="w1", prompt="跑测试", client_token="ctx:1"
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first["taskId"], second["taskId"])

    def test_missing_token_still_creates_distinct_tasks(self) -> None:
        a, _ = self.store.enqueue(device_id="d1", workspace_id="w1", prompt="a")
        b, _ = self.store.enqueue(device_id="d1", workspace_id="w1", prompt="b")
        self.assertNotEqual(a["taskId"], b["taskId"])

    # -- 状态机 -------------------------------------------------------------

    def test_claim_is_atomic(self) -> None:
        task, _ = self.store.enqueue(device_id="d1", workspace_id="w1", prompt="x")
        results: list[bool] = []

        def worker() -> None:
            results.append(self.store.claim(task["taskId"]))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.store.task(task["taskId"])["state"], "running")

    def test_terminal_state_is_final(self) -> None:
        task, _ = self.store.enqueue(device_id="d1", workspace_id="w1", prompt="x")
        self.store.claim(task["taskId"])
        self.assertTrue(self.store.report(task["taskId"], kind="done", summary="好了")[0])
        accepted, state = self.store.report(task["taskId"], kind="failed", error="迟到的失败")
        self.assertFalse(accepted)
        self.assertEqual(state, "completed")
        self.assertEqual(self.store.task(task["taskId"])["summary"], "好了")

    def test_progress_does_not_clobber_input_required(self) -> None:
        task, _ = self.store.enqueue(device_id="d1", workspace_id="w1", prompt="x")
        self.store.claim(task["taskId"])
        self.store.report(task["taskId"], kind="question", question="删哪个？")
        self.store.report(task["taskId"], kind="progress")
        current = self.store.task(task["taskId"])
        self.assertEqual(current["state"], "input_required")
        self.assertEqual(current["question"], "删哪个？")

    def test_answer_resumes_only_from_input_required(self) -> None:
        task, _ = self.store.enqueue(device_id="d1", workspace_id="w1", prompt="x")
        self.store.claim(task["taskId"])
        self.assertFalse(self.store.answer(task["taskId"], "太早了"))
        self.store.report(task["taskId"], kind="question", question="删哪个？")
        self.assertTrue(self.store.answer(task["taskId"], "删 tmp"))
        current = self.store.task(task["taskId"])
        self.assertEqual(current["state"], "running")
        self.assertEqual(current["answer"], "删 tmp")
        self.assertIsNone(current["question"])

    def test_done_carries_summary_files_and_usage(self) -> None:
        task, _ = self.store.enqueue(device_id="d1", workspace_id="w1", prompt="x")
        self.store.claim(task["taskId"])
        self.store.report(
            task["taskId"],
            kind="done",
            summary="改完了",
            files=["src/a.ts", "src/b.ts"],
            usage={"totalTokens": 1234},
            local_session_id="s-1",
        )
        current = self.store.task(task["taskId"])
        self.assertEqual(current["state"], "completed")
        self.assertEqual(current["filesChanged"], ["src/a.ts", "src/b.ts"])
        self.assertEqual(current["usage"], {"totalTokens": 1234})
        self.assertEqual(current["localSessionId"], "s-1")

    def test_summary_and_file_list_are_bounded(self) -> None:
        task, _ = self.store.enqueue(device_id="d1", workspace_id="w1", prompt="x")
        self.store.claim(task["taskId"])
        self.store.report(
            task["taskId"],
            kind="done",
            summary="x" * 9000,
            files=[f"f{i}.ts" for i in range(500)],
        )
        current = self.store.task(task["taskId"])
        self.assertEqual(len(current["summary"]), 4000)
        self.assertEqual(len(current["filesChanged"]), 200)

    def test_cancel_only_touches_live_tasks(self) -> None:
        task, _ = self.store.enqueue(device_id="d1", workspace_id="w1", prompt="x")
        canceled = self.store.cancel(task["taskId"], reason="用户叫停")
        self.assertEqual(canceled["state"], "canceled")
        self.assertEqual(canceled["error"], "用户叫停")
        self.assertIsNone(self.store.cancel(task["taskId"], reason="再来一次"))

    def test_expire_marks_overdue_tasks(self) -> None:
        task, _ = self.store.enqueue(
            device_id="d1", workspace_id="w1", prompt="x", deadline_seconds=30
        )
        self.assertEqual(self.store.expire(int(time.time())), [])
        self.assertEqual(self.store.expire(task["deadlineAt"] + 1), [task["taskId"]])
        self.assertEqual(self.store.task(task["taskId"])["state"], "expired")

    def test_purge_keeps_live_tasks(self) -> None:
        done, _ = self.store.enqueue(device_id="d1", workspace_id="w1", prompt="done")
        self.store.claim(done["taskId"])
        self.store.report(done["taskId"], kind="done", summary="ok")
        live, _ = self.store.enqueue(device_id="d1", workspace_id="w1", prompt="live")
        removed = self.store.purge(int(time.time()) + 8 * 24 * 3600)
        self.assertEqual(removed, 1)
        self.assertIsNotNone(self.store.task(live["taskId"]))
        self.assertIsNone(self.store.task(done["taskId"]))

    # -- 等待 ---------------------------------------------------------------

    def test_wait_returns_when_the_device_reports(self) -> None:
        task, _ = self.store.enqueue(device_id="d1", workspace_id="w1", prompt="x")
        timer = threading.Timer(
            0.05, lambda: self.store.report(task["taskId"], kind="done", summary="好了")
        )
        timer.start()
        started = time.monotonic()
        result = self.store.wait(task["taskId"], timeout=5)
        timer.cancel()
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(result["state"], "completed")

    def test_wait_returns_input_required_immediately(self) -> None:
        task, _ = self.store.enqueue(device_id="d1", workspace_id="w1", prompt="x")
        self.store.claim(task["taskId"])
        threading.Timer(
            0.05, lambda: self.store.report(task["taskId"], kind="question", question="选哪个")
        ).start()
        result = self.store.wait(task["taskId"], timeout=5)
        self.assertEqual(result["state"], "input_required")
        self.assertEqual(result["question"], "选哪个")

    def test_wait_times_out_without_changing_state(self) -> None:
        task, _ = self.store.enqueue(device_id="d1", workspace_id="w1", prompt="x")
        result = self.store.wait(task["taskId"], timeout=0.1)
        self.assertEqual(result["state"], "queued")

    # -- 事件 ---------------------------------------------------------------

    def test_events_are_bounded_per_task(self) -> None:
        task, _ = self.store.enqueue(device_id="d1", workspace_id="w1", prompt="x")
        for _ in range(260):
            self.store.report(task["taskId"], kind="progress")
        self.assertLessEqual(len(self.store.events(task["taskId"], limit=500)), 200)


if __name__ == "__main__":
    unittest.main()
