"""配置面：白名单读、只写 approvals.mode、写后读回、媒体路径。"""

from __future__ import annotations

import os
import unittest

from tests.helpers import DASHBOARD_KEY, HubTestCase


class ConfigTests(HubTestCase):
    def test_read_returns_only_whitelisted_subset(self) -> None:
        token = self.pair()
        status, payload, _ = self.call("GET", "/api/config", token=token)
        self.assertEqual(status, 200, payload)
        self.assertEqual(set(payload), {"approvals", "agent", "terminal", "policy"})
        self.assertEqual(payload["approvals"]["mode"], "manual")
        self.assertEqual(payload["approvals"]["timeout"], 300)
        self.assertEqual(payload["agent"]["disabled_toolsets"], ["code_execution"])
        self.assertEqual(payload["terminal"]["backend"], "local")
        self.assertEqual(payload["policy"]["denyCount"], 1)
        # 整份配置里的其他键绝不能泄出去
        self.assertNotIn("model", payload)

    def test_read_uses_dashboard_token_not_device_token(self) -> None:
        token = self.pair()
        self.call("GET", "/api/config", token=token)
        seen = self.upstream.requests[-1]["headers"]
        self.assertEqual(seen["authorization"], f"Bearer {DASHBOARD_KEY}")

    def test_write_mode_and_read_back(self) -> None:
        token = self.pair()
        status, payload, _ = self.call(
            "PUT", "/api/config", token=token, body={"config": {"approvals": {"mode": "off"}}}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["approvals"]["mode"], "off")

    def test_write_rejects_unknown_mode(self) -> None:
        token = self.pair()
        status, payload, _ = self.call(
            "PUT", "/api/config", token=token, body={"config": {"approvals": {"mode": "yolo"}}}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_params")

    def test_write_rejects_flat_body_and_says_the_shape(self) -> None:
        """漏掉外层 config 时报错要指形状，而不是让人去查取值表。"""
        token = self.pair()
        status, payload, _ = self.call(
            "PUT", "/api/config", token=token, body={"approvals": {"mode": "manual"}}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_params")
        self.assertIn("config", payload["error"]["message"])

    def test_write_rejects_other_keys(self) -> None:
        token = self.pair()
        status, payload, _ = self.call(
            "PUT",
            "/api/config",
            token=token,
            body={"config": {"approvals": {"mode": "smart", "timeout": 1}}},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_params")

        status, payload, _ = self.call(
            "PUT",
            "/api/config",
            token=token,
            body={"config": {"agent": {"disabled_toolsets": []}}},
        )
        self.assertEqual(status, 400)

    def test_write_verifies_read_back(self) -> None:
        self.upstream.read_back_mode = "manual"  # 服务端把写入改回去了
        token = self.pair()
        status, payload, _ = self.call(
            "PUT", "/api/config", token=token, body={"config": {"approvals": {"mode": "off"}}}
        )
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["code"], "internal")

    def test_write_surfaces_dashboard_failure(self) -> None:
        self.upstream.put_status = 500
        token = self.pair()
        status, payload, _ = self.call(
            "PUT", "/api/config", token=token, body={"config": {"approvals": {"mode": "smart"}}}
        )
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "upstream_unavailable")


class MediaTests(HubTestCase):
    def test_image_upload_is_forwarded(self) -> None:
        token = self.pair()
        status, payload, _ = self.call(
            "POST",
            "/api/chat/image-upload",
            token=token,
            body={"data_url": "data:image/png;base64,AAAA", "filename": "a.png"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["path"], "/uploads/a.png")

    def test_read_data_url_inside_roots(self) -> None:
        token = self.pair()
        inside = os.path.join(self.tmp, "pic.png")
        status, payload, _ = self.call(
            "GET", f"/api/fs/read-data-url?path={inside}", token=token
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["data_url"].startswith("data:image"))

    def test_read_data_url_outside_roots(self) -> None:
        token = self.pair()
        status, payload, _ = self.call(
            "GET", "/api/fs/read-data-url?path=/etc/passwd", token=token
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "path_not_allowed")

    def test_read_data_url_requires_path(self) -> None:
        token = self.pair()
        status, payload, _ = self.call("GET", "/api/fs/read-data-url", token=token)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_params")


if __name__ == "__main__":
    unittest.main()
