# UCSFOMOPAgent 0.3.0

Long database operations can now run as detached local jobs through MCP or CLI.
Submission returns immediately; agents monitor phase, heartbeat, row/byte counts,
elapsed time and advisory ETA. Results stream to private CSV/JSONL files and are
published as complete only on success. Cancellation, wall-clock and disk budgets,
worker-loss detection and explicit cleanup are supported. Credentials remain in
the connector/worker or OS keyring, outside tool arguments and job-status data.

The repository now includes a deterministic installable BRXT, lockfile, checksum,
bundled query-job skill, installation documentation, tests and release automation.
No-argument CLI invocation remains an MCP stdio server.

OMOP lab discovery now uses bounded, explicitly nonrandom samples separated by unit; exact coverage uses a monitored profiling query. Schema guidance, connection locking/cleanup, CSV output and error redaction were corrected. Sample results replace earlier full-population lab statistics; consumers must not interpret them as coverage estimates.

Validation includes offline lifecycle/security/domain tests and live small queries,
10,000-row real-table exports, 100,000-row generated transfers, parameterized
queries, EXPLAIN jobs, CLI monitoring after submit exits, cancellation, and the
BioRouter agent execution path with a deterministic local provider fixture.
Clinical tests selected constants from real tables, not patient details.

Jobs survive MCP/chat disconnects, not host shutdown. Interrupted exports require
explicit resubmission; arbitrary queries do not have automatic checkpoint/resume.
Network, permissions, database resource limits and disk exhaustion can still fail.
These limits are reported; this release does not promise every query can succeed.
