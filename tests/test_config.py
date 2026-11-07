"""配置组装：环境变量映射、`os.pathsep`、dashboard 推导、管理密钥抹除。"""

from __future__ import annotations

import unittest

from config import (
    ADMIN_KEY_ENV,
    DEFAULT_PORT,
    DEFAULT_RATE_LIMIT,
    load_config,
    take_admin_key,
)


class LoadConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        config = load_config({})
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, DEFAULT_PORT)
        self.assertEqual(config.rate_limit, DEFAULT_RATE_LIMIT)
        self.assertEqual(config.upstream_base, "http://127.0.0.1:8642")
        self.assertEqual(config.dashboard_base, "http://127.0.0.1:9119")
        self.assertFalse(config.pairing_enabled)

    def test_full_mapping(self) -> None:
        env = {
            ADMIN_KEY_ENV: "secret",
            "PI_HUB_HOST": "127.0.0.1",
            "PI_HUB_PORT": "9999",
            "PI_HUB_PUBLIC_URL": "https://hub.example.com/",
            "PI_HUB_PUBLIC_DASHBOARD_URL": "https://dash.example.com/",
            "HERMES_API_BASE": "http://127.0.0.1:8642/",
            "API_SERVER_KEY": "api-key",
            "PI_HUB_DASHBOARD_BASE": "http://127.0.0.1:9119/",
            "HERMES_DASHBOARD_SESSION_TOKEN": "dash-token",
            "PI_HUB_RATE_LIMIT": "120",
            "HERMES_HOME": "/srv/hermes",
        }
        config = load_config(env)
        self.assertEqual(config.port, 9999)
        self.assertEqual(config.rate_limit, 120)
        self.assertEqual(config.public_url, "https://hub.example.com")
        self.assertEqual(config.public_dashboard_url, "https://dash.example.com")
        self.assertEqual(config.upstream_base, "http://127.0.0.1:8642")
        self.assertTrue(config.pairing_enabled)

    def test_dashboard_url_is_derived_from_public_url(self) -> None:
        config = load_config({"PI_HUB_PUBLIC_URL": "https://hub.example.com"})
        self.assertEqual(config.public_dashboard_url, "https://dash.example.com")

    def test_underivable_dashboard_url_stays_empty(self) -> None:
        config = load_config({"PI_HUB_PUBLIC_URL": "https://example.com"})
        self.assertEqual(config.public_dashboard_url, "")

    def test_bad_integers_fall_back(self) -> None:
        config = load_config({"PI_HUB_PORT": "abc", "PI_HUB_RATE_LIMIT": ""})
        self.assertEqual(config.port, DEFAULT_PORT)
        self.assertEqual(config.rate_limit, DEFAULT_RATE_LIMIT)

    def test_fs_roots_use_os_pathsep(self) -> None:
        import os

        raw = os.pathsep.join(["/media/a", "/media/b"])
        config = load_config({"PI_HUB_FS_ROOTS": raw})
        self.assertEqual(len(config.fs_roots), 2)

    def test_fs_roots_default_to_profile_dirs(self) -> None:
        config = load_config({"HERMES_HOME": "/srv/hermes"})
        self.assertTrue(any(p.endswith("uploads") for p in config.fs_roots))

    def test_admin_key_is_taken_out_of_environ(self) -> None:
        env = {ADMIN_KEY_ENV: "secret-value"}
        self.assertEqual(take_admin_key(env), "secret-value")
        self.assertNotIn(ADMIN_KEY_ENV, env)
        self.assertEqual(take_admin_key(env), "")


if __name__ == "__main__":
    unittest.main()
