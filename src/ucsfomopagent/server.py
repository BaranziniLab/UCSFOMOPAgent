"""
UCSFOMOPAgent - UCSF OMOP Clinical Database MCP Server

An MCP server for querying the UCSF OMOP electronic health records database
(OMOP CDM v5.4 on Microsoft SQL Server) for rapid clinical data retrieval.

Design notes (see CHANGELOG in the improvement project):
- The model is given rich OMOP/UCSF context via the FastMCP `instructions`
  field (BioRouter surfaces this into the agent system prompt) plus a
  concept-search tool and a schema-introspection tool, so it stops
  rediscovering the database by trial and error.
- All knowledge is *grounded* in live introspection where possible
  (get_omop_schema) so the agent degrades gracefully if UCSF changes the
  schema; the static guidance is limited to stable, factual orientation.
- A single pooled connection is reused across tool calls for speed.
"""
import json
import logging
import os
import re
import sys
import threading
import time
from typing import Any, Literal, Optional

import pymssql
from fastmcp.exceptions import ToolError
from fastmcp.server import FastMCP
from fastmcp.tools.tool import ToolResult, TextContent
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

logger = logging.getLogger("UCSFOMOPAgent")

# Hardcoded UCSF OMOP defaults (overridable by env for robustness / migration).
OMOP_SERVER = os.getenv("CLINICAL_RECORDS_SERVER", "QCDIDDWDB001.ucsfmedicalcenter.org")
OMOP_DATABASE = os.getenv("CLINICAL_RECORDS_DATABASE", "OMOP_DEID")
OMOP_SCHEMA = os.getenv("OMOP_SCHEMA", "omop")  # default schema; tables resolve unqualified

MAX_RESULT_ROWS = int(os.getenv("OMOP_MAX_RESULT_ROWS", "2000"))  # cap payloads


