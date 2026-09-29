---
name: ucsfomopagent-query-jobs
description: Plan, submit and monitor long read-only SQL Server queries and complete local exports through UCSFOMOPAgent; use after timeouts or when result size or query cost is uncertain.
---

Use existing schema/concept/entity tools for discovery and small previews. Resolve
identifiers before joining or traversing. For full exports, unbounded scans, or a
previous timeout, call `omop-submit_query_job`; it launches the CLI worker in
the background and returns a job ID immediately. Credentials are injected by
BioRouter. Never ask the user to paste credentials into chat or shell arguments.

1. Establish filters and projections. Prefer indexed anchors; avoid SELECT * and
   expensive counts solely for sizing. SQL Server parameters use `%(name)s`;
   Neo4j parameters use `$name`. Supply them in the parameters object.
2. If cost is uncertain, submit `mode="explain"` first and inspect the completed
   local plan. It does not execute the query. SQL Server SHOWPLAN may require
   additional permission; lack of a plan is not evidence of a cheap query.
3. Submit the query once. Set an explicit timeout budget (default 3600 seconds,
   maximum 86400) and a disk budget (`max_bytes`, default 10 GB). Do not run more
   than two simultaneous jobs per connector. Add `expected_rows` only if already
   known or reasonably estimated, and tell the user it is an estimate.
4. Poll `omop-query_job_status` using its `recommended_poll_seconds` (5–60).
   Status is immediate and contains phase, elapsed time, rows, bytes and heartbeat.
   Report unknown ETA honestly during blocking execution. Historical-query and
   streaming-rate estimates are advisory; changing load invalidates them.
5. Only `completed` supplies a complete `result_path`. Process CSV/JSONL locally;
   send summaries to chat. Exports do not silently apply an MCP preview row cap.
6. On `failed`, `timed_out` or `interrupted`, inspect the cause and revise the plan
   before a new submission. Partial files are not complete datasets. There is no
   automatic retry, keyset checkpoint, or safe general-purpose query resume.
   Use `omop-cancel_query_job` for unwanted work, then poll to terminal state.

CLI status needs no credentials: from the installed extension directory, run
`uv run ucsfomopagent status JOB_ID` or `uv run ucsfomopagent watch JOB_ID`.
A user running standalone CLI submissions can configure its separate OS-keyring
profile once with `uv run ucsfomopagent auth`; BioRouter submissions already use its own
credential store and do not require that step. Never dump either credential store.
