"""工具层：模型看到的话、离线 / 忙 / 反问 / 取消 的处理。

工具 handler 必须**永远返回字符串**：抛异常会让整轮对话失败，
而派活失败本来就该是「换台设备再试」这种可恢复情况。
"""

from __future__ import annotations

import tempfile
import threading
import unittest

from hub.app import HubApp
from tools import DispatchTools, tool_schemas


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="pi-tools-")
        self.app = HubApp(data_dir=self.tmp, admin_key="k", rate_limit=1000)
        self.state = {"app": self.app}
        self.tools = DispatchTools(self.state)
        self.app.db.upsert_device(
            device_id="d1",
            name="devbox",
            token_hash="hash",
            token_prefix="pi_x",
            scopes="chat,dispatch",
        )
        self.app.dispatch.replace_workspaces(
            "d1",
            [
                {"id": "w1", "label": "主仓库", "description": "Electron / React"},
                {"id": "w2", "label": "脚本", "dispatchable": False},
            ],
            dispatchable=True,
        )

    def tearDown(self) -> None:
        self.app.db.close()

    # -- 目录 ---------------------------------------------------------------

    def test_workspaces_lists_online_state(self) -> None:
        text = self.tools.workspaces({})
        self.assertIn("d1/w1", text)
        self.assertIn("主仓库", text)
        # 刚注册的设备有新鲜心跳，按在线算
        self.assertIn('"online": true', text)

    def test_stale_heartbeat_counts_as_offline(self) -> None:
        self._make_stale()
        text = self.tools.workspaces({})
        self.assertIn('"online": false', text)

    def _make_stale(self) -> None:
        self.app.db.connection().execute(
            "UPDATE devices SET last_seen_at = 0 WHERE device_id = 'd1'"
        )

    def test_workspaces_when_empty_explains_how_to_enable(self) -> None:
        self.app.dispatch.drop_device("d1")
        text = self.tools.workspaces({})
        self.assertIn("没有任何可派活的工作区", text)
        self.assertIn("派活", text)

    # -- 派活 ---------------------------------------------------------------

    def test_offline_device_is_refused_without_queueing(self) -> None:
        self._make_stale()
        text = self.tools.dispatch({"workspace": "d1/w1", "prompt": "跑测试"})
        self.assertIn("离线", text)
        self.assertEqual(self.app.dispatch.list_workspaces("d1")[0]["workspaceId"], "w1")
        self.assertEqual(self.app.dispatch.queued_for("d1"), [])

    def test_online_device_gets_the_task_and_the_reply_is_rendered(self) -> None:
        channel = self.app.relay.subscribe("d1")
        result: dict = {}

        def run() -> None:
            result["text"] = self.tools.dispatch(
                {"workspace": "主仓库", "prompt": "跑测试", "wait_seconds": 5}
            )

        worker = threading.Thread(target=run)
        worker.start()
        frame = channel.get(timeout=5)
        self.assertEqual(frame["type"], "task.assigned")
        self.assertEqual(frame["prompt"], "跑测试")
        task_id = frame["taskId"]

        self.app.dispatch.claim(task_id)
        self.app.dispatch.report(
            task_id,
            kind="done",
            summary="3 个测试通过",
            files=["src/a.ts"],
            usage={"totalTokens": 42},
        )
        worker.join(timeout=5)
        text = result["text"]
        self.assertIn("completed", text)
        self.assertIn("3 个测试通过", text)
        self.assertIn("src/a.ts", text)

    def test_busy_device_is_refused(self) -> None:
        self.app.relay.subscribe("d1")
        task = self.app.dispatch.enqueue(device_id="d1", workspace_id="w1", prompt="先来的")[0]
        self.app.dispatch.claim(task["taskId"])
        text = self.tools.dispatch({"workspace": "d1/w1", "prompt": "后来的"})
        self.assertIn("正忙", text)

    def test_workspace_with_switch_off_is_refused(self) -> None:
        self.app.relay.subscribe("d1")
        text = self.tools.dispatch({"workspace": "d1/w2", "prompt": "x"})
        self.assertIn("没有打开派活开关", text)

    def test_ambiguous_name_asks_for_device_prefix(self) -> None:
        self.app.db.upsert_device(
            device_id="d2", name="other", token_hash="h2", token_prefix="pi_y", scopes="chat,dispatch"
        )
        self.app.dispatch.replace_workspaces(
            "d2", [{"id": "w1", "label": "主仓库"}], dispatchable=True
        )
        self.app.relay.subscribe("d1")
        self.app.relay.subscribe("d2")
        text = self.tools.dispatch({"workspace": "w1", "prompt": "x"})
        self.assertIn("多个同名工作区", text)

    def test_idempotent_dispatch_does_not_publish_twice(self) -> None:
        channel = self.app.relay.subscribe("d1")
        first = self.tools.dispatch(
            {"workspace": "d1/w1", "prompt": "x", "client_token": "c-1", "wait_seconds": 0}
        )
        second = self.tools.dispatch(
            {"workspace": "d1/w1", "prompt": "x", "client_token": "c-1", "wait_seconds": 0}
        )
        task_id = channel.get(timeout=5)["taskId"]
        self.assertEqual(channel.qsize(), 0)  # 第二次没有重推
        self.assertIn(task_id, first)
        self.assertIn(task_id, second)

    # -- 续查 / 回答 / 取消 --------------------------------------------------

    def test_task_get_reports_input_required_and_reply_resumes(self) -> None:
        channel = self.app.relay.subscribe("d1")
        self.tools.dispatch({"workspace": "d1/w1", "prompt": "删点东西", "wait_seconds": 0})
        task_id = channel.get(timeout=5)["taskId"]
        self.app.dispatch.claim(task_id)
        self.app.dispatch.report(task_id, kind="question", question="删 src/a.ts 还是 src/b.ts？")

        text = self.tools.task_get({"task_id": task_id})
        self.assertIn("input_required", text)
        self.assertIn("删 src/a.ts", text)
        self.assertIn("local_task_reply", text)

        reply = self.tools.task_reply(
            {"task_id": task_id, "answer": "删前者", "wait_seconds": 0}
        )
        self.assertIn("running", reply)
        frame = channel.get(timeout=5)
        self.assertEqual(frame["type"], "task.answered")
        self.assertEqual(frame["answer"], "删前者")

    def test_reply_to_a_task_not_asking_is_refused(self) -> None:
        channel = self.app.relay.subscribe("d1")
        self.tools.dispatch({"workspace": "d1/w1", "prompt": "x", "wait_seconds": 0})
        task_id = channel.get(timeout=5)["taskId"]
        text = self.tools.task_reply({"task_id": task_id, "answer": "随便"})
        self.assertIn("不需要回答", text)

    def test_cancel_pushes_a_frame_and_marks_canceled(self) -> None:
        channel = self.app.relay.subscribe("d1")
        self.tools.dispatch({"workspace": "d1/w1", "prompt": "x", "wait_seconds": 0})
        task_id = channel.get(timeout=5)["taskId"]
        self.app.dispatch.claim(task_id)
        text = self.tools.task_cancel({"task_id": task_id, "reason": "用户改主意了"})
        self.assertIn("已取消", text)
        self.assertEqual(self.app.dispatch.task(task_id)["state"], "canceled")
        frame = channel.get(timeout=5)
        self.assertEqual(frame["type"], "task.cancel")

    def test_hub_not_ready_returns_text_instead_of_raising(self) -> None:
        tools = DispatchTools({"app": None})
        for call in (
            lambda: tools.workspaces({}),
            lambda: tools.dispatch({"workspace": "d1/w1", "prompt": "x"}),
            lambda: tools.task_get({"task_id": "dt_1"}),
            lambda: tools.task_reply({"task_id": "dt_1", "answer": "x"}),
            lambda: tools.task_cancel({"task_id": "dt_1"}),
        ):
            self.assertIsInstance(call(), str)

    def test_unknown_task_is_explained(self) -> None:
        self.assertIn("没有 id 为 dt_nope", self.tools.task_get({"task_id": "dt_nope"}))

    # -- schema -------------------------------------------------------------

    def test_schemas_are_well_formed(self) -> None:
        names = set()
        for schema in tool_schemas():
            self.assertEqual(schema["name"], schema["parameters"] and schema["name"])
            self.assertTrue(schema["description"])
            self.assertEqual(schema["parameters"]["type"], "object")
            self.assertIn("properties", schema["parameters"])
            self.assertIsInstance(schema["parameters"].get("required", []), list)
            names.add(schema["name"])
        self.assertEqual(
            names,
            {
                "local_workspaces",
                "local_dispatch",
                "local_task_get",
                "local_task_reply",
                "local_task_trace",
                "local_task_cancel",
            },
        )


