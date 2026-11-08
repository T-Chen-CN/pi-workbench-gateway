"""P3：对外 A2A 端点（Agent Card + JSON-RPC）。

对外只做**信封**：`message/send` / `tasks/get` / `tasks/cancel` /
`tasks/resubscribe` 都映射到同一个 `DispatchStore`，状态机不复制一份。
"""

from __future__ import annotations

import json
import unittest

from tests.helpers import ADMIN_KEY, HubTestCase


class AgentCardTests(HubTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.token = self.pair()
        status, _, _ = self.call(
            "POST",
            "/v1/hub/workspaces",
            body={
                "dispatchable": True,
                "workspaces": [
                    {"id": "w1", "label": "主仓库", "description": "触发了 Electron / React"},
                    {"id": "w2", "label": "另一个", "description": ""},
                ],
            },
            token=self.token,
        )
        self.assertEqual(status, 200)

    def test_card_requires_auth(self) -> None:
        status, _, _ = self.call("GET", "/.well-known/agent-card.json")
        self.assertEqual(status, 401)

    def test_card_projects_dispatchable_workspaces_into_skills(self) -> None:
        status, payload, _ = self.call(
            "GET", "/.well-known/agent-card.json", admin=ADMIN_KEY
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["protocolVersion"], "1.0")
        self.assertTrue(payload["capabilities"]["streaming"])
        skills = {item["id"]: item for item in payload["skills"]}
        self.assertIn("device-1/w1", skills)
        self.assertEqual(skills["device-1/w1"]["name"], "device-1 / 主仓库")
        # description 必须保留用户写的触发词
        self.assertIn("Electron", skills["device-1/w1"]["description"])

    def test_card_hides_workspaces_when_dispatch_disabled(self) -> None:
        self.call(
            "POST",
            "/v1/hub/workspaces",
            body={"dispatchable": False, "workspaces": [{"id": "w1", "label": "主仓库"}]},
            token=self.token,
        )
        status, payload, _ = self.call("GET", "/.well-known/agent-card.json", admin=ADMIN_KEY)
        self.assertEqual(status, 200)
        self.assertEqual(payload["skills"], [])


class A2ARpcTests(HubTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.token = self.pair()
        self.call(
            "POST",
            "/v1/hub/workspaces",
            body={"dispatchable": True, "workspaces": [{"id": "w1", "label": "主仓库"}]},
            token=self.token,
        )

    def rpc(self, method: str, params: dict, *, admin: str | None = ADMIN_KEY):
        return self.call(
            "POST",
            "/a2a",
            body={"jsonrpc": "2.0", "id": "req-1", "method": method, "params": params},
            admin=admin,
        )

    def send(self, *, blocking: bool = False, message_id: str = "m-1", skill: str = "device-1/w1"):
        return self.rpc(
            "message/send",
            {
                "message": {
                    "role": "user",
                    "messageId": message_id,
                    "parts": [{"kind": "text", "text": "把 README 的测试命令跑一遍"}],
                },
                "skill": skill,
                "configuration": {"blocking": blocking, "waitSeconds": 0.2},
            },
        )

    def test_message_send_returns_task_and_queues_it(self) -> None:
        status, payload, _ = self.send()
        self.assertEqual(status, 200, payload)
        task = payload["result"]
        self.assertEqual(task["kind"], "task")
        self.assertEqual(task["status"]["state"], "submitted")
        self.assertEqual(task["metadata"]["workspaceLabel"], "主仓库")
        stored = self.hub.app.dispatch.task(task["id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored["state"], "queued")

    def test_message_send_is_idempotent_by_message_id(self) -> None:
        _, first, _ = self.send(message_id="m-same")
        _, second, _ = self.send(message_id="m-same")
        self.assertEqual(first["result"]["id"], second["result"]["id"])

    def test_message_send_rejects_unknown_skill_with_available_list(self) -> None:
        status, payload, _ = self.rpc(
            "message/send",
            {
                "message": {
                    "role": "user",
                    "messageId": "m-2",
                    "parts": [{"kind": "text", "text": "x"}],
                },
                "skill": "device-1/nope",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], -32602)
        self.assertIn("device-1/w1", payload["error"]["message"])

    def test_blocking_send_waits_for_completion(self) -> None:
        self.call(
            "POST",
            "/v1/hub/relay/report",
            body={"taskId": "", "kind": "claimed"},
            token=self.token,
        )
        # 先排一条任务，再由设备侧回报完成；blocking 的调用应当拿到 completed
        _, queued, _ = self.send(message_id="m-block")
        task_id = queued["result"]["id"]

        import threading
        import time

        def finish() -> None:
            time.sleep(0.15)
            self.hub.app.dispatch.claim(task_id)
            self.hub.app.dispatch.report(task_id, kind="done", summary="跑完了")

        threading.Thread(target=finish, daemon=True).start()
        status, payload, _ = self.send(blocking=True, message_id="m-block")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"]["status"]["state"], "completed")
        self.assertEqual(payload["result"]["artifacts"][0]["parts"][0]["text"], "跑完了")

    def test_tasks_get_and_cancel(self) -> None:
        _, payload, _ = self.send(message_id="m-3")
        task_id = payload["result"]["id"]
        status, got, _ = self.rpc("tasks/get", {"id": task_id})
        self.assertEqual(status, 200)
        self.assertEqual(got["result"]["id"], task_id)

        status, canceled, _ = self.rpc("tasks/cancel", {"id": task_id, "reason": "不要了"})
        self.assertEqual(status, 200, canceled)
        self.assertEqual(canceled["result"]["status"]["state"], "canceled")
        # 终态不可逆：再取消一次仍返回 canceled，不报错
        _, again, _ = self.rpc("tasks/cancel", {"id": task_id})
        self.assertEqual(again["result"]["status"]["state"], "canceled")

    def test_input_required_projects_question_as_status_message(self) -> None:
        _, payload, _ = self.send(message_id="m-4")
        task_id = payload["result"]["id"]
        self.hub.app.dispatch.claim(task_id)
        self.hub.app.dispatch.report(task_id, kind="question", question="删哪个分支？")
        _, got, _ = self.rpc("tasks/get", {"id": task_id})
        self.assertEqual(got["result"]["status"]["state"], "input-required")
        self.assertEqual(
            got["result"]["status"]["message"]["parts"][0]["text"], "删哪个分支？"
        )

    def test_unknown_method_and_bad_body(self) -> None:
        _, payload, _ = self.rpc("nope/nope", {})
        self.assertEqual(payload["error"]["code"], -32601)
        # 空 body / 非 JSON：一律回 -32700，不抛 500
        status, payload, _ = self.call(
            "POST", "/a2a", body=None, admin=ADMIN_KEY, headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertIn(payload["error"]["code"], (-32700, -32600))
        status, payload, _ = self.call(
            "POST",
            "/a2a",
            body=["not", "an", "object"],
            admin=ADMIN_KEY,
        )
        self.assertEqual(payload["error"]["code"], -32600)

    def test_device_token_without_dispatch_scope_is_rejected(self) -> None:
        # 用管理密钥以外的身份：这里再造一个只有 chat scope 的设备
        code = self.make_pairing_code()
        status, payload, _ = self.call(
            "POST",
            "/v1/devices/pair",
            body={"code": code, "deviceId": "chat-only", "name": "c"},
        )
        self.assertEqual(status, 201)
        token = payload["token"]
        self.hub.app.db.connection().execute(
            "UPDATE devices SET scopes = ? WHERE device_id = ?",
            ("chat", "chat-only"),
        )
        self.hub.app.db.connection().commit()
        status, _, _ = self.call(
            "POST",
            "/a2a",
            body={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "x"}},
            token=token,
        )
        self.assertEqual(status, 403)
