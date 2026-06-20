# UCSFOMOPAgent

An MCP (Model Context Protocol) server for querying the **UCSF OMOP** de-identified
electronic health records database (OMOP CDM v5.4 on Microsoft SQL Server) for
fast, robust clinical data retrieval.

> **v0.2.0** is a major upgrade focused on getting the LLM to the *right* data
> *faster* and *more reliably*, while staying robust to database changes. See
> [`benchmark/CHANGELOG.md`](benchmark/CHANGELOG.md) for the full engineering log
> and [`benchmark/`](benchmark/) for the reproducible evaluation harness.

## BioRouter Extension

**[Download ucsfomopagent.brxt](https://github.com/BaranziniLab/UCSFOMOPAgent/releases/latest/download/ucsfomopagent.brxt)**

Drag the `.brxt` file into BioRouter's **Extensions → Add extension** dialog.
BioRouter installs the virtual environment automatically and prompts for
credentials.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CLINICAL_RECORDS_USERNAME` | ✅ | — | UCSF network username |
| `CLINICAL_RECORDS_PASSWORD` | ✅ | — | UCSF network password |
| `OMOP_LOG_LEVEL` | optional | `INFO` | Logging level |
| `OMOP_SCHEMA` | optional | `omop` | Default DB schema for OMOP tables |
| `CLINICAL_RECORDS_SERVER` | optional | (UCSF default) | Override DB host (migration) |
| `CLINICAL_RECORDS_DATABASE` | optional | `OMOP_DEID` | Override DB name (migration) |

## What's new in v0.2.0

The original agent exposed a raw SQL pipe and a table-lister with **no** context,
so the LLM rediscovered the SQL dialect, schema, OMOP conventions, and vocabulary
by trial and error every session — burning tokens and iterations, and sometimes
landing on wrong concepts. v0.2.0 fixes this entirely inside the extension:

### Injected context (surfaced into the agent's system prompt)
The server now ships a rich `instructions` block covering: the Microsoft SQL
Server dialect (`TOP` not `LIMIT`, window/median syntax), the `omop` default
schema, OMOP CDM essentials (the `*_concept_id` → `concept` join, `concept_ancestor`
for disease/drug-class cohorts, `drug_era` for ingredient exposure,
`COUNT(DISTINCT person_id)` for cohorts), demographic concept_ids with the high
"Unknown" rates flagged, the (very large) `measurement` rule (always filter by
concept_id), de-identification date-shift caveats, and the list of empty
tables/columns to never query.

### Tools (2 → 5)
| Tool | Purpose |
|------|---------|
| `query_ucsf_omop` | Read-only T-SQL query. Now reuses a pooled connection, caps result rows, tolerates leading comments, and returns **self-healing** error hints (e.g. `LIMIT`→`TOP`, unknown table/column → "check `get_omop_schema`"). |
| `search_concepts` | Resolve a clinical term to ranked OMOP `concept_id`s. **Tokenized** matching ("malignant breast cancer" → "Malignant neoplasm of breast"), `concept_code` lookup, and `descendant_count` for hierarchy expansion. |
| `find_measurement` | Lab/vital finder. Returns `recommended_concept_ids` (the value-bearing LOINC concepts that cover the lab, ready for `IN(...)`), the dominant unit, patient counts, and value ranges — in one call. Solves the "standard concept has no value" trap. |
| `get_omop_schema` | Live schema introspection: tables + row counts (no args) or a table's columns. Read live → robust to UCSF schema drift. |
| `list_ucsf_omop_tables` | Back-compat alias of `get_omop_schema`. |

### Speed & robustness
- One pooled DB connection reused across calls (health-checked, auto-reconnect),
  instead of connect-per-query.
- Schema/column introspection cached in-process; large result sets capped.
- Server/database/schema overridable by env (migration-safe); live introspection
  rather than hardcoded structure.

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

### Build the `.brxt`

```bash
zip -r ../ucsfomopagent.brxt manifest.json README.md pyproject.toml src/ skills/ \
  -x '*/__pycache__/*' '*.pyc'
```

(Exclude `.venv/`, `__pycache__/`, and the `benchmark/` directory — the bundle
needs only `manifest.json`, `README.md`, `pyproject.toml`, `src/`, and `skills/`.)

## Evaluation

`benchmark/` contains the reproducible evaluation used to drive these
improvements: a harness that drives the **real** MCP server (over stdio) with a
fixed neutral system prompt, a 100-question bench across four difficulty tiers,
and an LLM-judge grader. Absolute patient counts are redacted (this repo is
public); the methodology, relative improvements, and efficiency metrics
(iterations / tool calls / tokens / latency) are included. See
[`benchmark/README.md`](benchmark/README.md).

## Security

All queries are validated read-only (SELECT/WITH only; no DML/DDL; no stacked
statements). Credentials are provided via environment variables (stored in the OS
keyring by BioRouter) and are never logged or committed.
