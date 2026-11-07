"""鉴权：Bearer 解析、撤销、scope。"""

from __future__ import annotations

import unittest

from hub import auth
from hub.errors import HubError

from tests.helpers import HubTestCase


class AuthTests(HubTestCase):
    def test_missing_token(self) -> None:
        status, payload, _ = self.call("GET", "/v1/devices/me")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")
        self.assertEqual(payload["error"]["type"], "hub_error")

    def test_wrong_scheme(self) -> None:
        status, payload, _ = self.call(
            "GET", "/v1/devices/me", headers={"Authorization": "Basic abc"}
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_unknown_token(self) -> None:
        status, payload, _ = self.call("GET", "/v1/devices/me", token="pi_doesnotexist")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_revoked_token(self) -> None:
        token = self.pair(device_id="d-rev")
        status, payload, _ = self.call(
            "POST", "/v1/devices/d-rev/revoke", token=token
        )
        self.assertEqual(status, 200, payload)
        after = self.call("GET", "/v1/devices/me", token=token)
        self.assertEqual(after[0], 401)
        self.assertEqual(after[1]["error"]["code"], "device_revoked")

    def test_token_hash_is_not_stored_in_plaintext(self) -> None:
        token = self.pair(device_id="d-hash")
        row = self.hub.db.connection().execute(
            "SELECT token_hash, token_prefix FROM devices WHERE device_id = 'd-hash'"
        ).fetchone()
        self.assertNotEqual(row["token_hash"], token)
        self.assertEqual(len(row["token_hash"]), 64)
        self.assertEqual(row["token_prefix"], token[:7])


class AuthUnitTests(unittest.TestCase):
    def test_parse_bearer(self) -> None:
        self.assertEqual(auth.parse_bearer("Bearer pi_abc"), "pi_abc")
        self.assertEqual(auth.parse_bearer("bearer pi_abc"), "pi_abc")
        self.assertEqual(auth.parse_bearer("Bearer abc"), "")
        self.assertEqual(auth.parse_bearer(""), "")

    def test_admin_key_comparison(self) -> None:
        self.assertTrue(auth.admin_ok("same", "same"))
        self.assertFalse(auth.admin_ok("same", "other"))
        self.assertFalse(auth.admin_ok("", ""))
        self.assertFalse(auth.admin_ok("x", ""))

    def test_require_scope(self) -> None:
        device = auth.Device(
            device_id="d",
            name="n",
            scopes=("chat",),
            token_prefix="pi_x",
            created_at=0,
            last_seen_at=None,
            revoked_at=None,
        )
        auth.require_scope(device, "chat")
        auth.require_scope(device, None)
        with self.assertRaises(HubError) as caught:
            auth.require_scope(device, "dispatch")
        self.assertEqual(caught.exception.code, "scope_required")

    def test_new_token_shape(self) -> None:
        token = auth.new_token()
        self.assertTrue(token.startswith("pi_"))
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(auth.token_prefix(token), token[:7])
        self.assertNotEqual(token, auth.new_token())


if __name__ == "__main__":
    unittest.main()
