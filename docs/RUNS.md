# Run record

This file records only metadata observable by the implementation agent. Fields
the agent could not observe are recorded as `null` rather than invented.

## Run 1 — implementation of issue #1

| Field | Value |
| --- | --- |
| Stage | implementation + public tests |
| Issue | #1 (spec 0.1.4, product 0.1.0) |
| Spec hash | `null` (not provided to the agent) |
| Judge hash | `null` (hidden tests are external) |
| Requested model | `deepseek-flash` |
| Observed model | `deepseek-flash` (as reported by the execution environment) |
| Tokens / cost | `null` (not exposed to the agent) |
| Turns / correction rounds | not instrumented |
| Stop reason | task completion |
| Human interventions | `null` (not recorded by the agent) |
| Verdicts | local public suite only; no external verdict claimed |
| External verification | none available to the agent |
| Platform of local run | Linux |
| Declared CI targets | Ubuntu and macOS, Python 3.12, `PYTHONUTF8=1` |

Raw local results: `python -m unittest discover -s tests -v` passed 33 tests on
the Linux worktree. This does not establish macOS results; CI is the completion
gate.

Pending maintainer actions after merge (per spec §7.7): tag and release
`v0.1.0`, and any external publication.
