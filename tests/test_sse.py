"""SSE：不缓冲、不压缩、不设 Content-Length。

这是最难回归的一条：Caddy 或插件任一层缓冲，表现都是「对话卡住、最后一次性出全文」。
所以这里不只断言头部，还断言**第一帧真的早于流结束到达**。
"""

from __future__ import annotations

import http.client
import time
import unittest

from tests.helpers import HubTestCase


class SseTests(HubTestCase):
    def _open_stream(self, token: str) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        connection = http.client.HTTPConnection("127.0.0.1", self.hub.port, timeout=10)
        connection.request(
            "GET",
            "/v1/runs/run_1/events",
            headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        )
        return connection, connection.getresponse()

    def test_headers_disable_buffering(self) -> None:
        token = self.pair()
        connection, response = self._open_stream(token)
        try:
            self.assertEqual(response.status, 200)
            headers = {k.lower(): v for k, v in response.getheaders()}
            self.assertEqual(headers["x-accel-buffering"], "no")
            self.assertIn("text/event-stream", headers["content-type"])
            self.assertNotIn("content-length", headers)
        finally:
            connection.close()

    def test_first_frame_arrives_before_stream_ends(self) -> None:
        token = self.pair()
        connection, response = self._open_stream(token)
        started = time.monotonic()
        try:
            first = response.read(1)
            first_at = time.monotonic() - started
            self.assertTrue(first, "流里应该有数据")
            rest = response.read()
            total_at = time.monotonic() - started
        finally:
            connection.close()
        # 上游三帧之间各等 0.25s：不缓冲的话第一帧会远早于整段结束
        self.assertLess(first_at, 0.2, f"首字节用了 {first_at:.3f}s，疑似被缓冲")
        self.assertGreater(total_at, first_at)
        self.assertIn(b"id: 3", first + rest)

    def test_last_seq_is_forwarded(self) -> None:
        token = self.pair()
        connection, response = self._open_stream(token)
        try:
            response.read()
        finally:
            connection.close()
        self.assertIn("last_seq", self._call_with_seq(token))

    def _call_with_seq(self, token: str) -> str:
        connection = http.client.HTTPConnection("127.0.0.1", self.hub.port, timeout=10)
        connection.request(
            "GET",
            "/v1/runs/run_1/events?last_seq=7",
            headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        return self.upstream.requests[-1]["path"]


if __name__ == "__main__":
    unittest.main()
