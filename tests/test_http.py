"""Black-box HTTP tests, one or more per acceptance criterion (AC-1..AC-14)."""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest

import support


def _json_body(body: bytes) -> object:
    return json.loads(body.decode("utf-8"))


def _read_socket_response(sock: socket.socket) -> tuple[int, dict[str, str], bytes]:
    """Read exactly one Content-Length-delimited HTTP response from a socket."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError("connection closed before response headers")
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.decode("ascii").strip().lower()] = value.decode("ascii").strip()
    length = int(headers.get("content-length", "0"))
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return status, headers, body[:length]


class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = support.TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.upstream = support.Upstream()
        self.addCleanup(self.upstream.close)
        self.clock_path = support.write_clock(self.tmp.file("clock.txt"), 0)
        self.gateway: support.GatewayProcess | None = None

    def start(
        self,
        *,
        limit: int = 2,
        window_ms: int = 1000,
        token: str = support.DEFAULT_TOKEN,
        clock: bool = True,
        upstream_port: int | None = None,
    ) -> int:
        config = support.make_config(
            self.tmp.path,
            upstream_port=self.upstream.port
            if upstream_port is None
            else upstream_port,
            token=token,
            limit=limit,
            window_ms=window_ms,
        )
        self.gateway = support.GatewayProcess(
            config,
            clock_path=self.clock_path if clock else None,
        )
        self.addCleanup(self._stop_gateway)
        assert self.gateway.port is not None
        return self.gateway.port

    def _stop_gateway(self) -> None:
        if self.gateway is not None:
            self.gateway.stop()

    def get(self, port: int, path: str, token: str | None = support.DEFAULT_TOKEN):
        headers = support.auth_headers(token) if token is not None else None
        return support.http_request(port, "GET", path, headers=headers)

    # -- AC-1 --------------------------------------------------------------

    def test_ac01_health_without_auth(self) -> None:
        port = self.start()
        status, headers, body = support.http_request(port, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"status":"ok"}\n')
        self.assertTrue(headers["Content-Type"].startswith("application/json"))

    def test_ac01_health_ignores_query_and_invalid_clock(self) -> None:
        port = self.start()
        support.write_file(self.clock_path, b"not-a-clock\n")
        for path in ("/health", "/health?x=1"):
            with self.subTest(path=path):
                status, _, body = support.http_request(port, "GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(_json_body(body), {"status": "ok"})
        self.assertEqual(self.upstream.call_count(), 0)

    # -- AC-2 --------------------------------------------------------------

    def test_ac02_forwards_bytes_and_content_type(self) -> None:
        binary = b"\x00\x01upstream-\xff\xfe\n"
        self.upstream.configure(
            body=binary, content_type="application/x-custom; charset=latin-1"
        )
        port = self.start()
        status, headers, body = self.get(port, "/proxy")
        self.assertEqual(status, 200)
        self.assertEqual(body, binary)
        self.assertEqual(
            headers["Content-Type"], "application/x-custom; charset=latin-1"
        )

    def test_ac02_defaults_content_type_without_one(self) -> None:
        self.upstream.configure(body=b"plain", content_type=None)
        port = self.start()
        status, headers, body = self.get(port, "/proxy")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"plain")
        self.assertEqual(headers["Content-Type"], "application/octet-stream")

    # -- AC-3 --------------------------------------------------------------

    def test_ac03_auth_failures_are_401_without_upstream_calls(self) -> None:
        port = self.start()
        cases = {
            "missing": None,
            "wrong": "wrong-token",
            "lowercase_scheme": "bearer demo-token",
            "double_space": "Bearer  demo-token",
            "comma": "Bearer demo-token,",
        }
        for name, token in cases.items():
            with self.subTest(case=name):
                headers = {"Authorization": token} if token is not None else {}
                status, _, body = support.http_request(
                    port, "GET", "/proxy", headers=headers
                )
                self.assertEqual(status, 401)
                self.assertEqual(_json_body(body), {"error": "unauthorized"})
        request = (
            b"GET /proxy HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer demo-token\r\n"
            b"Authorization: Bearer demo-token\r\n"
            b"Connection: close\r\n\r\n"
        )
        response = support.raw_http(port, request)
        self.assertEqual(support.raw_status(response), 401)
        self.assertEqual(self.upstream.call_count(), 0)

        stdout, stderr = self.gateway.stop()  # type: ignore[union-attr]
        self.assertNotIn(b"demo-token", stderr)
        self.assertNotIn(b"demo-token", stdout)
        self.assertNotIn(b"demo-token", response)

    # -- AC-4 / AC-5 -------------------------------------------------------

    def test_ac04_limit_and_429(self) -> None:
        port = self.start(limit=2, window_ms=1000)
        statuses = [self.get(port, "/proxy")[0] for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])
        _, _, body = self.get(port, "/proxy")
        self.assertEqual(_json_body(body), {"error": "rate_limited"})
        self.assertEqual(self.upstream.call_count(), 2)

    def test_ac05_window_boundary(self) -> None:
        port = self.start(limit=2, window_ms=1000)
        self.assertEqual(self.get(port, "/proxy")[0], 200)
        self.assertEqual(self.get(port, "/proxy")[0], 200)
        self.assertEqual(self.get(port, "/proxy")[0], 429)

        support.write_clock(self.clock_path, 999)
        self.assertEqual(self.get(port, "/proxy")[0], 429)
        self.assertEqual(self.upstream.call_count(), 2)

        support.write_clock(self.clock_path, 1000)
        self.assertEqual(self.get(port, "/proxy")[0], 200)
        self.assertEqual(self.get(port, "/proxy")[0], 200)
        self.assertEqual(self.get(port, "/proxy")[0], 429)
        self.assertEqual(self.upstream.call_count(), 4)

    # -- AC-6 --------------------------------------------------------------

    def test_ac06_concurrent_requests_reserve_one_slot(self) -> None:
        port = self.start(limit=2, window_ms=1000)
        self.assertEqual(self.get(port, "/proxy")[0], 200)

        barrier = threading.Barrier(2)
        results: list[int] = []
        lock = threading.Lock()

        def call() -> None:
            barrier.wait()
            status, _, _ = self.get(port, "/proxy")
            with lock:
                results.append(status)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sorted(results), [200, 429])
        self.assertEqual(self.upstream.call_count(), 2)

    # -- AC-7 --------------------------------------------------------------

    def test_ac07_upstream_timeout_is_controlled_502(self) -> None:
        self.upstream.configure(behavior="sleep", delay=2.5)
        port = self.start(limit=5)
        started = time.monotonic()
        status, _, body = self.get(port, "/proxy")
        elapsed = time.monotonic() - started
        self.assertEqual(status, 502)
        self.assertEqual(_json_body(body), {"error": "upstream_error"})
        self.assertLess(elapsed, 2.0)

    def test_ac07_connection_refused_is_502(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        refused_port = probe.getsockname()[1]
        probe.close()
        port = self.start(upstream_port=refused_port)
        status, _, body = self.get(port, "/proxy")
        self.assertEqual(status, 502)
        self.assertEqual(_json_body(body), {"error": "upstream_error"})

    def test_ac07_oversized_upstream_body_is_502(self) -> None:
        self.upstream.configure(behavior="big")
        port = self.start(limit=5)
        status, _, body = self.get(port, "/proxy")
        self.assertEqual(status, 502)
        self.assertEqual(_json_body(body), {"error": "upstream_too_large"})

    def test_ac07_exactly_65536_bytes_is_allowed(self) -> None:
        self.upstream.configure(
            body=b"y" * 65536, content_type="application/octet-stream"
        )
        port = self.start(limit=5)
        status, _, body = self.get(port, "/proxy")
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 65536)

    # -- AC-8 --------------------------------------------------------------

    def test_ac08_redirect_is_not_followed_and_location_is_dropped(self) -> None:
        self.upstream.configure(behavior="redirect")
        port = self.start(limit=5)
        status, headers, body = self.get(port, "/proxy")
        self.assertEqual(status, 302)
        self.assertNotIn("Location", headers)
        self.assertEqual(body, b"")
        self.assertEqual(self.upstream.call_count(), 1)

    # -- AC-9 --------------------------------------------------------------

    def test_ac09_query_is_forwarded_and_authorization_is_not(self) -> None:
        port = self.start(limit=5)
        status, _, _ = self.get(port, "/proxy?a=1&b=two+words")
        self.assertEqual(status, 200)
        self.assertEqual(self.upstream.call_count(), 1)
        path, headers = self.upstream.requests[0]
        self.assertEqual(path, "/?a=1&b=two+words")
        lowered = {key.lower() for key in headers}
        self.assertNotIn("authorization", lowered)

    # -- AC-10 -------------------------------------------------------------

    def test_ac10_unknown_paths_and_methods(self) -> None:
        port = self.start(limit=2)

        def raw_get_status(target: str) -> int:
            request = (
                f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            return support.raw_status(support.raw_http(port, request))

        self.assertEqual(raw_get_status("/proxy/extra"), 404)
        self.assertEqual(raw_get_status("/proxy/"), 404)
        self.assertEqual(raw_get_status("/Health"), 404)
        self.assertEqual(raw_get_status("/health/"), 404)

        status, _, body = support.http_request(
            port, "POST", "/proxy", headers=support.auth_headers()
        )
        self.assertEqual(status, 405)
        self.assertEqual(_json_body(body), {"error": "method_not_allowed"})
        self.assertEqual(self.upstream.call_count(), 0)

        # 404/405 do not consume quota: two passes remain.
        self.assertEqual(self.get(port, "/proxy")[0], 200)
        self.assertEqual(self.get(port, "/proxy")[0], 200)
        self.assertEqual(self.get(port, "/proxy")[0], 429)
        self.assertEqual(self.upstream.call_count(), 2)

    def test_ac10_head_returns_405_with_empty_body(self) -> None:
        port = self.start(limit=5)
        for path in ("/health", "/proxy"):
            with self.subTest(path=path):
                status, _, body = support.http_request(port, "HEAD", path)
                self.assertEqual(status, 405)
                self.assertEqual(body, b"")
        self.assertEqual(self.upstream.call_count(), 0)

    def test_ac10_post_body_does_not_desync_keep_alive(self) -> None:
        port = self.start(limit=5)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(
                b"POST /proxy HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Authorization: Bearer demo-token\r\n"
                b"Content-Length: 11\r\n\r\n"
                b"hello world"
            )
            status, _, body = _read_socket_response(sock)
            self.assertEqual(status, 405)
            self.assertEqual(_json_body(body), {"error": "method_not_allowed"})

            sock.sendall(
                b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            status, _, body = _read_socket_response(sock)
            self.assertEqual(status, 200)
            self.assertEqual(_json_body(body), {"status": "ok"})
        self.assertEqual(self.upstream.call_count(), 0)

    def test_ac10_get_with_body_does_not_desync_keep_alive(self) -> None:
        port = self.start(limit=5)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(
                b"GET /proxy HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Length: 4\r\n\r\n"
                b"body"
            )
            status, _, body = _read_socket_response(sock)
            self.assertEqual(status, 401)
            self.assertEqual(_json_body(body), {"error": "unauthorized"})

            sock.sendall(
                b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            status, _, body = _read_socket_response(sock)
            self.assertEqual(status, 200)
            self.assertEqual(_json_body(body), {"status": "ok"})
        self.assertEqual(self.upstream.call_count(), 0)

    def test_ac10_undrainable_body_closes_connection(self) -> None:
        port = self.start(limit=5)
        cases = {
            "oversized": b"Content-Length: 65537\r\n",
            "chunked": b"Transfer-Encoding: chunked\r\n",
            "malformed": b"Content-Length: nope\r\n",
        }
        for name, framing in cases.items():
            with self.subTest(case=name):
                with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                    sock.sendall(
                        b"POST /proxy HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                        + framing
                        + b"\r\n"
                    )
                    status, headers, body = _read_socket_response(sock)
                    self.assertEqual(status, 405)
                    self.assertEqual(_json_body(body), {"error": "method_not_allowed"})
                    self.assertEqual(headers.get("connection", "").lower(), "close")
                    self.assertEqual(sock.recv(4096), b"")
        self.assertEqual(self.upstream.call_count(), 0)

    # -- AC-12 -------------------------------------------------------------

    def test_ac12_clock_regression_invalid_or_missing_is_503(self) -> None:
        port = self.start(limit=2, window_ms=1000)
        self.assertEqual(self.get(port, "/proxy")[0], 200)
        self.assertEqual(self.get(port, "/proxy")[0], 200)
        self.assertEqual(self.get(port, "/proxy")[0], 429)

        support.write_clock(self.clock_path, 500)
        self.assertEqual(self.get(port, "/proxy")[0], 429)

        support.write_clock(self.clock_path, 300)
        status, _, body = self.get(port, "/proxy")
        self.assertEqual(status, 503)
        self.assertEqual(_json_body(body), {"error": "clock_error"})

        support.write_clock(self.clock_path, 500)
        self.assertEqual(self.get(port, "/proxy")[0], 429)

        support.write_file(self.clock_path, b"12x\n")
        status, _, body = self.get(port, "/proxy")
        self.assertEqual(status, 503)
        self.assertEqual(_json_body(body), {"error": "clock_error"})

        support.write_file(self.clock_path, b"+500\n")
        self.assertEqual(self.get(port, "/proxy")[0], 503)
        support.write_file(self.clock_path, b"500\r\n")
        self.assertEqual(self.get(port, "/proxy")[0], 503)

        import os

        os.unlink(self.clock_path)
        status, _, body = self.get(port, "/proxy")
        self.assertEqual(status, 503)
        self.assertEqual(_json_body(body), {"error": "clock_error"})

        support.write_clock(self.clock_path, 500)
        self.assertEqual(self.get(port, "/proxy")[0], 429)
        self.assertEqual(self.upstream.call_count(), 2)

    # -- AC-13 -------------------------------------------------------------

    def test_ac13_restart_clears_quota(self) -> None:
        port = self.start(limit=2, window_ms=1000)
        self.assertEqual(self.get(port, "/proxy")[0], 200)
        self.assertEqual(self.get(port, "/proxy")[0], 200)
        self.assertEqual(self.get(port, "/proxy")[0], 429)
        self.gateway.stop()  # type: ignore[union-attr]
        self.gateway = None

        port = self.start(limit=2, window_ms=1000)
        self.assertEqual(self.get(port, "/proxy")[0], 200)
        self.assertEqual(self.upstream.call_count(), 3)

    # -- AC-14 -------------------------------------------------------------

    def test_ac14_clock_upper_bound_and_integers(self) -> None:
        max_clock = (1 << 53) - 1
        support.write_clock(self.clock_path, max_clock)
        port = self.start(limit=1, window_ms=1000)
        self.assertEqual(self.get(port, "/proxy")[0], 200)
        self.assertEqual(self.get(port, "/proxy")[0], 429)

        support.write_clock(self.clock_path, max_clock + 1)
        status, _, body = self.get(port, "/proxy")
        self.assertEqual(status, 503)
        self.assertEqual(_json_body(body), {"error": "clock_error"})

        support.write_clock(self.clock_path, 0)
        self.assertEqual(self.get(port, "/proxy")[0], 503)
        self.assertEqual(self.upstream.call_count(), 1)

    # -- AC-11-adjacent startup checks ------------------------------------

    def test_startup_stdout_is_single_json_line(self) -> None:
        port = self.start()
        gateway = self.gateway
        assert gateway is not None
        self.assertEqual(gateway.startup_line, b'{"port":%d}\n' % port)
        stdout_tail, stderr = gateway.stop()
        self.assertEqual(stdout_tail, b"")
        self.assertEqual(stderr, b"")


if __name__ == "__main__":
    unittest.main()
