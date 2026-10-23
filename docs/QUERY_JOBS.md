# Query jobs and extension layout

The BRXT is a ZIP with manifest.json, README.md, pyproject.toml, uv.lock, LICENSE,
NOTICE, src/ucsfomopagent/ and skills/. BioRouter validates and extracts it, runs uv sync,
collects configured credentials through its trusted UI/CLI, and launches the
console entry point over MCP stdio. The manifest declares secret keys, package
version, entry point and bundled skills. Endpoint defaults are connector-specific;
CDW and OMOP must not accidentally reuse each other's database setting.

## Why this organization

Keep each connector independently installable and releasable. Database adapters
live in query_backend.py, orchestration in jobs.py/job_worker.py, and command
parsing in query_cli.py. Existing domain tools remain alongside them. This avoids
a new separately published dependency or any requirement to clone sibling repos.
OMOP and CDW keep jobs.py and job_worker.py identical. Their query adapters differ
only in connector defaults. Run the same contract tests in both repositories when
changing the protocol; the SPOKE connector has an independently released runtime.
A future shared package is appropriate once its API and release ownership settle.

`extensions/ucsfomopagent.brxt` is the current committed installable artifact. Build it
with `uv run python scripts/build_brxt.py`; input paths are allowlisted and ZIP
metadata is deterministic. Development tests, credentials, result data, virtual
environments and this repository's benchmark history never enter the bundle.
`extensions/SHA256SUMS` covers the bundle. GitHub releases publish both the stable
asset name and a versioned alias, plus SHA256SUMS. CI rebuilds and compares the
committed artifact so source and downloadable extension cannot drift silently.

## Execution and credentials

Both MCP submit and CLI submit use JobStore. The connector sends query parameters
and connection credentials to a detached Python worker over a private stdin pipe.
They are never arguments or job-status fields, and neither query text nor
parameters are saved in the job database. Credentials are resolved when starting
work, not returned to the language model. The worker owns its DB connection; a
separate supervisor thread of control updates status while the driver blocks.
Cancel or deadline closes the worker process, releasing its connection. The DB
may take additional time to notice disconnect and finish cancelling server work.

BioRouter uses its own credential store. Standalone `auth` uses the OS keyring
under service `ucsf-agent:ucsfomopagent`; it is deliberately not an undocumented reader
of BioRouter's internal secret format. Environment variables remain supported
for secret-manager injection. No plaintext credential fallback is implemented.
This prevents accidental disclosure through tools; it is not isolation from
malicious code running under the same OS account.

## Storage and progress

Jobs live in `~/.local/share/ucsf-agents/ucsfomopagent/` (override the parent using
UCSF_AGENT_JOB_ROOT). Directories are private (0700) and DB/results are 0600 on
POSIX. On Windows use the account's private profile directory and its ACLs.
SQLite serializes admission (two active jobs per connector). Results stream in
1000-row batches to a `.partial` file and are renamed only after success and
fsync. The default disk budget is 10 GB; exceeding it fails visibly rather than
silently truncating the dataset. JSONL preserves nulls; CSV encodes null as empty
and JSON-encodes graph/list values. Use JSONL when empty string and null must be
distinguished. SQL projections require unique column aliases. Exported text may
contain spreadsheet formula strings: treat it as data when importing CSV.

Status records queued/running/terminal states and separate connecting/planning/
executing/streaming phases,
rows written, bytes, heartbeat, elapsed time and advisory ETA. A heartbeat proves
the local supervisor is alive, not that the server is making progress. ETA is
unknown before a comparable prior query or usable caller row estimate exists;
we do not convert optimizer cost to fictitious wall-clock precision.

Workers survive MCP or chat disconnects. Reboot/crash is detected as interrupted
after a lost heartbeat and released worker lease; `list` reconciles stale records.
A stale heartbeat with a held lease is reported as stalled and does not free a job
slot or permit purging a live worker. They do not survive OS
termination or machine shutdown. Arbitrary SQL/Cypher cannot safely restart at a
row offset: explicit resubmission starts from the beginning. For very large
repeated exports use stable key/date partitions with a fixed snapshot strategy.
Finished and partial results remain local until explicitly purged:

```bash
uv run ucsfomopagent list
uv run ucsfomopagent status JOB_ID
uv run ucsfomopagent cancel JOB_ID
uv run ucsfomopagent purge JOB_ID
```

## MCP waiting, capacity and result handoff

Six namespace-prefixed job tools are available. `submit_query_job` starts work;
`query_job_status` is an immediate lookup; `cancel_query_job` requests cancellation.
`query_job_capacity` reports `active_jobs`, `max_active_jobs` (2), and
`available_slots`. Admission is checked atomically, so a later submission can
still return `capacity_exceeded` with current capacity. Waiting jobs hold their
slot until completion, failure or confirmed cancellation.

`wait_for_query_job(job_id, timeout_seconds=30)` waits natively for 0–30 seconds.
It returns a current status plus `wait_timed_out`; true means the wait budget ended
while the job remains active. Cancelling the MCP wait does not cancel the job.
Use another bounded wait or respect `recommended_poll_seconds` for immediate
status polls. Status lookups run off the MCP event loop with a short database
lock budget. If the wait expires while storage is busy after an active state
was observed, it returns that last state with `wait_timed_out=true`,
`wait_status_stale=true` and an explicit warning. If no state could be read,
it returns a transient `status_lookup_busy` lookup error without claiming an
operation outcome. A busy lookup never marks the job failed. No shell sleep,
fabricated progress or automatic resubmission is needed. Unknown ETA is normal
during connection, planning and execution.

