"""Black-box CLI and configuration tests (AC-11, AC-14 boundaries)."""

from __future__ import annotations

import json
import os
import unittest

import support


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = support.TempDir()
        self.addCleanup(self.tmp.cleanup)

    def write_config(
        self, payload: object, *, raw: bool = False, name: str = "config.json"
    ) -> str:
        path = self.tmp.file(name)
        if raw:
            with open(path, "wb") as handle:
                handle.write(payload)  # type: ignore[arg-type]
        else:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
        return path

    def valid_payload(self) -> dict[str, object]:
        return {
            "upstream_port": 9000,
            "token": "demo-token",
            "limit": 2,
            "window_ms": 1000,
        }

    def assert_cli_error(self, result: object, exit_code: int) -> None:
        self.assertEqual(result.returncode, exit_code, result.stderr)  # type: ignore[attr-defined]
        self.assertEqual(result.stdout, b"")  # type: ignore[attr-defined]
        self.assertTrue(result.stderr.startswith(b"error:"))  # type: ignore[attr-defined]
        self.assertEqual(result.stderr.count(b"\n"), 1)  # type: ignore[attr-defined]

    # -- version / help ---------------------------------------------------

    def test_ac_version_is_exact(self) -> None:
        result = support.run_cli("--version")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"loopback-gateway 0.1.0\n")
        self.assertEqual(result.stderr, b"")

    def test_ac_help_lists_documented_interface(self) -> None:
        result = support.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")
        text = result.stdout.decode("utf-8")
        for token in (
            "usage:",
            "serve",
            "--help",
            "--version",
            "--config",
            "--port",
            "--clock-file",
        ):
            self.assertIn(token, text)

    # -- argument validation ----------------------------------------------

    def test_unknown_argument_is_exit_2(self) -> None:
        self.assert_cli_error(support.run_cli("--nope"), 2)

    def test_missing_required_arguments_is_exit_2(self) -> None:
        self.assert_cli_error(support.run_cli(), 2)
        self.assert_cli_error(support.run_cli("--config", "x.json"), 2)
        self.assert_cli_error(support.run_cli("--port", "8080"), 2)

    def test_option_without_value_is_exit_2(self) -> None:
        self.assert_cli_error(support.run_cli("--config"), 2)
        config = self.write_config(self.valid_payload())
        self.assert_cli_error(support.run_cli("--config", config, "--port"), 2)
        self.assert_cli_error(
            support.run_cli("--config", config, "--port", "0", "--clock-file"), 2
        )

    def test_invalid_port_spellings_are_exit_2(self) -> None:
        config = self.write_config(self.valid_payload())
        for raw_port in ("-1", "+80", " 80", "1.5", "65536", "80 81", "", "abc"):
            with self.subTest(port=raw_port):
                self.assert_cli_error(
                    support.run_cli("--config", config, "--port", raw_port), 2
                )

    def test_port_zero_is_valid_and_ephemeral(self) -> None:
        config = self.write_config(self.valid_payload())
        gateway = support.GatewayProcess(config, port=0)
        try:
            self.assertIsInstance(gateway.port, int)
            self.assertGreater(gateway.port or 0, 0)
        finally:
            gateway.stop()

    def test_unicode_and_space_config_path_works(self) -> None:
        directory = os.path.join(self.tmp.path, "config dir", "cönfig")
        os.makedirs(directory)
        path = os.path.join(directory, "gateway config.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(self.valid_payload(), separators=(",", ":")) + "\n")
        gateway = support.GatewayProcess(path, port=0)
        try:
            status, _, body = support.http_request(gateway.port or 0, "GET", "/health")
            self.assertEqual(status, 200)
            self.assertEqual(body, b'{"status":"ok"}\n')
        finally:
            gateway.stop()

    # -- configuration validation (AC-11) ---------------------------------

    def test_ac11_invalid_configs_are_exit_2(self) -> None:
        cases: dict[str, object] = {
            "limit_true": {**self.valid_payload(), "limit": True},
            "upstream_port_zero": {**self.valid_payload(), "upstream_port": 0},
            "short_token": {**self.valid_payload(), "token": "short"},
            "extra_field": {**self.valid_payload(), "extra": 1},
            "missing_field": {
                k: v for k, v in self.valid_payload().items() if k != "limit"
            },
            "limit_float": {**self.valid_payload(), "limit": 2.0},
            "window_float": {**self.valid_payload(), "window_ms": 1000.0},
            "upstream_port_string": {**self.valid_payload(), "upstream_port": "9000"},
            "token_bad_chars": {**self.valid_payload(), "token": "demo token!"},
            "limit_too_high": {**self.valid_payload(), "limit": 1001},
            "window_too_low": {**self.valid_payload(), "window_ms": 0},
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                path = self.write_config(payload, name=f"{name}.json")
                self.assert_cli_error(
                    support.run_cli("--config", path, "--port", "0"), 2
                )

    def test_ac11_duplicate_keys_nan_and_infinity_are_exit_2(self) -> None:
        raw_cases = {
            "duplicate": b'{"upstream_port":9000,"token":"demo-token","limit":2,"limit":3,"window_ms":1000}\n',
            "nan": b'{"upstream_port":9000,"token":"demo-token","limit":NaN,"window_ms":1000}\n',
            "infinity": b'{"upstream_port":9000,"token":"demo-token","limit":2,"window_ms":Infinity}\n',
            "not_object": b"[1,2,3]\n",
            "bad_json": b'{"upstream_port":\n',
            "invalid_utf8": b'{"upstream_port":9000,"token":"demo\xff-token","limit":2,"window_ms":1000}\n',
        }
        for name, raw in raw_cases.items():
            with self.subTest(case=name):
                path = self.write_config(raw, raw=True, name=f"{name}.json")
                self.assert_cli_error(
                    support.run_cli("--config", path, "--port", "0"), 2
                )

    def test_config_with_utf8_bom_is_exit_2(self) -> None:
        payload = json.dumps(self.valid_payload()).encode("utf-8")
        path = self.write_config(b"\xef\xbb\xbf" + payload, raw=True)
        self.assert_cli_error(support.run_cli("--config", path, "--port", "0"), 2)

    def test_missing_or_unreadable_config_is_exit_3(self) -> None:
        missing = self.tmp.file("does-not-exist.json")
        self.assert_cli_error(support.run_cli("--config", missing, "--port", "0"), 3)
        self.assert_cli_error(
            support.run_cli("--config", self.tmp.path, "--port", "0"), 3
        )

    # -- bind failure ------------------------------------------------------

    def test_occupied_port_exits_1(self) -> None:
        import socket

        config = self.write_config(self.valid_payload())
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            result = support.run_cli("--config", config, "--port", str(port))
        finally:
            blocker.close()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.stderr.startswith(b"error:"))


if __name__ == "__main__":
    unittest.main()
