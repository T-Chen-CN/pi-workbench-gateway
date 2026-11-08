"""健康与诊断：匿名端点必须瘦，完整诊断要凭据。"""

from __future__ import annotations

import unittest

from hub import VERSION
from tests.helpers import ADMIN_KEY, HubTestCase

SLIM_FIELDS = {"status", "upstreamOk", "hub", "contractVersion", "features"}
LEAKY_FIELDS = {"devices", "uptimeSeconds", "adminKeyConfigured", "upstreamVersion", "dashboard"}


class HealthTests(HubTestCase):
    def test_health_is_anonymous_and_slim(self) -> None:
        status, payload, _ = self.call("GET", "/v1/hub/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["upstreamOk"])
        self.assertEqual(set(payload), SLIM_FIELDS)
        self.assertFalse(LEAKY_FIELDS & set(payload), "匿名端点不得吐诊断信息")

    def test_health_reports_upstream_down(self) -> None:
        self.upstream.stop()
        # 探测缓存 5 秒，这里用 force 走 status 的路径确认「不会假装健康」
        status, payload, _ = self.call("GET", "/v1/hub/status", admin=ADMIN_KEY)
        self.assertEqual(status, 200)
        self.assertFalse(payload["upstreamOk"])

    def test_status_requires_credentials(self) -> None:
        status, payload, _ = self.call("GET", "/v1/hub/status")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_status_with_admin_key(self) -> None:
        status, payload, _ = self.call("GET", "/v1/hub/status", admin=ADMIN_KEY)
        self.assertEqual(status, 200)
        self.assertEqual(payload["hub"], VERSION)  # 版本号跟着 hub.VERSION 走，别再写死
        self.assertTrue(payload["adminKeyConfigured"])
        self.assertTrue(payload["pairingEnabled"])
        self.assertEqual(payload["contractVersion"], 1)
        self.assertIn("pairing", payload["features"])
        self.assertEqual(payload["devices"]["total"], 0)

    def test_status_with_device_token(self) -> None:
        token = self.pair(device_id="d-status")
        status, payload, _ = self.call("GET", "/v1/hub/status", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["devices"]["total"], 1)
        self.assertEqual(payload["devices"]["active"], 1)

    def test_unknown_route_is_not_found(self) -> None:
        status, payload, _ = self.call("GET", "/v1/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_method_not_allowed(self) -> None:
        status, payload, _ = self.call("DELETE", "/v1/hub/health")
        self.assertEqual(status, 405)
        self.assertEqual(payload["error"]["code"], "method_not_allowed")


if __name__ == "__main__":
    unittest.main()
