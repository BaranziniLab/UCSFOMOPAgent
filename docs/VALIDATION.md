# UCSFOMOPAgent 0.3.0 validation

Validated on macOS ARM64 with Python 3.13 and BioRouter 1.91.2 on 2026-09-28.

- Offline suite: 23 tests, one expected skip for the other database adapter.
- Wheel build, deterministic BRXT construction, skill validation, and diff checks pass.
- Real database connectivity and existing MCP tools return results.
- Real-table background export: 10,000 rows completed and file row count verified.
- Generated read-only transfer: 100,000 rows completed through standalone CLI in
  approximately 1.22 seconds in this environment. This is a transport/streaming
  check, not a benchmark of large clinical joins or graph traversals.
- Submit process exits before completion; monitoring works with credential variables
  removed. Cancellation reaches cancelled and never publishes a completed file.
- Named parameters including an apostrophe round-trip correctly; plan-only jobs pass.
- Installed the exact BRXT with `biorouter extension install`, then verified tool
  discovery and successful small queries plus 10,000-row monitored jobs through
  BioRouter's normal agent loop using a deterministic local provider fixture.
  This verifies integration and privacy-aware dispatch, not an LLM reasoning benchmark.

OMOP schema 0.56 s; concept search 0.40 s; bounded lab discovery 0.64 s.

Clinical smoke tests select constants from actual tables rather than patient
attributes. No credentials or clinical records are included in this repository,
release artifacts or report. Tests use local authorized credentials, which CI does
not have. Production availability and arbitrary query performance remain bounded
by database permissions, plans, network conditions, server limits and disk space.

Reproduce credential-free checks with `uv run python -m unittest discover -s tests`.
For authorized live checks, inject credentials from a secret manager and run
`uv run python scripts/live_smoke.py`. Returned row totals and timings are logged;
row contents and credentials are not.

After refreshing the dependency locks and requiring cryptography 50+, pip-audit
reported no known vulnerabilities in the resolved dependency set on this test
platform. The former Intel macOS cryptography<49 cap was removed.
