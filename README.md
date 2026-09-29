# UCSFOMOPAgent

Current version: **0.3.1**. Small queries stay on MCP; long queries and full
exports run as monitored local jobs without tying up an MCP request.

## Install in BioRouter

1. Download **[ucsfomopagent.brxt](https://github.com/BaranziniLab/UCSFOMOPAgent/releases/latest/download/ucsfomopagent.brxt)**
   from [Releases](https://github.com/BaranziniLab/UCSFOMOPAgent/releases/latest).
   The same current bundle is committed under [extensions/](extensions/).
2. In BioRouter, open **Extensions → Add extension**, select the BRXT and install.
   BioRouter creates the Python environment; `uv` and Python 3.11+ are required.
3. Enter credentials in BioRouter's own configuration dialog, never in chat.
4. Enable the extension in your chat. Verify a small query before a larger export.

Terminal installation uses the same installer:

```bash
biorouter extension install ./extensions/ucsfomopagent.brxt
biorouter extension configure ucsfomopagent
```

Configure `CLINICAL_RECORDS_USERNAME` and `CLINICAL_RECORDS_PASSWORD`. Host defaults to the UCSF SQL Server; database defaults to `OMOP_DEID`. Optional `CLINICAL_RECORDS_SERVER` / `CLINICAL_RECORDS_DATABASE` overrides are declared in the manifest. UCSF network/VPN access and database read permission are required.

The Desktop installer discovers bundled `skills/*/SKILL.md`. If using a BioRouter
CLI version that does not copy bundled skills, the MCP server still provides the
job-routing instructions; the skill folders can also be installed separately.

## Long queries, CLI and progress

Use `omop-submit_query_job` for an export or a query that could exceed the
interactive timeout. It returns a job ID immediately. Poll
`omop-query_job_status` at its recommended interval; use
`omop-cancel_query_job` to stop. Only `completed` means the file is complete.
Status includes rows, bytes, elapsed time, phase and an advisory ETA when known.
Set `mode="explain"` to obtain a plan without running the query.

From a source checkout, or BioRouter's installed extension directory:

```bash
uv sync --locked
# Standalone CLI only: configure its OS-keyring profile interactively once.
# BioRouter MCP jobs already receive credentials and do not need this command.
uv run ucsfomopagent auth
uv run ucsfomopagent submit --query-file query.sql --format jsonl --timeout-seconds 3600
uv run ucsfomopagent watch JOB_ID
```

No-argument `uv run ucsfomopagent` continues to start the MCP server. `status`, `watch`,
`list`, `cancel` and `purge` need no database credentials. Results remain in the
local private job directory and are not sent to chat. Database/server limits can
still fail a query; jobs report those failures instead of silently truncating.

See [architecture, storage, security and release details](docs/QUERY_JOBS.md).
To rebuild the tracked bundle: `uv run python scripts/build_brxt.py`.


An MCP (Model Context Protocol) server for querying the **UCSF OMOP** de-identified
electronic health records database (OMOP CDM v5.4 on Microsoft SQL Server) for
fast, robust clinical data retrieval.

> **v0.2.0** is a major upgrade focused on getting the LLM to the *right* data
> *faster* and *more reliably*, while staying robust to database changes. See
> [`benchmark/CHANGELOG.md`](benchmark/CHANGELOG.md) for the full engineering log
> and [`benchmark/`](benchmark/) for the reproducible evaluation harness.


## Database-specific behavior

The server supplies T-SQL and OMOP conventions, cached live schema discovery,
concept resolution and a lab/vital finder. `find_measurement` now examines bounded
numeric samples per candidate and unit. These are nonrandom samples, not complete
coverage counts or population estimates. It returns a full profiling query for a
monitored job when exact statistics are needed. Never mix units without an
explicit conversion plan. Live schema takes precedence over historical counts.

Interactive queries use a serialized reusable connection, autocommit, a short
preview timeout, valid CSV quoting and result caps. Full exports use independent
job connections and do not inherit preview caps. Failed connections are discarded.

## Features

- **Query UCSF OMOP**: read-only T-SQL on the de-identified OMOP CDM.
- **Concept & lab resolution**: built-in vocabulary search and a lab finder so
  the model stops guessing concept_ids.
- **Schema introspection**: live, drift-resistant.
- **Pre-configured**: server/database baked in — just provide credentials.

## Installation

### From GitHub (using uvx)

```bash
uvx --from git+https://github.com/BaranziniLab/UCSFOMOPAgent ucsfomopagent
```


## Evaluation

`benchmark/` contains the reproducible evaluation used to drive these
improvements: a harness that drives the **real** MCP server (over stdio) with a
fixed neutral system prompt, a 100-question bench across four difficulty tiers,
and an LLM-judge grader. Absolute patient counts are redacted (this repo is
public); the methodology, relative improvements, and efficiency metrics
(iterations / tool calls / tokens / latency) are included. See
[`benchmark/README.md`](benchmark/README.md).

## Security

User SQL must parse as one read-only SELECT/WITH query, and unsafe operations
are rejected. Use a database account restricted to SELECT; validation supplements
server permissions. Credentials are provided via environment variables (stored in the OS
keyring by BioRouter) and are never logged or committed.
