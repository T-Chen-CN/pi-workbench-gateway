"""设备面：me / heartbeat / revoke / register。"""

from __future__ import annotations

import unittest

from hub import VERSION
from tests.helpers import ADMIN_KEY, HubTestCase


class DeviceTests(HubTestCase):
    def test_me_shape_matches_desktop_contract(self) -> None:
        token = self.pair(device_id="d-me", name="devbox")
        status, payload, _ = self.call("GET", "/v1/devices/me", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["deviceId"], "d-me")
        self.assertEqual(payload["name"], "devbox")
        self.assertEqual(payload["hub"], VERSION)  # 版本号跟着 hub.VERSION 走，别再写死
        self.assertEqual(sorted(payload["scopes"]), ["chat", "dispatch"])
        self.assertEqual(payload["onlineWindow"], 90)
        self.assertIn("serverTime", payload)
        self.assertTrue(payload["online"])
        self.assertIsNone(payload["revokedAt"])

    def test_heartbeat(self) -> None:
        token = self.pair(device_id="d-hb")
        status, payload, _ = self.call("POST", "/v1/devices/me/heartbeat", token=token)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["deviceId"], "d-hb")
        self.assertEqual(payload["onlineWindow"], 90)
        self.assertIn("serverTime", payload)

    def test_self_revoke(self) -> None:
        token = self.pair(device_id="d-self")
        status, payload, _ = self.call("POST", "/v1/devices/d-self/revoke", token=token)
        self.assertEqual(status, 200)
        self.assertTrue(payload["revoked"])

    def test_revoking_another_device_needs_admin(self) -> None:
        token = self.pair(device_id="d-a")
        self.pair(device_id="d-b")
        denied = self.call("POST", "/v1/devices/d-b/revoke", token=token)
        self.assertEqual(denied[0], 403)
        self.assertEqual(denied[1]["error"]["code"], "admin_required")

        allowed = self.call("POST", "/v1/devices/d-b/revoke", admin=ADMIN_KEY)
        self.assertEqual(allowed[0], 200)
        row = self.hub.db.device("d-b")
        self.assertIsNotNone(row["revoked_at"])

    def test_register_break_glass(self) -> None:
        status, payload, _ = self.call(
            "POST", "/v1/devices/register", body={"name": "ops-laptop"}, admin=ADMIN_KEY
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload["name"], "ops-laptop")
        self.assertEqual(sorted(payload["scopes"]), ["chat", "dispatch"])
        token = payload["token"]
        me = self.call("GET", "/v1/devices/me", token=token)
        self.assertEqual(me[0], 200)
        self.assertEqual(me[1]["deviceId"], payload["deviceId"])

    def test_register_requires_admin(self) -> None:
        status, payload, _ = self.call("POST", "/v1/devices/register", body={})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "admin_required")

    def test_name_is_sanitized(self) -> None:
        code = self.make_pairing_code()
        status, payload, _ = self.call(
            "POST",
            "/v1/devices/pair",
            body={"code": code, "deviceId": "d-name", "name": "x" * 200},
        )
        self.assertEqual(status, 201)
        self.assertLessEqual(len(payload["name"]), 64)


if __name__ == "__main__":
    unittest.main()