# ---------------------------------------------------------------------------
# Agent-facing orientation. This is the single highest-leverage change: it is
# surfaced into the agent's system prompt by BioRouter (extension instructions),
# so the model knows the dialect, schema, conventions, data shape and pitfalls
# BEFORE it writes a single query. Keep it factual and stable; anything that can
# drift should be discovered live via get_omop_schema.
# ---------------------------------------------------------------------------
OMOP_INSTRUCTIONS = """\
You are querying the **UCSF OMOP de-identified EHR database** (OMOP CDM v5.4) on
**Microsoft SQL Server**. It holds **7.17 million patients**. Access is READ-ONLY.

## How to be fast and correct (read this first)
1. Resolve clinical terms to concept_ids BEFORE writing SQL — never guess
   concept_ids and never LIKE-scan `*_source_value` on the big event tables:
   - Diseases / drugs / procedures → `search_concepts`.
   - **Labs / vitals (anything in the `measurement` table) → `find_measurement`**
     (it returns the concept_ids that actually carry values + their ranges).
2. Use `get_omop_schema` if unsure of a table's columns (read live — trust it).
3. Then write ONE well-targeted query with `query_ucsf_omop`. You MAY start a
   query with a `-- comment`.

## SQL dialect (Microsoft SQL Server / T-SQL) — common mistakes
- Row limiting: use `SELECT TOP 100 ...`. **There is no `LIMIT`** (it errors).
- For "top N per group" use `ROW_NUMBER() OVER (...)`. Median: `PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x) OVER ()`.
- String concat is `+`; current date is `GETDATE()`; date math is `DATEDIFF(year,a,b)` / `DATEADD(...)`.
- Tables are in the `omop` schema, which is the default — reference them
  UNQUALIFIED (e.g. `FROM person`, `FROM condition_occurrence`).

## OMOP data model essentials
- One row per patient in `person` (`person_id`). **Count cohorts with `COUNT(DISTINCT person_id)`**, never `COUNT(*)`.
- Clinical events live in domain tables and reference a STANDARD `*_concept_id`
  that joins to the `concept` vocabulary table:
  - `condition_occurrence.condition_concept_id`  (diagnoses; standard = SNOMED)
  - `drug_exposure.drug_concept_id`              (medications; standard = RxNorm)
  - `measurement.measurement_concept_id`         (labs & vitals; standard = LOINC)
  - `procedure_occurrence.procedure_concept_id`  (procedures; CPT4/SNOMED/ICD10PCS)
  - `observation.observation_concept_id`         (social hx, etc.)
  - `visit_occurrence.visit_concept_id`          (encounters)
- **Disease/drug-class cohorts must expand the hierarchy.** A standard concept
  has many descendant concepts. To find every patient with e.g. "diabetes" or
  on "any statin", join through `concept_ancestor`:
  ```
  SELECT COUNT(DISTINCT co.person_id)
  FROM condition_occurrence co
  JOIN concept_ancestor ca ON co.condition_concept_id = ca.descendant_concept_id
  WHERE ca.ancestor_concept_id = <standard concept_id from search_concepts>
  ```
- For "patient exposed to drug ingredient X", `drug_era` (ingredient-level,
  pre-rolled) is the simplest: `drug_era.drug_concept_id` = the ingredient.
- For a **drug CLASS** ("any statin", "any insulin", "any anticoagulant"),
  resolve the class concept (e.g. an ATC class or ingredient) with
  search_concepts, then JOIN `concept_ancestor` to its descendants — do NOT
  write `WHERE drug_concept_id IN (SELECT concept_id FROM concept WHERE
  concept_name LIKE '%insulin%')`; that is slow and imprecise. Pattern:
  ```
  SELECT COUNT(DISTINCT de.person_id) FROM drug_era de
  JOIN concept_ancestor ca ON de.drug_concept_id = ca.descendant_concept_id
  WHERE ca.ancestor_concept_id = <class/ingredient concept_id>
  ```
- `condition_era` rolls condition_occurrence into episodes.

## Demographics (concept_ids you can use directly)
- gender_concept_id: 8507 = Male, 8532 = Female.
- ethnicity_concept_id: 38003563 = Hispanic or Latino, 38003564 = Not Hispanic,
  8552 = Unknown.  race_concept_id: 8527 White, 8516 Black/African American,
  8515 Asian, 8552 Unknown.
- **Caveat: race is ~47% Unknown and ethnicity ~57% Unknown** — always report
  the Unknown share when giving demographic breakdowns.
- Age: there is no age column; compute from `year_of_birth` (e.g.
  `YEAR(GETDATE()) - year_of_birth`). `birth_datetime` is populated.

## Measurements / labs (the largest table: 1.24 BILLION rows)
- Call `find_measurement('<lab name>')` first; it gives you the concept_id(s)
  that actually carry values plus their range. **Filter `measurement` DIRECTLY by
  those measurement_concept_id(s)** (use `IN (...)` for several LOINC ids of the
  same lab). NEVER expand measurement concepts through `concept_ancestor` and
  never scan `measurement` unfiltered.
- TRAP: the nominally-standard SNOMED concept for a lab often has NO
  `value_as_number` (e.g. HbA1c SNOMED 4184637 is value-less; the values live on
  LOINC concepts like 3004410). `find_measurement` already filters to
  value-bearing concepts, so trust it over a plain search_concepts result.
- `find_measurement` returns a `recommended_concept_ids` list — use it verbatim
  (`measurement_concept_id IN (...)`) and DO NOT re-query the value distribution;
  it already covers the lab's numeric rows. Picking one rare sub-concept yourself
  is the usual cause of an implausibly tiny count.
- Lab values are in `value_as_number`; units in `unit_concept_id`. Bound absurd
  values (de-id outliers exist) when computing thresholds. Some results are
  coded in `value_as_concept_id` (e.g. positive/negative) — check both.
- **Cohort + lab questions** ("among patients with X, how many have lab > t"):
  use a CTE for the cohort, then filter measurement by concept_id — do NOT write
  one 4-table join across condition_occurrence × concept_ancestor × measurement
  (it times out on the 1.24B-row table). Pattern:
  ```
  WITH cohort AS (
    SELECT DISTINCT co.person_id FROM condition_occurrence co
    JOIN concept_ancestor ca ON co.condition_concept_id=ca.descendant_concept_id
    WHERE ca.ancestor_concept_id = <disease concept_id>)
  SELECT COUNT(DISTINCT m.person_id) FROM measurement m
  JOIN cohort ON cohort.person_id = m.person_id
  WHERE m.measurement_concept_id IN (<recommended ids>) AND m.value_as_number > <t>
  ```

## De-identification & data-quality caveats (avoid wrong answers)
- Dates are shifted per-patient for de-id. There are impossible-future date
  tails (years up to ~2599). For time-trend questions, bound with a sane window
  (e.g. `WHERE condition_start_date BETWEEN '2011-01-01' AND '2025-12-31'`).
- The real data mass spans ~2000–2025 and ramps after 2011 (Epic go-live).
- **Empty tables — do not query:** note, note_nlp, cohort, cohort_definition,
  cost, specimen, metadata, fact_relationship, payer_plan_period, dose_era,
  source_to_concept_map.
- **Unpopulated columns:** drug_exposure.days_supply & route_concept_id,
  condition_occurrence.condition_status_concept_id, person.location_id
  (NO geography data exists). death.cause_concept_id is empty (~0).
- UCSF adds row-for-row `*_extension` tables (source-EHR lineage) and
  `concept_recommended` (curated source→standard mappings); you rarely need them.

## Complex, multi-step, and open-ended questions
- Plan first, then execute with FEW queries. Resolve all needed concept_ids
  up front (search_concepts / find_measurement), then write one combined query
  (CTEs are fine) rather than many incremental ones. Do not re-run a query just
  to reformat — compute everything you need in one pass.
- For DISTRIBUTION questions (by age decade, by category, by value band), state
  the bin definitions and reference year you use (e.g. "age = 2025 −
  year_of_birth", "HbA1c bands <7 / 7–9 / >9"); produce the whole distribution
  in a single GROUP BY.
- For OPEN-ENDED cohort/diagnosis questions, form ONE hypothesis, size it with
  1–3 targeted queries, report the cohort definition + size + supporting counts,
  and stop. Do not exhaustively enumerate every alternative — that wastes time
  and tokens. Offer broader/narrower variants in prose instead of querying them all.

## Answering style
- State the key number(s) clearly, the cohort definition you used, the
  concept_id(s) you resolved, and any assumption (e.g. "diabetes = SNOMED
  201826 + descendants"). If a result looks implausible (date tails, Unknowns),
  flag it. Prefer one good query over many exploratory ones.
"""