`read_query_job_result(job_id, offset=0, max_bytes=8192)` reads only a completed
job's managed CSV/JSONL file. It accepts no path. It returns `text`, byte
`offset`, `bytes_read`, `total_bytes`, `eof` and `next_offset`. Limits are 4–65536
bytes per call; follow `next_offset` until `eof=true`. Chunk boundaries preserve
UTF-8 characters but can split rows or quoted CSV records. Assemble the desired
bounded content before parsing it. A mid-character offset is rejected.

The reader ignores saved `result_path` values and opens a fixed result name
relative to the managed job directory, using pinned-root identity, descriptor-
relative opens and no-follow flags. It rejects directory/file symlinks, hardlinks,
nonregular files and changed result sizes. POSIX secure-open primitives are
required. On unsupported platforms (including Windows) it fails closed with
`secure_reader_unavailable`; the existing local CLI result remains available.
This does not grant arbitrary filesystem access or isolate hostile code running
under the same OS account. Keep controlled data local and use bounded summaries
rather than placing complete patient-level exports in chat.

Failed jobs retain `error_details` with stable `code`, `kind`, sanitized `message`,
original failure `phase`, and boolean `retryable`. Driver messages and query
literals are not exposed. Retryability is advisory; resubmission remains explicit.
New submissions, status/wait responses and cancellation responses also carry:

```json
{"_meta":{"io.biorouter/operation":{"version":1,"id":"ucsfomopagent:JOB_ID","status":"failed","error":{"code":"query_rejected","kind":"invalid_args","message":"The database rejected the query. Check schema, projections and parameters.","phase":"executing","retryable":false}}}}
```

Allowed operation states are `queued`, `running`, `completed`, `failed` and
`cancelled`; job `timed_out` and `interrupted` map to operation `failed`. A status
lookup that finds a failed job still has MCP `isError=false`. Unknown-job,
admission and result-reader failures have `isError=true` and structured safe
errors, without changing the stored job outcome. Older failed records without
structured details receive a generic safe operation error.

## Credential-free app smoke fixture

From a source checkout, create a fresh empty absolute directory and run:

```bash
uv run python scripts/synthetic_job_server.py --root /absolute/fresh-empty-dir --delay-seconds 2
```

This stdio MCP server registers the actual six job tools with an isolated store.
It accepts only `SELECT 1 AS example` (three synthetic multilingual rows),
`SELECT 0 AS simulate_failure` (stable safe failure), and
`SELECT 2 AS simulate_slow` (bounded delayed success). No real database connection,
credential lookup or installed job store is used. An existing nonempty directory
is refused. Use `omop-submit_query_job`, wait/status, and the result reader to
exercise real MCP metadata and chunked handoff in a development app. Use separate
fresh roots for concurrent fixtures.

## Read-only and failure boundaries

Use database accounts with SELECT/read-only privileges. The shared query guard
rejects writes, stacked statements, procedure calls and external data sources;
T-SQL is additionally parsed as a single SELECT/UNION/EXCEPT/INTERSECT statement.
Cypher uses a conservative lexical guard. Neither replaces least-privilege roles. Advanced stored-procedure exports are
not supported. Query jobs preserve full returned records and never inherit the
interactive preview LIMIT. User-supplied LIMIT/TOP still applies.

Network interruptions, invalid queries, server resource limits, exhausted disk,
and revoked access can still fail. There is no promise that any query of any size
will succeed. Failures return stable categories; raw driver messages are withheld
because they can contain credentials, query literals or patient values.

## Development and release

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
uv build --wheel
uv run python scripts/build_brxt.py
# For the next release:
uv run python scripts/version.py X.Y.Z
uv lock
# Update release notes, rerun tests/build, commit the BRXT and checksum.
git tag vX.Y.Z
git push origin main vX.Y.Z
```

The release workflow tests and rebuilds before publishing. Live tests require
UCSF connectivity and authorized credentials; they are separate from credential-
free CI. Do not commit database rows, credentials or local export directories.

Graph exports preserve node labels/element IDs, relationship type/endpoints and
path topology as tagged JSON objects. Date/time and decimal values serialize as
strings. Memory scales with a batch plus the largest returned record: a giant
collect() or single field can still exhaust memory, so project scalar properties
and avoid giant aggregates for exports.

Standalone `auth --clear` removes the CLI profile. Explicit environment credentials
select that complete route instead of mixing in a saved username or passcode.

Opt-in live smoke test (requires authorized environment credentials and network):
`uv run python scripts/live_smoke.py`. It checks MCP registration, a small real-
table query and a monitored 10,000-row export, selecting constants to avoid
exporting patient details. CI does not use production credentials.

Security updates remove the former Intel-macOS cryptography<49 constraint.
Patched cryptography 50+ is required. If a compatible Intel macOS wheel is not
available, install the upstream Rust/OpenSSL build prerequisites or use a
supported Python/platform combination; do not restore a vulnerable version cap.
