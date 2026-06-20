# UCSFOMOPAgent evaluation harness

This directory contains the reproducible evaluation that drove the v0.1.0 → v0.2.0
improvements. It measures the **extension** (tools + injected instructions), not a
bespoke agent: the harness drives the *real* `ucsfomopagent` MCP server over stdio
with a **fixed, neutral system prompt**, so any measured gain comes from changes to
`src/ucsfomopagent/server.py`.

> Absolute patient/cohort counts and ground-truth values are redacted from this
> public repo. What's published: the methodology, the question set (without answer
> values), and per-question efficiency + verdict metrics.

## How it works

```
question ──► MiMo (mimo-v2.5-pro)  ◄──tools── ucsfomopagent MCP server (stdio) ──► OMOP DB
                  │  fixed neutral system prompt + the server's own `instructions`
                  ▼
            agent loop (tool calls) ──► final answer ──► LLM-judge vs ground truth
```

- `harness/agent_runner.py` — launches the real MCP server, lists its tools,
  reads its `instructions`, and runs the MiMo tool-calling loop. Records
  iterations, tool calls, tokens (prompt+completion+reasoning), DB time, wall
  time, and the full transcript. Fixed system prompt; OMOP knowledge comes only
  from the extension.
- `harness/run_batch.py` — runs a range of questions, saves per-question records,
  prints a compact metrics table.
- `harness/grade.py` — LLM-judge (MiMo) comparing the agent's answer to the
  ground-truth value / rubric → CORRECT / PARTIAL / WRONG.
- `harness/profile_schema.py` — one-shot DB structure profiler.
- `harness/db.py` — DB connection helper (reads credentials from the OS keyring;
  no secrets in code).

## Question set (`questions.json`)

100 questions across four difficulty tiers:

| Tier | IDs | Kind |
|------|-----|------|
| 1 | 1–30 | Simple single counts / distributions |
| 2 | 31–60 | Multi-filter cohorts, time windows, lab thresholds |
| 3 | 61–85 | Complex multi-step distributions / fractions across a cohort |
| 4 | 86–100 | Open-ended diagnosis inference / cohort matching (rubric-graded) |

Each question carries the validation SQL (which contains only public OMOP
concept_ids, no counts) and, for Tier 4, a grading rubric. Ground-truth values
were computed directly against the DB and are **redacted here**.

## Results (`results/metrics.csv`)

Per-question metrics — `batch, agent_version, id, tier, verdict, iterations,
tool_calls, total_tokens, wall_seconds`. No answers or counts. See
[`CHANGELOG.md`](CHANGELOG.md) for the batch-by-batch findings and the fixes they
drove.

## Reproducing

Requires: the `ucsfomopagent` extension installed/available, UCSF DB credentials
in the keyring, and a MiMo (`xiaomi_mimo`) API key. Then:

```bash
cd benchmark/harness
uv run --with mcp --with httpx --with pymssql python run_batch.py ../questions.json myrun 1 10
uv run --with httpx python grade.py myrun
```
