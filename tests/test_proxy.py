"""代理：deny-by-default 白名单、header 重写、author 覆盖、幂等键前缀。"""

from __future__ import annotations

import json
import unittest

from tests.helpers import UPSTREAM_KEY, HubTestCase


class WhitelistTests(HubTestCase):
    def test_unknown_path_is_denied(self) -> None:
        token = self.pair()
        status, payload, _ = self.call("GET", "/v1/admin/danger", token=token)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertTrue(payload["error"]["message"].startswith("no hub route"))

    def test_unlisted_dashboard_path_is_denied(self) -> None:
        token = self.pair()
        status, payload, _ = self.call("GET", "/api/secrets", token=token)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_proxy_requires_device_token(self) -> None:
        status, payload, _ = self.call("GET", "/v1/capabilities")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")


class ForwardingTests(HubTestCase):
    def test_header_rewrite(self) -> None:
        token = self.pair(device_id="d-headers")
        status, payload, _ = self.call(
            "GET",
            "/v1/capabilities",
            token=token,
            admin="client-should-not-leak",
            headers={"X-Pi-Device": "spoofed", "Accept-Encoding": "gzip"},
        )
        self.assertEqual(status, 200, payload)
        seen = self.upstream.requests[-1]["headers"]
        self.assertEqual(seen["authorization"], f"Bearer {UPSTREAM_KEY}")
        self.assertEqual(seen["x-pi-device"], "d-headers")
        self.assertEqual(seen["accept-encoding"], "identity")
        self.assertNotIn("x-pi-admin-key", seen)

    def test_query_string_is_forwarded(self) -> None:
        token = self.pair()
        self.call("GET", "/api/sessions?limit=5&offset=10", token=token)
        self.assertEqual(self.upstream.requests[-1]["path"], "/api/sessions?limit=5&offset=10")

    def test_runs_create_overrides_author_and_idempotency(self) -> None:
        token = self.pair(device_id="d-runs", name="devbox")
        status, _, _ = self.call(
            "POST",
            "/v1/runs",
            token=token,
            body={"input": "hi", "author": {"id": "spoofed"}},
            headers={"Idempotency-Key": "client-key-1"},
        )
        self.assertEqual(status, 202)
        seen = self.upstream.requests[-1]
        forwarded = json.loads(seen["body"].decode("utf-8"))
        self.assertEqual(forwarded["author"]["id"], "pi-workbench/d-runs")
        self.assertEqual(forwarded["author"]["name"], "devbox")
        self.assertEqual(forwarded["input"], "hi")
        self.assertEqual(seen["headers"]["idempotency-key"], "pi:d-runs:client-key-1")

    def test_runs_create_rejects_broken_json(self) -> None:
        token = self.pair()
        status, payload, _ = self.call(
            "POST",
            "/v1/runs",
            token=token,
            headers={"Content-Type": "application/json"},
            body=None,
        )
        self.assertEqual(status, 202)  # 空 body 允许；由上游决定
        _ = payload
        # 真正畸形的 body
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.hub.port, timeout=10)
        connection.request(
            "POST",
            "/v1/runs",
            body=b"{not json",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        self.assertEqual(response.status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_params")

    def test_upstream_404_is_passed_through(self) -> None:
        token = self.pair()
        status, payload, _ = self.call("GET", "/v1/toolsets", token=token)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_upstream_unavailable(self) -> None:
        token = self.pair()
        self.upstream.stop()
        status, payload, _ = self.call("GET", "/v1/capabilities", token=token)
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "upstream_unavailable")


if __name__ == "__main__":
    unittest.main()
