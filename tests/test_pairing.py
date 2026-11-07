"""配对：生成、消费、重放、过期、不存在、轮换。"""

from __future__ import annotations

import re
import tempfile
import shutil
import unittest

from hub import pairing
from hub.server import HubServer

from tests.helpers import ADMIN_KEY, HubTestCase


class PairingTests(HubTestCase):
    def test_pairing_requires_admin_key(self) -> None:
        status, payload, _ = self.call("POST", "/v1/devices/pairing")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "admin_required")
        self.assertEqual(payload["error"]["type"], "hub_error")

    def test_pairing_rejects_wrong_admin_key(self) -> None:
        status, payload, _ = self.call("POST", "/v1/devices/pairing", admin="nope")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "admin_required")

    def test_create_returns_code_expiry_and_url(self) -> None:
        status, payload, _ = self.call("POST", "/v1/devices/pairing", admin=ADMIN_KEY)
        self.assertEqual(status, 201)
        self.assertRegex(payload["code"], r"^[0-9A-Z]{5}-[0-9A-Z]{5}$")
        self.assertGreater(payload["expiresAt"], 0)
        # 链接里保留连字符：跟 code 字段同一个展示形（否则人眼以为拼错了）
        self.assertEqual(payload["pairingUrl"], f"https://hub.test/#pair={payload['code']}")
        # 服务端只存哈希：把码原样存进去就说明漏了
        rows = self.hub.db.connection().execute("SELECT code_hash FROM pairing_codes").fetchall()
        self.assertTrue(all(len(row["code_hash"]) == 64 for row in rows))
        self.assertNotIn(payload["code"].replace("-", ""), rows[0]["code_hash"].upper())

    def test_pair_issues_token_without_admin_credentials(self) -> None:
        code = self.make_pairing_code()
        status, payload, _ = self.call(
            "POST", "/v1/devices/pair", body={"code": code, "deviceId": "d-1", "name": "devbox"}
        )
        self.assertEqual(status, 201)
        self.assertTrue(payload["token"].startswith("pi_"))
        self.assertEqual(payload["tokenPrefix"], payload["token"][:7])
        self.assertEqual(payload["deviceId"], "d-1")
        self.assertEqual(sorted(payload["scopes"]), ["chat", "dispatch"])
        self.assertNotIn("admin", payload["scopes"])
        self.assertEqual(payload["dashboardUrl"], "https://dash.test")

    def test_code_is_normalized(self) -> None:
        code = self.make_pairing_code()
        messy = code.lower().replace("-", " ")
        status, payload, _ = self.call(
            "POST", "/v1/devices/pair", body={"code": messy, "deviceId": "d-2"}
        )
        self.assertEqual(status, 201, payload)

    def test_replay_is_rejected(self) -> None:
        code = self.make_pairing_code()
        first = self.call("POST", "/v1/devices/pair", body={"code": code, "deviceId": "d-3"})
        self.assertEqual(first[0], 201)
        second = self.call(
            "POST", "/v1/devices/pair", body={"code": code, "deviceId": "d-4"}
        )
        self.assertEqual(second[0], 409)
        self.assertEqual(second[1]["error"]["code"], "pairing_used")

    def test_expired_code(self) -> None:
        past = 1_000
        code = pairing.generate()
        self.hub.db.insert_pairing_code(
            code_hash=pairing.hash_code(code), created_at=past, expires_at=past + 1
        )
        status, payload, _ = self.call(
            "POST", "/v1/devices/pair", body={"code": code, "deviceId": "d-5"}
        )
        self.assertEqual(status, 410)
        self.assertEqual(payload["error"]["code"], "pairing_expired")

    def test_unknown_code(self) -> None:
        status, payload, _ = self.call(
            "POST", "/v1/devices/pair", body={"code": "ABCDE-FGHJK", "deviceId": "d-6"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "pairing_not_found")

    def test_garbage_code(self) -> None:
        status, payload, _ = self.call(
            "POST", "/v1/devices/pair", body={"code": "!!!", "deviceId": "d-7"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "pairing_not_found")

    def test_missing_device_id(self) -> None:
        code = self.make_pairing_code()
        status, payload, _ = self.call("POST", "/v1/devices/pair", body={"code": code})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_params")

    def test_repair_rotates_token(self) -> None:
        old_token = self.pair(device_id="d-8", name="first")
        status, _, _ = self.call("GET", "/v1/devices/me", token=old_token)
        self.assertEqual(status, 200)

        new_token = self.pair(device_id="d-8", name="second")
        self.assertNotEqual(old_token, new_token)

        stale = self.call("GET", "/v1/devices/me", token=old_token)
        self.assertEqual(stale[0], 401)
        fresh = self.call("GET", "/v1/devices/me", token=new_token)
        self.assertEqual(fresh[0], 200)
        self.assertEqual(fresh[1]["name"], "second")

    def test_pairing_disabled_without_admin_key(self) -> None:
        tmp = tempfile.mkdtemp(prefix="pi-hub-noadmin-")
        other = HubServer(
            data_dir=tmp,
            port=0,
            admin_key="",
            upstream_base=self.upstream.base,
            upstream_key="k",
        )
        other.start()
        try:
            import http.client
            import json

            connection = http.client.HTTPConnection("127.0.0.1", other.port, timeout=10)
            connection.request("POST", "/v1/devices/pairing", body=b"{}", headers={})
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 503)
            self.assertEqual(payload["error"]["code"], "pairing_disabled")
        finally:
            other.stop()
            shutil.rmtree(tmp, ignore_errors=True)


class PairingUnitTests(unittest.TestCase):
    def test_generate_shape(self) -> None:
        code = pairing.generate()
        self.assertRegex(code, r"^[0-9A-Z]{5}-[0-9A-Z]{5}$")
        self.assertTrue(pairing.is_valid(code))

    def test_ambiguous_characters_are_mapped(self) -> None:
        # O → 0, I / L → 1, U → V
        self.assertEqual(pairing.normalize("o1l-uv"), "011VV")
        self.assertEqual(len(pairing.normalize("ABCDE-FGHJK")), 10)

    def test_hash_is_stable_across_formats(self) -> None:
        self.assertEqual(pairing.hash_code("ABCDE-FGHJK"), pairing.hash_code("abcde fghjk"))


if __name__ == "__main__":
    unittest.main()
