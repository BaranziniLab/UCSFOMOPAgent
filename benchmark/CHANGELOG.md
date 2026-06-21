# UCSFOMOPAgent improvement changelog (v0.1.0 → v0.2.0)

> **Note on numbers:** this repository is public, so absolute patient/cohort
> counts and ground-truth values are redacted (shown as `‹count›`). Efficiency
> metrics (iterations, tool calls, tokens, latency) and *relative* correctness
> changes are kept, since those carry the engineering story without exposing
> internal database statistics.

All changes live in the BioRouter **extension** source (`src/ucsfomopagent/server.py`)
so the real BioRouter agent picks them up identically — BioRouter surfaces an MCP
server's `instructions` and tool descriptions into the agent system prompt. The
evaluation harness (`benchmark/harness/`) is a neutral measurement driver only;
no agent behavior lives in it, and its system prompt is fixed across all runs so
measured gains come from the extension.

Baseline (v0) = the original 226-line server with 2 tools (`query_ucsf_omop`,
`list_ucsf_omop_tables`) and **zero** injected context.

Per-question metrics: iterations (LLM turns), tool_calls, total_tokens
(prompt+completion+reasoning), wall seconds, and an LLM-judged verdict
(CORRECT / PARTIAL / WRONG) against ground truth computed directly against the DB.

---

## Fix #1 — Foundational context + concept/schema tools + speed (after Batch 1, Q1–10)

### What Batch 1 revealed (v0, simple counts)
Correctness was already 100% on simple counts, but the agent was **inefficient
and fragile**: it wasted its first tool call on `list_ucsf_omop_tables` on nearly
every question just to learn tables live in the `omop` schema; one demographic
question burned 5 tool calls / ~10.5k tokens because it wrote `LIMIT` (Postgres
syntax) → SQL Server error → retried with `TOP`, and its concept search
`LIKE '%hispanic%'` returned junk (a *plant*, "Crambe hispanica") for lack of a
domain/standard filter; and every tool call opened a new DB connection.

### Root cause
The agent was handed a raw SQL pipe with no knowledge of (a) the SQL dialect
(MS SQL Server / T-SQL), (b) the schema, (c) OMOP conventions, or (d) the
vocabulary — so it rediscovered all of it by trial and error every session.

### Changes (all in `server.py`)
1. **Rich `instructions` injected via FastMCP** (server_instructions_len 0 →
   ~4.8k chars): T-SQL dialect (`TOP` not `LIMIT`, median/window syntax), the
   `omop` default schema, OMOP CDM essentials (`*_concept_id` → `concept` join,
   `concept_ancestor` for disease/drug-class cohorts, `drug_era` for ingredient
   exposure, `COUNT(DISTINCT person_id)` for cohorts), demographic concept_ids
   with the high "Unknown" rates flagged, the large-`measurement`-table rule,
   de-id date-shift caveats, and the empty tables/columns to avoid.
2. **New tool `search_concepts`** — ranked vocabulary lookup returning
   concept_id, name, domain, vocabulary, class, standard flag, code, and
   `descendant_count`. Eliminates concept guessing and source_value LIKE-scans.
3. **New tool `get_omop_schema`** — live introspection: tables + row counts
   (no args) or a table's columns. Robust to UCSF schema drift.
   `list_ucsf_omop_tables` kept as a thin alias.
4. **Speed**: one pooled DB connection reused across tool calls (health-checked,
   auto-reconnect) instead of connect-per-query; schema/column caching; result
   payloads capped with an "aggregate instead" message.
5. **Robustness**: server/database/schema overridable by env; self-healing error
   messages (detects `LIMIT`, `Invalid object/column name` and tells the agent
   how to fix the query).

### Effect (Tier-2 spot-check, "T2DM + essential hypertension")
v1 resolves both concepts with `search_concepts`, expands with `concept_ancestor`,
and returns the correct count in 3 tool calls, stating the concept_ids used —
behavior the original agent reached (if at all) only after many exploratory
queries.

---

## Fix #2 — Tokenized concept search, lab finder, dialect/validator bugs (after Q21–40)

### What Q21–40 revealed (Tier-2 cohorts on v1)
Weighted 65% (10 CORRECT / 6 PARTIAL / 4 WRONG; avg ~24k tokens, ~50s). Causes:
- **Validator false-positive (bug):** a query beginning with a `-- comment`
  failed the `startswith('SELECT')` check and was rejected as non-read-only —
  wasting 2–3 iterations per affected question.
- **Whole-phrase concept search missed:** `LIKE '%malignant breast cancer%'` and
  `'%cerebral infarction ischemic stroke%'` matched nothing, forcing reworded
  retries; one ended on too-narrow a concept (a large undercount).
- **Lab/measurement trap:** the nominally-standard HbA1c concept (SNOMED) has NO
  `value_as_number`; values live on LOINC concepts. The agent ancestor-expanded
  measurement concepts (0 rows) then flailed 9–14 tool calls. Ancestor-joins on
  the very large measurement table ran 38–178s, while a direct
  `measurement_concept_id IN (loinc ids)` filter was 0.5s.
- **Drug-class cohorts ("any insulin/statin"):** slow `IN (SELECT … LIKE
  '%insulin%')` subqueries (~50s) instead of a `concept_ancestor` join.

### Changes (all in `server.py`)
1. **Validator tolerates leading comments** (`_strip_leading_comments`): strips
   `--` and `/* */` prefixes before the SELECT/WITH check; allows one trailing `;`.