class UCSFOMOPConfig(BaseModel):
    """UCSF OMOP clinical database configuration"""
    server: str = Field(default=OMOP_SERVER, description="EHR database server host")
    database: str = Field(default=OMOP_DATABASE, description="EHR database name")
    schema_name: str = Field(default=OMOP_SCHEMA, description="default schema")
    username: str = Field(..., description="EHR database username (must be provided)")
    password: str = Field(..., description="EHR database password (must be provided)")
    log_level: str = Field("INFO", description="Logging level (DEBUG, INFO, WARNING, ERROR)")


def _is_write_query(query: str) -> bool:
    """Check if the query contains write operations"""
    return re.search(
        r"\b(MERGE|CREATE|SET|DELETE|REMOVE|ADD|INSERT|UPDATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|EXEC|EXECUTE|SP_)\b",
        query, re.IGNORECASE) is not None


def _strip_leading_comments(q: str) -> str:
    """Remove leading -- line comments and /* */ block comments so a query that
    starts with an explanatory comment is still recognized as a SELECT/WITH."""
    q = q.lstrip()
    while q:
        if q.startswith("--"):
            nl = q.find("\n")
            q = (q[nl + 1:] if nl != -1 else "").lstrip()
        elif q.startswith("/*"):
            end = q.find("*/")
            q = (q[end + 2:] if end != -1 else "").lstrip()
        else:
            break
    return q


