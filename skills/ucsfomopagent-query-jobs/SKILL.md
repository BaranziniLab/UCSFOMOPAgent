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

## Staged extraction and monitoring

For large fact-table studies, freeze the seed cohort and clinical definition as
described in `omop-phenotype-query`. Maintain a private batch manifest with the
cohort/query version, domain, disjoint batch membership, job ID, status, output
path, row count and file checksum. This is an explicit local workflow; the job
service does not automatically schedule, checkpoint or resume a study. Keep at
most two jobs active. Do not substitute OFFSET pagination for stable membership.

Set a pilot observation budget before scaling. If execution produces no rows
beyond that budget, inspect the plan or cancel and redesign; confirm terminal
status before replacement. A healthy heartbeat alone does not justify extending
the timeout. Zero rows streamed by a running job does not mean an empty result.

Report completed batches separately from extracted rows and eligible patients.
Estimate remaining wall time only from comparable completed batches, accounting
for concurrency and variability, and label it provisional. Do not infer total
rows from the first batch. Retry only failed or incomplete batches, retain prior
complete outputs and record which attempt is authoritative to avoid double-counting.
Declare the study complete only when every required batch is reconciled and the
frozen eligibility checks pass. Never relax clinical filters merely for speed.

CLI status needs no credentials: from the installed extension directory, run
`uv run ucsfomopagent status JOB_ID` or `uv run ucsfomopagent watch JOB_ID`.
A user running standalone CLI submissions can configure its separate OS-keyring
profile once with `uv run ucsfomopagent auth`; BioRouter submissions already use its own
credential store and do not require that step. Never dump either credential store.

In BioRouter's JavaScript code tool, use namespace access for hyphenated MCP names:

```javascript
import * as omop from "ucsfomopagent";
const status = omop["omop-query_job_status"]({job_id: "JOB_ID"});
record_result(status);
```

A hyphenated name is not a JavaScript identifier and cannot appear in a named
import. Pass only fields declared by the tool schema; remove local bookkeeping
keys from argument objects. Keep file writes and database calls in small separate
batches so one failing discovery call does not discard all prior results. Each
query must be one read-only SELECT/CTE; use VALUES in a CTE rather than DECLARE,
INSERT, temporary tables, or multiple statements.
