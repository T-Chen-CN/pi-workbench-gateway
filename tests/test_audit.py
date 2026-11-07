"""审计与限流：JSONL 双写、脱敏、窗口计数。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from hub.audit import FORBIDDEN_KEYS, Audit, sanitize
from hub.db import Database
from hub.errors import HubError
from hub.ratelimit import RateLimiter

from tests.helpers import ADMIN_KEY, HubTestCase


class AuditTests(HubTestCase):
    def test_pairing_events_are_recorded(self) -> None:
        self.pair(device_id="d-audit")
        events = [line["event"] for line in self.audit_lines()]
        self.assertIn("pairing.created", events)
        self.assertIn("pairing.consumed", events)

    def test_audit_never_contains_secrets(self) -> None:
        token = self.pair(device_id="d-secret")
        code = self.make_pairing_code()
        self.call("GET", "/v1/capabilities", token=token)
        raw = json.dumps(self.audit_lines(), ensure_ascii=False)
        self.assertNotIn(token, raw)
        self.assertNotIn(code.replace("-", ""), raw)
        self.assertNotIn(ADMIN_KEY, raw)

    def test_auth_failures_are_recorded(self) -> None:
        self.call("GET", "/v1/devices/me", token="pi_bogus")
        events = [line["event"] for line in self.audit_lines()]
        self.assertIn("auth.failed", events)

    def test_proxy_completion_is_recorded(self) -> None:
        token = self.pair()
        self.call("GET", "/v1/capabilities", token=token)
        completions = [line for line in self.audit_lines() if line["event"] == "proxy.completed"]
        self.assertTrue(completions)
        self.assertEqual(completions[-1]["status"], 200)


class SanitizeTests(unittest.TestCase):
    def test_forbidden_keys_are_dropped(self) -> None:
        detail = {"mode": "off", "token": "pi_x", "code": "ABC", "authorization": "Bearer y"}
        cleaned = json.loads(sanitize(detail) or "{}")
        self.assertEqual(cleaned, {"mode": "off"})
        for key in ("token", "code", "authorization"):
            self.assertIn(key, FORBIDDEN_KEYS)

    def test_empty_detail(self) -> None:
        self.assertIsNone(sanitize(None))
        self.assertIsNone(sanitize({}))
        self.assertIsNone(sanitize({"token": "x"}))


class AuditDualWriteTests(unittest.TestCase):
    def test_sqlite_and_jsonl_both_written(self) -> None:
        tmp = tempfile.mkdtemp(prefix="audit-")
        db = Database(os.path.join(tmp, "hub.db"))
        audit = Audit(db, tmp, clock=lambda: 1_793_878_400)
        audit.log("pairing.created", ip="127.0.0.1", status=201)
        rows = db.audit_events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "pairing.created")
        files = [name for name in os.listdir(tmp) if name.startswith("audit-")]
        self.assertTrue(files)
        with open(os.path.join(tmp, files[0]), encoding="utf-8") as handle:
            line = json.loads(handle.readline())
        self.assertEqual(line["event"], "pairing.created")


class RateLimiterTests(unittest.TestCase):
    def test_allows_until_limit_then_blocks(self) -> None:
        now = [0.0]
        limiter = RateLimiter(clock=lambda: now[0])
        for _ in range(3):
            self.assertTrue(limiter.hit("k", 3)[0])
        allowed, retry_after = limiter.hit("k", 3)
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry_after, 1)

        now[0] = 61.0
        self.assertTrue(limiter.hit("k", 3)[0])

    def test_independent_keys(self) -> None:
        limiter = RateLimiter(clock=lambda: 0.0)
        self.assertTrue(limiter.hit("a", 1)[0])
        self.assertFalse(limiter.hit("a", 1)[0])
        self.assertTrue(limiter.hit("b", 1)[0])

    def test_check_raises_with_retry_after(self) -> None:
        limiter = RateLimiter(clock=lambda: 0.0)
        limiter.check("k", 1)
        with self.assertRaises(HubError) as caught:
            limiter.check("k", 1)
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertIn("Retry-After", caught.exception.headers)

    def test_zero_limit_means_unlimited(self) -> None:
        limiter = RateLimiter(clock=lambda: 0.0)
        for _ in range(10):
            self.assertTrue(limiter.hit("k", 0)[0])


class RateLimitRouteTests(HubTestCase):
    rate_limit = 2

    def test_device_rate_limit(self) -> None:
        token = self.pair(device_id="d-rate")
        self.assertEqual(self.call("GET", "/v1/capabilities", token=token)[0], 200)
        self.assertEqual(self.call("GET", "/v1/capabilities", token=token)[0], 200)
        status, payload, headers = self.call("GET", "/v1/capabilities", token=token)
        self.assertEqual(status, 429)
        self.assertEqual(payload["error"]["code"], "rate_limited")
        self.assertIn("retry-after", headers)

    def test_health_is_not_rate_limited(self) -> None:
        for _ in range(5):
            self.assertEqual(self.call("GET", "/v1/hub/health")[0], 200)


if __name__ == "__main__":
    unittest.main()