class ClinicalQueryValidator:
    """Clinical record query validator for read-only operations"""

    @staticmethod
    def is_read_only_clinical_query(query: str) -> bool:
        # Tolerate leading SQL comments (the model often annotates its queries).
        body = _strip_leading_comments(query).upper()
        allowed_statements = ['SELECT', 'WITH', 'DECLARE']
        if not any(body.startswith(stmt) for stmt in allowed_statements):
            return False
        if _is_write_query(query):
            return False
        # block stacked statements like "; drop ..." (a single trailing ; is fine)
        if re.search(r';\s*\w', query.rstrip().rstrip(';')):
            return False
        return True


def _escape_like(s: str) -> str:
    """Escape % _ [ for a T-SQL LIKE literal and single-quotes for SQL."""
    return s.replace("'", "''").replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")


def create_ucsf_omop_server(config: UCSFOMOPConfig) -> FastMCP:
    """Create UCSFOMOPAgent server with UCSF OMOP clinical database tools"""

    logging.basicConfig(level=getattr(logging, config.log_level.upper()))
    mcp = FastMCP("UCSFOMOPAgent", instructions=OMOP_INSTRUCTIONS)

    # --- pooled connection (reused across tool calls; reconnect on failure) ---
    _conn_lock = threading.Lock()
    _conn_holder: dict = {"conn": None}

    def _new_connection():
        return pymssql.connect(server=config.server, user=config.username,
                               password=config.password, database=config.database,
                               timeout=120, login_timeout=20)

    def get_conn():
        with _conn_lock:
            conn = _conn_holder["conn"]
            if conn is not None:
                try:
                    c = conn.cursor()
                    c.execute("SELECT 1")
                    c.fetchall()
                    c.close()
                    return conn
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    _conn_holder["conn"] = None
            try:
                conn = _new_connection()
            except Exception as e:
                logger.error(f"Clinical records connection failed: {e}")
                raise ToolError(f"Clinical records connection failed: {e}")
            _conn_holder["conn"] = conn
            return conn

    def _run(sql: str, cap: Optional[int] = MAX_RESULT_ROWS):
        """Execute SELECT, return (columns, rows, truncated, elapsed)."""
        conn = get_conn()
        cur = conn.cursor()
        t = time.time()
        cur.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        if cap is None:
            rows = cur.fetchall()
            truncated = False
        else:
            rows = cur.fetchmany(cap + 1)
            truncated = len(rows) > cap
            rows = rows[:cap]
        cur.close()
        return columns, rows, truncated, time.time() - t

    def _csv(columns, rows):
        lines = [",".join(columns)]
        lines.extend(",".join("" if v is None else str(v) for v in row) for row in rows)
        return "\n".join(lines)

    # ---------------------------- query tool ----------------------------
    @mcp.tool(
        name="query_ucsf_omop",
        annotations=ToolAnnotations(
            title="Query UCSF OMOP Electronic Health Records",
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def query_ucsf_omop(
        sql_query: str = Field(..., description=(
            "A single read-only T-SQL SELECT/WITH query (Microsoft SQL Server). "
            "Use TOP not LIMIT. Tables are unqualified (omop schema). Resolve "
            "concept_ids with search_concepts first."))
    ) -> ToolResult:
        """Execute a READ-ONLY T-SQL query on the UCSF OMOP EHR database.

        Results are returned as CSV, capped at a few thousand rows (aggregate or
        use TOP for large result sets). This is MS SQL Server: use TOP, not LIMIT.
        """
        if not ClinicalQueryValidator.is_read_only_clinical_query(sql_query):
            # Targeted, self-healing guidance for the most common mistakes.
            if re.search(r"\bLIMIT\b", sql_query, re.IGNORECASE):
                raise ToolError("This is Microsoft SQL Server: replace `LIMIT n` "
                                "with `SELECT TOP n ...` (no LIMIT clause exists).")
            raise ToolError("Only read-only SELECT/WITH queries are allowed (no "
                            "INSERT/UPDATE/DELETE/DDL and no stacked statements).")
        try:
            columns, rows, truncated, elapsed = _run(sql_query)
            if not columns:
                return ToolResult(content=[TextContent(type="text",
                    text="Query executed successfully (no result set).")])
            text = _csv(columns, rows)
            footer = f"\n\n[{len(rows)} row(s), {elapsed:.1f}s]"
            if truncated:
                footer = (f"\n\n[TRUNCATED to {len(rows)} rows ({elapsed:.1f}s). "
                          f"Add aggregation (COUNT/GROUP BY) or a tighter filter "
                          f"instead of returning raw rows.]")
            logger.debug(f"query returned {len(rows)} rows in {elapsed:.1f}s")
            return ToolResult(content=[TextContent(type="text", text=text + footer)])
        except ToolError:
            raise
        except Exception as e:
            msg = str(e)
            hint = ""
            if re.search(r"LIMIT", msg, re.IGNORECASE):
                hint = " HINT: use `TOP n` instead of `LIMIT` (SQL Server)."
            elif "Invalid object name" in msg:
                hint = " HINT: check the table name with get_omop_schema (no args lists all tables)."
            elif "Invalid column name" in msg:
                hint = " HINT: check columns with get_omop_schema('<table>')."
            logger.error(f"query error: {e}")
            raise ToolError(f"EHR query error: {msg}{hint}")

    # ---------------------------- schema tool ----------------------------
    _schema_cache: dict = {"tables": None, "cols": {}}

    @mcp.tool(
        name="get_omop_schema",
        annotations=ToolAnnotations(
            title="Describe UCSF OMOP Schema", readOnlyHint=True,
            destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def get_omop_schema(
        table: Optional[str] = Field(default=None, description=(
            "Table name to describe (columns + types). Omit to list all "
            "populated tables with row counts."))
    ) -> ToolResult:
        """Introspect the live OMOP schema. No args -> list tables with row counts
        (so you never query an empty table). With a table name -> its columns and
        types. Read live, so it reflects the current UCSF database."""
        try:
            if not table:
                if _schema_cache["tables"] is None:
                    cols, rows, _, _ = _run("""
                        SELECT t.name AS table_name, SUM(p.rows) AS row_count
                        FROM sys.tables t
                        JOIN sys.partitions p ON t.object_id=p.object_id AND p.index_id IN (0,1)
                        GROUP BY t.name ORDER BY SUM(p.rows) DESC""", cap=None)
                    _schema_cache["tables"] = [{"table": r[0], "row_count": int(r[1])} for r in rows]
                payload = {"database": config.database, "schema": config.schema_name,
                           "tables": _schema_cache["tables"]}
                return ToolResult(content=[TextContent(type="text",
                    text=json.dumps(payload, indent=2))])
            tname = table.split(".")[-1]
            if tname not in _schema_cache["cols"]:
                cols, rows, _, _ = _run(f"""
                    SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_NAME = '{tname.replace("'", "''")}'
                    ORDER BY ORDINAL_POSITION""", cap=None)
                _schema_cache["cols"][tname] = [
                    {"column": r[0], "type": r[1], "max_len": r[2], "nullable": r[3]} for r in rows]
            colinfo = _schema_cache["cols"][tname]
            if not colinfo:
                raise ToolError(f"Table '{tname}' not found. Call get_omop_schema "
                                f"with no arguments to list available tables.")
            return ToolResult(content=[TextContent(type="text",
                text=json.dumps({"table": tname, "columns": colinfo}, indent=2))])
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"schema error: {e}")
            raise ToolError(f"Schema introspection error: {e}")

    # ---------------------------- concept search ----------------------------
    @mcp.tool(
        name="search_concepts",
        annotations=ToolAnnotations(
            title="Search OMOP Vocabulary Concepts", readOnlyHint=True,
            destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def search_concepts(
        query: str = Field(..., description="Clinical term to look up, e.g. 'type 2 diabetes', 'atorvastatin', 'hemoglobin a1c'."),
        domain: Optional[str] = Field(default=None, description="Filter by domain_id: Condition, Drug, Measurement, Procedure, Observation, etc."),
        vocabulary: Optional[str] = Field(default=None, description="Filter by vocabulary_id: SNOMED, RxNorm, LOINC, CPT4, ICD10CM, ..."),
        standard_only: bool = Field(default=True, description="Only standard concepts (standard_concept='S'), which is what clinical event tables reference. Set False to also see source/classification concepts."),
        max_results: int = Field(default=20, description="Max rows (1-50)."),
    ) -> ToolResult:
        """Resolve a clinical term to OMOP concept_id(s). Returns ranked matches
        with concept_id, name, domain, vocabulary, class, standard flag, code, and
        the number of descendant concepts (use that with concept_ancestor to build
        a full disease/drug-class cohort). USE THIS BEFORE WRITING SQL — it is the
        fast path to the right concept_id and avoids guessing or LIKE-scanning."""
        try:
            n = max(1, min(int(max_results), 50))
            raw = query.strip()
            # Tokenized AND match: every word must appear, in any order, so
            # "malignant breast cancer" matches "Malignant neoplasm of breast".
            tokens = [t for t in re.split(r"\s+", raw) if t]
            name_match = " AND ".join(f"c.concept_name LIKE '%{_escape_like(t)}%'" for t in tokens) \
                or f"c.concept_name LIKE '%{_escape_like(raw)}%'"
            code_match = f"c.concept_code = '{raw.replace(chr(39), chr(39)*2)}'"  # also match a raw code like '4548-4'
            where = [f"(({name_match}) OR {code_match})"]
            if standard_only:
                where.append("c.standard_concept = 'S'")
            if domain:
                where.append(f"c.domain_id = '{domain.replace(chr(39), chr(39)*2)}'")
            if vocabulary:
                where.append(f"c.vocabulary_id = '{vocabulary.replace(chr(39), chr(39)*2)}'")
            exact = raw.replace("'", "''").lower()
            sql = f"""
                SELECT TOP {n}
                    c.concept_id, c.concept_name, c.domain_id, c.vocabulary_id,
                    c.concept_class_id, c.standard_concept, c.concept_code,
                    (SELECT COUNT(*) FROM concept_ancestor ca
                     WHERE ca.ancestor_concept_id = c.concept_id) AS descendant_count
                FROM concept c
                WHERE {' AND '.join(where)}
                ORDER BY
                    CASE WHEN LOWER(c.concept_name) = '{exact}' THEN 0
                         WHEN LOWER(c.concept_name) LIKE '{exact}%' THEN 1
                         ELSE 2 END,
                    LEN(c.concept_name)
            """
            cols, rows, _, elapsed = _run(sql, cap=n)
            if not rows:
                return ToolResult(content=[TextContent(type="text", text=(
                    f"No concepts matched '{query}'"
                    + (f" (domain={domain})" if domain else "")
                    + (f" (vocabulary={vocabulary})" if vocabulary else "")
                    + ". Try a broader term, a synonym, or standard_only=false."))])
            out = [{"concept_id": r[0], "concept_name": r[1], "domain_id": r[2],
                    "vocabulary_id": r[3], "concept_class_id": r[4],
                    "standard_concept": r[5], "concept_code": r[6],
                    "descendant_count": int(r[7])} for r in rows]
            note = ("Use a standard_concept='S' concept_id to query event tables; "
                    "if descendant_count>0, join concept_ancestor to include subtypes.")
            return ToolResult(content=[TextContent(type="text",
                text=json.dumps({"matches": out, "hint": note}, indent=2))])
        except Exception as e:
            logger.error(f"concept search error: {e}")
            raise ToolError(f"Concept search error: {e}")

    # ---------------------------- lab/vital finder ----------------------------
    @mcp.tool(
        name="find_measurement",
        annotations=ToolAnnotations(
            title="Find Lab/Vital Measurement Concepts (with real data coverage)",
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def find_measurement(
        name: str = Field(..., description="Lab or vital name, e.g. 'hemoglobin a1c', 'creatinine', 'LDL', 'systolic blood pressure'."),
        max_results: int = Field(default=8, description="Max concepts to profile (1-15)."),
    ) -> ToolResult:
        """Resolve a lab/vital to the RIGHT measurement_concept_id(s) for querying
        the `measurement` table, with real coverage stats: patient count, % of
        rows that have a numeric value, and the value range.

        Use this instead of search_concepts for anything measured in `measurement`.
        It solves a common trap: the nominally-"standard" concept for a lab (often
        a SNOMED concept) frequently has NO value_as_number, while the value-bearing
        rows use a LOINC concept_id. This tool ranks by patient_count among concepts
        that ACTUALLY appear in the measurement table, so you pick a usable
        concept_id immediately. Then filter `measurement` DIRECTLY by that
        concept_id (do NOT expand measurements through concept_ancestor)."""
        try:
            n = max(1, min(int(max_results), 15))
            tokens = [t for t in re.split(r"\s+", name.strip()) if t]
            name_match = " AND ".join(f"concept_name LIKE '%{_escape_like(t)}%'" for t in tokens) \
                or f"concept_name LIKE '%{_escape_like(name.strip())}%'"
            # candidate Measurement-domain concepts by name
            cand_sql = f"""SELECT TOP 40 concept_id, concept_name, vocabulary_id, standard_concept
                           FROM concept
                           WHERE domain_id='Measurement' AND ({name_match})"""
            cols, rows, _, _ = _run(cand_sql, cap=40)
            if not rows:
                return ToolResult(content=[TextContent(type="text", text=(
                    f"No measurement concepts matched '{name}'. Try a simpler term "
                    f"(e.g. 'a1c', 'creatinine') or use search_concepts."))])
            cand = {r[0]: {"concept_id": r[0], "concept_name": r[1],
                           "vocabulary_id": r[2], "standard_concept": r[3]} for r in rows}
            ids = ",".join(str(i) for i in cand)
            # one indexed pass over measurement for coverage on the candidates
            cov_sql = f"""
                SELECT measurement_concept_id,
                       COUNT(*) AS rows_,
                       COUNT(DISTINCT person_id) AS patients,
                       SUM(CASE WHEN value_as_number IS NOT NULL THEN 1 ELSE 0 END) AS numeric_rows,
                       MIN(value_as_number) AS min_val,
                       MAX(value_as_number) AS max_val,
                       AVG(value_as_number) AS avg_val
                FROM measurement
                WHERE measurement_concept_id IN ({ids})
                GROUP BY measurement_concept_id"""
            cols, rows, _, elapsed = _run(cov_sql, cap=None)
            results = []
            for r in rows:
                c = cand.get(r[0], {})
                rows_, pts, numr = int(r[1]), int(r[2]), int(r[3] or 0)
                results.append({
                    "concept_id": r[0], "concept_name": c.get("concept_name"),
                    "vocabulary_id": c.get("vocabulary_id"),
                    "standard_concept": c.get("standard_concept"),
                    "patients": pts, "rows": rows_,
                    "pct_numeric": round(100 * numr / rows_, 1) if rows_ else 0.0,
                    "numeric_rows": numr,
                    "value_min": None if r[4] is None else round(float(r[4]), 2),
                    "value_max": None if r[5] is None else round(float(r[5]), 2),
                    "value_avg": None if r[6] is None else round(float(r[6]), 2),
                })
            # value-bearing + high-patient concepts first
            results.sort(key=lambda x: (x["pct_numeric"] > 0, x["patients"]), reverse=True)
            if not results:
                return ToolResult(content=[TextContent(type="text", text=(
                    f"Concepts named like '{name}' exist but none appear in the "
                    f"measurement table. Use search_concepts to inspect them."))])
            # Decisive recommendation: the value-bearing concepts that together
            # cover ~all numeric rows (drop negligible tails), as a ready IN-list.
            valued = [r for r in results if r["numeric_rows"] > 0]
            total_num = sum(r["numeric_rows"] for r in valued) or 1
            recommended, acc = [], 0
            for r in valued:
                recommended.append(r["concept_id"])
                acc += r["numeric_rows"]
                if acc / total_num >= 0.99:  # enough concepts to cover the lab
                    break
            # dominant unit for the recommended concepts (one small grouped pass)
            unit = None
            if recommended:
                try:
                    ucols, urows, _, _ = _run(f"""
                        SELECT TOP 1 u.concept_name, COUNT(*) AS n
                        FROM measurement m JOIN concept u ON m.unit_concept_id=u.concept_id
                        WHERE m.measurement_concept_id IN ({','.join(map(str, recommended))})
                          AND m.value_as_number IS NOT NULL
                        GROUP BY u.concept_name ORDER BY COUNT(*) DESC""", cap=1)
                    unit = urows[0][0] if urows else None
                except Exception:
                    unit = None
            note = (f"USE THESE: filter `measurement` with "
                    f"`measurement_concept_id IN ({','.join(map(str, recommended))})` "
                    f"and `value_as_number` (unit ≈ {unit}). Do NOT ancestor-expand "
                    f"measurements and do NOT re-query the distribution — these "
                    f"concepts already cover the lab's numeric rows.")
            return ToolResult(content=[TextContent(type="text",
                text=json.dumps({"recommended_concept_ids": recommended,
                                 "dominant_unit": unit,
                                 "measurements": results[:n], "hint": note,
                                 "coverage_query_seconds": round(elapsed, 1)}, indent=2))])
        except Exception as e:
            logger.error(f"find_measurement error: {e}")
            raise ToolError(f"find_measurement error: {e}")

    # ------------------ legacy list-tables (kept for back-compat) ------------------
    @mcp.tool(
        name="list_ucsf_omop_tables",
        annotations=ToolAnnotations(
            title="List UCSF OMOP Clinical Data Tables", readOnlyHint=True,
            destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def list_ucsf_omop_tables() -> ToolResult:
        """List clinical data tables with row counts (prefer get_omop_schema)."""
        return get_omop_schema(table=None)

    return mcp


def main(
    transport: Literal["stdio", "sse", "http"] = "stdio",
    username: Optional[str] = None,
    password: Optional[str] = None,
    log_level: str = "INFO",
    host: str = "127.0.0.1",
    port: int = 8000,
    path: str = "/mcp/",
) -> None:
    """Main entry point for the UCSFOMOPAgent server"""
    if not username or not password:
        raise ValueError("CLINICAL_RECORDS_USERNAME and CLINICAL_RECORDS_PASSWORD must be provided")

    config = UCSFOMOPConfig(username=username, password=password, log_level=log_level)
    logger.info("Starting UCSFOMOPAgent - UCSF OMOP Clinical Database MCP Server")
    logger.info(f"OMOP Server: {config.server}  Database: {config.database}")
    logger.info(f"Username: {config.username}")

    mcp = create_ucsf_omop_server(config)
    mcp.run()


if __name__ == "__main__":
    main(
        username=os.getenv("CLINICAL_RECORDS_USERNAME"),
        password=os.getenv("CLINICAL_RECORDS_PASSWORD"),
        log_level=os.getenv("OMOP_LOG_LEVEL", "INFO"),
    )