2. **`search_concepts` upgraded**: tokenized AND matching (every word must
   appear, any order → "malignant breast cancer" matches "Malignant neoplasm of
   breast"), plus exact `concept_code` matching (a raw code like `4548-4` resolves).
3. **New tool `find_measurement(name)`**: finds Measurement-domain concepts by
   name, then does ONE indexed pass over `measurement` to report per concept the
   patient count, `% numeric`, and value range — ranked so value-bearing (LOINC)
   concepts come first.
4. **Instructions updated**: labs → use `find_measurement` and filter
   `measurement` directly by concept_id (never ancestor-expand; SNOMED-has-no-value
   trap spelled out); drug CLASS cohorts → resolve the class concept and JOIN
   `concept_ancestor` (slow `IN (SELECT … LIKE)` anti-pattern called out); queries
   may start with a comment.

### Effect (re-run of the two worst Q21–40 cases)
- "Any insulin": PARTIAL → **exact** ground truth, via the ATC class + concept_ancestor.
- "T2DM with HbA1c ≥ 7": was 14 tool calls / 77k tokens / 248s of flailing →
  **exact** ground truth, resolving the right LOINC concept via find_measurement.

---

## Fix #3 — Decisive lab finder + cohort×lab query pattern (after Q41–60)

### What Q41–60 revealed (Tier-2 on v2)
Weighted 67.5% (12 CORRECT / 3 PARTIAL / 5 WRONG). Simple cohorts were now cheap
(~3 calls / ~14k tokens), but **lab-threshold and cohort×lab questions were still
expensive and sometimes very wrong**:
- "LDL > 190": a ~70× undercount while burning **120,983 tokens / 7 calls** —
  `find_measurement` returned several LDL concepts and the agent picked rare/empty
  sub-concepts (one had 0 rows), then re-queried distributions for 5 more calls.
- "CKD + serum potassium": **223s** — the agent's first query was a 4-table join
  (condition_occurrence × concept_ancestor × measurement) that timed out; a CTE
  retry worked in 8s.

### Changes (all in `server.py`)
1. **`find_measurement` is now decisive.** It returns `recommended_concept_ids`
   (the value-bearing concepts covering ~99% of the lab's numeric rows, ready as
   an `IN(...)` list), the `dominant_unit`, and a hint to use them verbatim and
   NOT re-query the distribution — killing the "pick one rare sub-concept → tiny
   wrong count → re-explore" failure mode.
2. **Instructions: cohort + lab pattern.** Use a CTE for the cohort then filter
   `measurement` by `recommended_concept_ids` — never a 4-table join across the
   very large measurement table.

### Effect (re-run of the two worst Q41–60 cases)
- "LDL > 190": ~70× undercount → correct value, with **120,983 → 35,064 tokens**
  (3.5× cheaper).
- "CKD + serum potassium > 5.5": PARTIAL → **exact** ground truth, in 4 calls.

### Note on residual PARTIAL/WRONG verdicts
Several remaining non-CORRECT verdicts are sub-2% differences from ground truth
driven by *defensible concept-definition choices* (which descendant set / which
LOINC sub-concepts / age reference-year boundary), not agent errors — e.g. one
case was within 0.2%. The agent states its concept_ids and assumptions, so these
are reproducible and auditable. The LLM judge scores them strictly.

---

## Fix #4 — Efficiency & assumption-clarity for complex/open-ended questions (after Q61–100)

### What Tier 3/4 revealed (v3)
Weighted 73.8% (23 CORRECT / 13 PARTIAL / 4 WRONG) — strong for the hardest
tiers. The remaining non-CORRECT cases were **definitional, not bugs**:
distribution questions where bin boundaries / age reference-year differ from
ground truth, an open-ended cohort defined more strictly than the rubric, and one
transient MiMo 429. The genuine, generalizable issue was **cost**: open-ended
Tier-4 questions over-explored (enumerating alternatives, re-running queries to
reformat).

### Changes (`server.py` instructions)
- Complex/multi-step: plan first, resolve all concept_ids up front, then one
  combined query (CTEs ok); never re-run a query just to reformat.
- Distribution questions: state bin definitions + reference year and produce the
  whole distribution in a single GROUP BY.
- Open-ended cohort/diagnosis: form ONE hypothesis, size it with 1–3 targeted
  queries, report and stop; offer variants in prose, not extra queries.

---

## v0 → v0.2.0 regression (same 12 representative questions)

Identical question set run against the original v0 server and the final v0.2.0
server (LLM-judge verdicts; counts redacted):

| Metric              | v0 (original) | v0.2.0 (final) |
|---------------------|--------------:|---------------:|
| Weighted score      |        58.3%  |     **75.0%**  |
| CORRECT             |            5  |          **8** |
| PARTIAL             |            4  |              2 |
| WRONG               |            3  |              2 |
| avg iterations      |          6.2  |        **3.8** |
| avg tool calls      |          5.8  |        **3.2** |
| avg tokens          |       24,915  |     **20,662** |
| avg wall seconds    |         65.6  |       **30.4** |

Net: **+16.7 pts correctness, ~45% fewer tool calls, ~2× faster**, with fewer
tokens despite the added context. The original made a catastrophic lab-trap error
(off by ~5×) that v0.2.0 fixes.

### Honest caveats
- Improvement is in aggregate, not uniform per-question. MiMo at temperature 0.1
  is non-deterministic: one lab question landed on a wrong LDL sub-concept on the
  final run even though `find_measurement` recommends the right one (the model
  occasionally ignores the recommendation), and one oncology question lands on a
  narrower concept than the chosen ground truth.
- Several "WRONG/PARTIAL" verdicts reflect defensible concept-definition choices
  and strict LLM-judge grading on open-ended rubrics rather than agent errors;
  the agent states its concept_ids and assumptions so results are auditable.
