"""Shared helpers for the public black-box test suite.

Tests drive the gateway only through its CLI and loopback HTTP interface.
No private gateway module is imported.
"""

from __future__ import annotations

import http.client
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVE = os.path.join(ROOT, "serve")
PYTHON = sys.executable
DEFAULT_TOKEN = "demo-token"
STARTUP_TIMEOUT = 5.0


def make_config(
    directory: str,
    *,
    upstream_port: int,
    token: str = DEFAULT_TOKEN,
    limit: int = 2,
    window_ms: int = 1000,
) -> str:
    path = os.path.join(directory, "config.json")
    payload = {
        "upstream_port": upstream_port,
        "token": token,
        "limit": limit,
        "window_ms": window_ms,
    }
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return path


def write_file(path: str, data: bytes | str) -> str:
    mode = "wb" if isinstance(data, bytes) else "w"
    kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8", "newline": ""}
    with open(path, mode, **kwargs) as handle:
        handle.write(data)
    return path


def write_clock(path: str, value: object) -> str:
    if isinstance(value, bytes):
        return write_file(path, value)
    return write_file(path, f"{value}\n")


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        server: "Upstream" = self.server  # type: ignore[assignment]
        server.record(self.path, dict(self.headers.items()))
        behavior = server.behavior
        if behavior == "sleep":
            time.sleep(server.delay)
        if behavior == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if behavior == "big":
            body = b"x" * 65537
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass
            return
        body = server.body
        self.send_response(server.status)
        if server.content_type is not None:
            self.send_header("Content-Type", server.content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        return


class Upstream:
    """A controllable loopback upstream used as the gateway's target."""

    def __init__(self) -> None:
        self.status = 200
        self.body = b"upstream-ok"
        self.content_type: str | None = "text/plain"
        self.behavior = "normal"
        self.delay = 2.5
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._lock = threading.Lock()
        self._server = _QuietServer(("127.0.0.1", 0), _UpstreamHandler)
        self._server.daemon_threads = True
        self._server.behavior = self.behavior  # type: ignore[attr-defined]
        self._server.body = self.body  # type: ignore[attr-defined]
        self._server.content_type = self.content_type  # type: ignore[attr-defined]
        self._server.status = self.status  # type: ignore[attr-defined]
        self._server.delay = self.delay  # type: ignore[attr-defined]
        self._server.record = self.record  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def configure(self, **kwargs: object) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)
            setattr(self._server, key, value)

    def record(self, path: str, headers: dict[str, str]) -> None:
        with self._lock:
            self.requests.append((path, headers))

    def call_count(self) -> int:
        with self._lock:
            return len(self.requests)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class GatewayProcess:
    """A gateway subprocess observed only through stdout/stderr and HTTP."""

    def __init__(
        self,
        config_path: str,
        *,
        port: int = 0,
        clock_path: str | None = None,
        extra_args: list[str] | None = None,
        expect_ready: bool = True,
    ) -> None:
        args = [PYTHON, SERVE, "--config", config_path, "--port", str(port)]
        if clock_path is not None:
            args.extend(["--clock-file", clock_path])
        if extra_args:
            args.extend(extra_args)
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        self.process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            cwd=ROOT,
            env=env,
        )
        self.port: int | None = None
        self.startup_line = b""
        self._closed = False
        if expect_ready:
            self.port = self._read_startup_port()

    def _read_startup_port(self) -> int:
        stdout = self.process.stdout
        assert stdout is not None
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("gateway did not become ready within 5 seconds")
            ready, _, _ = select.select([stdout], [], [], remaining)
            if not ready:
                raise AssertionError("gateway did not become ready within 5 seconds")
            line = stdout.readline()
            if not line:
                raise AssertionError(
                    f"gateway exited before startup: {self._drain_stderr()}"
                )
            self.startup_line = line
            payload = json.loads(line)
            port = payload["port"]
            assert isinstance(port, int)
            return port

    def _drain_stderr(self) -> bytes:
        stderr = self.process.stderr
        if stderr is None:
            return b""
        try:
            return stderr.read() or b""
        except OSError:
            return b""

    def stop(self, timeout: float = 5.0) -> tuple[bytes, bytes]:
        if self._closed:
            return b"", b""
        self._closed = True
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=timeout)
        stdout_tail = b""
        stderr_tail = b""
        if self.process.stdout is not None:
            stdout_tail = self.process.stdout.read() or b""
            self.process.stdout.close()
        if self.process.stderr is not None:
            stderr_tail = self.process.stderr.read() or b""
            self.process.stderr.close()
        return stdout_tail, stderr_tail


class TempDir:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="loopback-gateway-test-")
        self.path = self._tmp.name

    def file(self, name: str) -> str:
        return os.path.join(self.path, name)

    def cleanup(self) -> None:
        self._tmp.cleanup()


def http_request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), body
    finally:
        connection.close()


def raw_http(port: int, request: bytes, *, timeout: float = 5.0) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks)


def raw_status(response: bytes) -> int:
    status_line = response.split(b"\r\n", 1)[0]
    return int(status_line.split(b" ")[1])


def raw_header_block(response: bytes) -> bytes:
    return response.split(b"\r\n\r\n", 1)[0].lower()


def raw_body(response: bytes) -> bytes:
    parts = response.split(b"\r\n\r\n", 1)
    return parts[1] if len(parts) == 2 else b""


def run_cli(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [PYTHON, SERVE, *args],
        capture_output=True,
        cwd=ROOT,
        env=env,
        timeout=timeout,
    )


def auth_headers(token: str = DEFAULT_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
