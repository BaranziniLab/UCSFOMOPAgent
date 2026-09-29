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
The three small job-runtime modules intentionally have identical source; when
changing the protocol, run the same contract tests in all three repositories.
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
