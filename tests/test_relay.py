"""中继与回报：SSE 推帧、回报改状态、目录上报、撤销即下线。

这里断言的是**端到端行为**：设备挂在 SSE 上，云端入库一条任务，
设备必须真的收到 `task.assigned`；回报之后 store 的状态必须变。
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import unittest

from tests.helpers import ADMIN_KEY, HubTestCase


class RelayTests(HubTestCase):
    def _open_relay(self, token: str) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        connection = http.client.HTTPConnection("127.0.0.1", self.hub.port, timeout=10)
        connection.request(
            "GET",
            "/v1/hub/relay/stream",
            headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        )
        return connection, connection.getresponse()

    def _read_frame(self, response, seconds: float = 5.0) -> dict:
        deadline = time.time() + seconds
        seen_data = False
        payload = ""
        while time.time() < deadline:
            line = response.readline()
            if not line:
                break
            text = line.decode().rstrip("\n")
            if text.startswith("data: "):
                payload = text[6:]
                seen_data = True
            elif text == "" and seen_data:
                return json.loads(payload)
        raise AssertionError("no frame arrived in time")

    # -- 推送 ---------------------------------------------------------------

    def test_task_is_pushed_to_the_waiting_device(self) -> None:
        token = self.pair(device_id="d-relay")
        connection, response = self._open_relay(token)
        try:
            self.assertEqual(response.status, 200)
            headers = {k.lower(): v for k, v in response.getheaders()}
            self.assertIn("text/event-stream", headers["content-type"])
            self.assertEqual(headers["x-accel-buffering"], "no")
            self.assertNotIn("content-length", headers)

            # 连上之后云端才派活：必须靠推送而不是靠连接时的快照
            task = self.hub.app.dispatch.enqueue(
                device_id="d-relay", workspace_id="w1", prompt="跑一下测试"
            )[0]
            self.hub.app.relay.publish("d-relay", {"type": "task.assigned", "taskId": task["taskId"]})
            frame = self._read_frame(response)
            self.assertEqual(frame["type"], "task.assigned")
            self.assertEqual(frame["taskId"], task["taskId"])
        finally:
            connection.close()

    def test_queued_tasks_are_replayed_on_connect(self) -> None:
        token = self.pair(device_id="d-replay")
        task = self.hub.app.dispatch.enqueue(
            device_id="d-replay", workspace_id="w1", prompt="断线期间派的活"
        )[0]
        connection, response = self._open_relay(token)
        try:
            frame = self._read_frame(response)
            self.assertEqual(frame["taskId"], task["taskId"])
        finally:
            connection.close()

    def test_relay_requires_dispatch_scope(self) -> None:
        token = self.pair(device_id="d-chat-only")
        # 直接把 scope 降级，模拟只拿到 chat 的设备
        self.hub.app.db.connection().execute(
            "UPDATE devices SET scopes = 'chat' WHERE device_id = 'd-chat-only'"
        )
        connection, response = self._open_relay(token)
        try:
            self.assertEqual(response.status, 403)
        finally:
            connection.close()

    # -- 回报 ---------------------------------------------------------------

    def test_report_advances_the_task(self) -> None:
        token = self.pair(device_id="d-report")
        task = self.hub.app.dispatch.enqueue(
            device_id="d-report", workspace_id="w1", prompt="x"
        )[0]
        status, payload, _ = self.call(
            "POST",
            "/v1/hub/relay/report",
            token=token,
            body={"taskId": task["taskId"], "kind": "claimed"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["accepted"])
        self.assertEqual(self.hub.app.dispatch.task(task["taskId"])["state"], "running")

        status, payload, _ = self.call(
            "POST",
            "/v1/hub/relay/report",
            token=token,
            body={
                "taskId": task["taskId"],
                "kind": "done",
                "summary": "改了 2 个文件",
                "files": ["a.ts"],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "completed")
        self.assertEqual(self.hub.app.dispatch.task(task["taskId"])["filesChanged"], ["a.ts"])

    def test_report_rejects_unknown_kind(self) -> None:
        token = self.pair(device_id="d-bad")
        task = self.hub.app.dispatch.enqueue(device_id="d-bad", workspace_id="w1", prompt="x")[0]
        status, payload, _ = self.call(
            "POST",
            "/v1/hub/relay/report",
            token=token,
            body={"taskId": task["taskId"], "kind": "sabotage"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_params")

    def test_other_devices_task_is_invisible(self) -> None:
        owner = self.pair(device_id="d-owner")
        thief = self.pair(device_id="d-thief")
        task = self.hub.app.dispatch.enqueue(device_id="d-owner", workspace_id="w1", prompt="x")[0]
        status, payload, _ = self.call(
            "POST",
            "/v1/hub/relay/report",
            token=thief,
            body={"taskId": task["taskId"], "kind": "done", "summary": "我抢到了"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertEqual(self.hub.app.dispatch.task(task["taskId"])["state"], "queued")
        self.assertTrue(owner)

    # -- 目录 ---------------------------------------------------------------

    def test_workspace_catalog_roundtrip(self) -> None:
        token = self.pair(device_id="d-cat")
        status, payload, _ = self.call(
            "POST",
            "/v1/hub/workspaces",
            token=token,
            body={
                "dispatchable": True,
                "workspaces": [
                    {"id": "w1", "label": "主仓库", "description": "前端 / Electron"},
                    {"id": "w2", "label": "脚本"},
                ],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["workspaces"], 2)

        status, payload, _ = self.call("GET", "/v1/hub/dispatch/workspaces", token=token)
        self.assertEqual(status, 200)
        entries = {item["workspaceId"]: item for item in payload["workspaces"]}
        self.assertEqual(entries["w1"]["label"], "主仓库")
        self.assertTrue(entries["w1"]["dispatchable"])
        self.assertTrue(entries["w1"]["online"])

    def test_global_off_hides_dispatchability(self) -> None:
        token = self.pair(device_id="d-off")
        self.call(
            "POST",
            "/v1/hub/workspaces",
            token=token,
            body={"dispatchable": False, "workspaces": [{"id": "w1", "label": "x"}]},
        )
        items = self.hub.app.dispatch.list_workspaces("d-off")
        self.assertFalse(items[0]["dispatchable"])

    def test_revoke_kills_relay_and_catalog(self) -> None:
        token = self.pair(device_id="d-gone")
        self.call(
            "POST",
            "/v1/hub/workspaces",
            token=token,
            body={"workspaces": [{"id": "w1", "label": "x"}]},
        )
        connection, response = self._open_relay(token)
        try:
            self.assertEqual(response.status, 200)
            self.assertTrue(self.hub.app.relay.online("d-gone"))
            status, _, _ = self.call(
                "POST", "/v1/devices/d-gone/revoke", admin=ADMIN_KEY
            )
            self.assertEqual(status, 200)
        finally:
            connection.close()
        self.assertFalse(self.hub.app.relay.online("d-gone"))
        self.assertEqual(self.hub.app.dispatch.list_workspaces("d-gone"), [])
        time.sleep(0.05)

    def test_relay_does_not_consume_proxy_sse_slots(self) -> None:
        """中继与代理流各算各的：两个会话在看流时设备仍能收到派活。"""
        token = self.pair(device_id="d-slots")
        opened = []
        try:
            for _ in range(2):
                conn = http.client.HTTPConnection("127.0.0.1", self.hub.port, timeout=10)
                conn.request(
                    "GET",
                    "/v1/runs/run_1/events",
                    headers={"Authorization": f"Bearer {token}"},
                )
                opened.append((conn, conn.getresponse()))
            connection, response = self._open_relay(token)
            try:
                self.assertEqual(response.status, 200)
                self.assertTrue(self.hub.app.relay.online("d-slots"))
            finally:
                connection.close()
        finally:
            for conn, response in opened:
                response.close()
                conn.close()


class CatalogTests(HubTestCase):
    def test_catalog_needs_a_token(self) -> None:
        status, payload, _ = self.call("GET", "/v1/hub/dispatch/workspaces")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")


if __name__ == "__main__":
    unittest.main()
