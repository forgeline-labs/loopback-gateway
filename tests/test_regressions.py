"""Public regressions: framing, total deadlines, validation and DNS-free startup."""

import json
import os
import socket
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import support


@contextmanager
def raw_upstream(parts):
    stop = threading.Event()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)

        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(3)
                    connection.recv(4096)
                    for delay, data in parts:
                        if stop.wait(delay):
                            break
                        connection.sendall(data)
            except OSError:
                pass  # The gateway may close at its deadline.

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            yield listener.getsockname()[1]
        finally:
            stop.set()
            worker.join(timeout=4)
            assert not worker.is_alive()


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = support.TempDir()
        self.addCleanup(self.tmp.cleanup)

    def gateway(self, upstream_port=9, **kwargs):
        config = support.make_config(
            self.tmp.path, upstream_port=upstream_port, limit=1, window_ms=60000
        )
        gateway = support.GatewayProcess(config, **kwargs)
        self.addCleanup(gateway.stop)
        return gateway

    def upstream_error(self, parts, *, timed=False):
        with raw_upstream(parts) as port:
            gateway = self.gateway(port)
            started = time.monotonic()
            status, _, body = support.http_request(
                gateway.port, "GET", "/proxy", headers=support.auth_headers()
            )
            elapsed = time.monotonic() - started
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body), {"error": "upstream_error"})
        if timed:
            self.assertLess(elapsed, 2.0)
        # Failures still consume their reserved slot.
        status, _, _ = support.http_request(
            gateway.port, "GET", "/proxy", headers=support.auth_headers()
        )
        self.assertEqual(status, 429)

    def test_truncated_content_length_is_not_success(self):
        self.upstream_error([(0, b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nabc")])

    def test_total_deadline_includes_headers_body_and_chunk_framing(self):
        headers = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n"
        cases = {
            "headers": [(0, headers)]
            + [(0.3, b"X-Slow: x\r\n")] * 8
            + [(0, b"\r\nabcde")],
            "body": [(0, headers + b"\r\na")] + [(0.6, b"b")] * 4,
            "combined": [(0.6, headers + b"\r\n"), (0.6, b"abcde")],
            "chunked": [(0, b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")]
            + [(0.3, b"1\r\nx\r\n")] * 8
            + [(0, b"0\r\n\r\n")],
        }
        for phase, parts in cases.items():
            with self.subTest(phase=phase):
                self.upstream_error(parts, timed=True)

    def test_no_body_statuses_ignore_declared_representation_length(self):
        for status in (204, 304):
            with self.subTest(status=status):
                with raw_upstream(
                    [
                        (
                            0,
                            f"HTTP/1.1 {status} Empty\r\nContent-Length: 10\r\n\r\n".encode(),
                        )
                    ]
                ) as port:
                    gateway = self.gateway(port)
                    actual, _, body = support.http_request(
                        gateway.port, "GET", "/proxy", headers=support.auth_headers()
                    )
                    self.assertEqual(actual, status)
                    self.assertEqual(body, b"")

    def test_client_body_read_has_total_deadline_and_closes_connection(self):
        gateway = self.gateway()
        for dripping in (False, True):
            with self.subTest(dripping=dripping):
                with socket.create_connection(
                    ("127.0.0.1", gateway.port), timeout=2
                ) as sock:
                    sock.sendall(
                        b"POST /proxy HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
                        + (b"5" if dripping else b"1")
                        + b"\r\n\r\n"
                    )
                    stop = threading.Event()

                    def drip():
                        try:
                            while not stop.wait(0.3):
                                sock.sendall(b"x")
                        except OSError:
                            pass

                    worker = threading.Thread(target=drip, daemon=True)
                    if dripping:
                        worker.start()
                    started = time.monotonic()
                    try:
                        response = b""
                        while True:
                            data = sock.recv(65536)
                            if not data:
                                break
                            response += data
                    finally:
                        stop.set()
                        if dripping:
                            worker.join(timeout=2)
                    self.assertLess(time.monotonic() - started, 2.0)
                    self.assertEqual(support.raw_status(response), 405)
                    self.assertIn(
                        b"connection: close", support.raw_header_block(response)
                    )
                    self.assertEqual(
                        json.loads(support.raw_body(response)),
                        {"error": "method_not_allowed"},
                    )

    def test_long_port_is_controlled_and_leading_zeroes_remain_valid(self):
        config = support.make_config(self.tmp.path, upstream_port=9)
        result = support.run_cli("--config", config, "--port", "9" * 4301)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.stderr.startswith(b"error:"))
        self.assertEqual(result.stderr.count(b"\n"), 1)
        self.assertNotIn(b"Traceback", result.stderr)
        self.gateway(port="0" * 4301)

    def test_duplicate_key_diagnostic_does_not_echo_key(self):
        config = support.write_file(
            self.tmp.file("duplicate.json"),
            '{"token":"demo-token","demo-token":1,"demo-token":2}',
        )
        result = support.run_cli("--config", config, "--port", "0")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr.count(b"\n"), 1)
        self.assertTrue(result.stderr.startswith(b"error:"))
        self.assertNotIn(b"demo-token", result.stderr)

    def test_startup_does_not_wait_for_reverse_dns(self):
        # External environment fault injection; no gateway internals imported.
        support.write_file(
            self.tmp.file("sitecustomize.py"),
            "import socket, time\n"
            "def slow_dns(*args, **kwargs):\n"
            "    time.sleep(6)\n"
            '    return "localhost"\n'
            "socket.getfqdn = slow_dns\n",
        )
        with patch.dict(os.environ, {"PYTHONPATH": self.tmp.path}):
            gateway = self.gateway()
        status, _, _ = support.http_request(gateway.port, "GET", "/health")
        self.assertEqual(status, 200)
