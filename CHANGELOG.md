# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-13

Initial release implementing spec 0.1.4.

### Added

- `serve` entrypoint, executable and runnable as `python serve`, with `--version`,
  `--help`, `--config`, `--port` and `--clock-file`.
- Strict JSON configuration validation: exact fields, strict integers,
  duplicate keys and non-finite numbers rejected, UTF-8 without BOM.
- `GET /health` returning `{"status":"ok"}` without authentication or quota.
- `GET /proxy` forwarding to a loopback upstream `/` with the same query,
  a 1-second timeout, a 64 KiB response cap and no redirect following.
- Static `Authorization: Bearer TOKEN` check; the header is never forwarded and
  never logged.
- Fixed-window, per-process rate limiting with atomic slot reservation and
  injectable millisecond clock via `--clock-file`.
- Controlled local JSON errors: `not_found`, `method_not_allowed`, `unauthorized`,
  `clock_error`, `rate_limited`, `upstream_error`, `upstream_too_large`.
- Public black-box test suite in `tests/` traceable to AC-1..AC-14.
- CI workflow for Ubuntu and macOS on Python 3.12.