if __name__ == "__main__":
    unittest.main()


class TraceToolTests(unittest.TestCase):
    """`local_task_trace`：轨迹默认不推送，但云端要能事后查。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="pi-trace-tool-")
        self.app = HubApp(data_dir=self.tmp, admin_key="k", rate_limit=1000)
        self.tools = DispatchTools({"app": self.app})
        self.app.db.upsert_device(
            device_id="d1", name="devbox", token_hash="h", token_prefix="pi_x", scopes="chat,dispatch"
        )
        self.app.dispatch.replace_workspaces(
            "d1", [{"id": "w1", "label": "主仓库"}], dispatchable=True
        )

    def _finished(self, trace=None, summary="做完了") -> str:
        task, _ = self.app.dispatch.enqueue(device_id="d1", workspace_id="w1", prompt="x")
        self.app.dispatch.claim(task["taskId"])
        self.app.dispatch.report(
            task["taskId"], kind="done", summary=summary, local_session_id="s1", trace=trace
        )
        return task["taskId"]

    def test_renders_steps_with_markers_and_files(self) -> None:
        task_id = self._finished(
            trace={
                "steps": [
                    {"at": 1, "tool": "bash", "args": "npm test", "ok": True, "ms": 1200},
                    {
                        "at": 2,
                        "tool": "edit",
                        "args": "src/a.ts",
                        "ok": False,
                        "ms": 40,
                        "file": "src/a.ts",
                        "result": "写失败",
                    },
                ],
                "total": 2,
                "truncated": False,
                "ms": 5000,
            }
        )
        text = self.tools.task_trace({"task_id": task_id})
        self.assertIn("共 2 步", text)
        self.assertIn("1. [ok] bash — npm test", text)
        self.assertIn("2. [FAILED] edit", text)
        self.assertIn("src/a.ts", text)
        self.assertIn("5s", text)

    def test_paginates(self) -> None:
        steps = [
            {"at": index, "tool": "bash", "args": f"cmd{index}", "ok": True, "ms": 1}
            for index in range(10)
        ]
        task_id = self._finished(trace={"steps": steps, "total": 10, "ms": 1000})
        text = self.tools.task_trace({"task_id": task_id, "offset": 7, "limit": 5})
        self.assertIn("第 8–10 步", text)
        self.assertIn("cmd9", text)
        self.assertNotIn("cmd6", text)

    def test_explains_missing_trace(self) -> None:
        task_id = self._finished(trace=None)
        text = self.tools.task_trace({"task_id": task_id})
        self.assertIn("没有轨迹", text)

    def test_render_mentions_trace_availability(self) -> None:
        task_id = self._finished(
            trace={"steps": [{"at": 1, "tool": "bash", "args": "ls", "ok": True, "ms": 5}], "total": 1, "ms": 100}
        )
        text = self.tools.task_get({"task_id": task_id})
        self.assertIn("local_task_trace", text)
        self.assertIn(f'"{task_id}"', text)

    def test_expired_wording_does_not_suggest_another_device(self) -> None:
        task, _ = self.app.dispatch.enqueue(device_id="d1", workspace_id="w1", prompt="x")
        self.app.dispatch.claim(task["taskId"])
        # 直接把截止时间推到过去，让 sweep 走真实的 expire 路径
        self.app.db.connection().execute(
            "UPDATE dispatch_tasks SET deadline_at = 0 WHERE task_id = ?", (task["taskId"],)
        )
        self.assertEqual(self.app.dispatch.expire(), [task["taskId"]])
        text = self.tools.task_get({"task_id": task["taskId"]})
        self.assertIn("同一个工作区", text)
        self.assertNotIn("换台设备", text)
