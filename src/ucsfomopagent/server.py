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
import threading
import time
from typing import Literal, Optional

import pymssql
from fastmcp.exceptions import ToolError
from fastmcp.server import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ucsfomopagent.jobs import INSTRUCTIONS as JOB_INSTRUCTIONS, register_job_tools

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
You query the UCSF de-identified OMOP CDM on Microsoft SQL Server, read-only.
Use get_omop_schema for current tables, approximate metadata row counts and columns.
Do not treat historical population sizes, empty tables or date ranges as current facts.

Discovery and execution:
- Resolve conditions, drugs and procedures with search_concepts before writing SQL.
  It matches literal words or codes, not synonyms. Standard concepts generally map
  to event columns; ATC classes require standard_only=false and concept_ancestor.
- find_measurement returns candidate labs with up to 100 nonrandom numeric rows per
  concept, separated by unit. These are presence samples, NOT population coverage.
  Review specimen, assay, concept and unit before combining results or thresholds.
  recommended_concept_ids are candidates, not an exhaustive clinical definition.
  Its full_profile_query can be submitted as a monitored job for exact statistics.
  Coded nonnumeric measurements need value_as_concept_id queries after discovery.
- query_ucsf_omop is for small previews and inexpensive queries, with a short timeout
  and capped output. A TOP bound limits returned rows, not database computation.
  For slow aggregates, large exports, uncertain costs or timeouts, use
  omop-submit_query_job once, then omop-query_job_status at recommended intervals.
  Use estimated plans when permitted, avoid full COUNT scans solely for estimates,
  and report unknown ETA honestly. Never claim every query is guaranteed to finish.

Large fact-table studies:
- Require staged extraction: freeze the smallest valid seed cohort once, then
  fetch each required domain in bounded, parameterized batches. A seed may be a
  candidate superset; apply every eligibility rule before calling it eligible.
  Avoid repeating the entire eligibility CTE graph for each domain or batch.
  Longer timeouts do not fix expensive plans. Pilot batches and adapt batch size
  while preserving the full phenotype definition. Keep identifiers and files local.
- Freeze concepts, temporal rules, units and output grain in a private manifest;
  reconcile every batch before declaring the study complete. Never relax filters,
  follow-up or eligibility merely to make extraction finish.
- A heartbeat proves worker responsiveness, not database progress. Zero streamed
  rows before completion does not mean an empty cohort. ETA stays unknown until
  useful measurements exist; estimates from comparable batches are provisional.

SQL and clinical correctness:
- SQL Server uses SELECT TOP n, not LIMIT. Use explicit columns and selective
  concept/date/person filters. Tables normally resolve in omop; verify the schema.
- Count distinct person_id for patient cohorts. Avoid multiplying rows when joining
  event tables: use EXISTS or distinct cohort IDs. CTEs do not force materialization
  or guarantee faster execution; inspect plans for expensive joins.
- Expand disease or drug classes via concept_ancestor. drug_era is ingredient-level;
  drug_exposure is for individual exposures. Keep cohort definitions explicit.
- Filter measurement directly on selected measurement_concept_id values. Do not
  ancestor-expand numeric labs without confirming clinical equivalence. Keep units
  separate, and state any outlier exclusions instead of silently discarding values.
- concept_id=0 is unmapped. Report Unknown demographics. Dates are shifted per person;
  assess available ranges and agree a date window before time-trend analysis.
  YEAR(reference_date)-year_of_birth is an approximate age, not exact age.
- Present aggregate results, concepts, filters, units, assumptions and limitations.
  Keep full exports local; do not paste row-level clinical data or credentials into chat.
"""


class UCSFOMOPConfig(BaseModel):
    """UCSF OMOP clinical database configuration"""
    server: str = Field(default=OMOP_SERVER, description="EHR database server host")
    database: str = Field(default=OMOP_DATABASE, description="EHR database name")
    schema_name: str = Field(default=OMOP_SCHEMA, description="default schema")
    username: str = Field(..., description="EHR database username (must be provided)")
    password: str = Field(..., description="EHR database password (must be provided)")
    log_level: str = Field("INFO", description="Logging level (DEBUG, INFO, WARNING, ERROR)")


class ClinicalQueryValidator:
    """Clinical record query validator for read-only operations"""

    @staticmethod
    def is_read_only_clinical_query(query: str) -> bool:
        from .jobs import validate_query
        try:
            validate_query(query, 'sql')
            return True
        except ValueError:
            return False


def _escape_like(s: str) -> str:
    """Escape % _ [ for a T-SQL LIKE literal and single-quotes for SQL."""
    return s.replace("'", "''").replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")


def create_ucsf_omop_server(config: UCSFOMOPConfig) -> FastMCP:
    """Create UCSFOMOPAgent server with UCSF OMOP clinical database tools"""

    logging.basicConfig(level=getattr(logging, config.log_level.upper()))
    mcp = FastMCP("UCSFOMOPAgent", instructions=OMOP_INSTRUCTIONS + JOB_INSTRUCTIONS)

    # --- pooled connection (reused across tool calls; reconnect on failure) ---
    _conn_lock = threading.RLock()
    _conn_holder: dict = {"conn": None}

    def _new_connection():
        return pymssql.connect(server=config.server, user=config.username,
                               password=config.password, database=config.database,
                               timeout=30, login_timeout=20, autocommit=True)

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
                logger.error("Clinical records connection failed (%s)", type(e).__name__)
                raise ToolError("Clinical records connection failed; check network access and configured credentials.") from None
            _conn_holder["conn"] = conn
            return conn

    def _run(sql: str, cap: Optional[int] = MAX_RESULT_ROWS):
        """Execute SELECT, return (columns, rows, truncated, elapsed)."""
        with _conn_lock:
            conn = get_conn()
            cur = conn.cursor()
            t = time.monotonic()
            try:
                cur.execute(sql)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchall() if cap is None else cur.fetchmany(cap + 1)
                truncated = cap is not None and len(rows) > cap
                return columns, rows if cap is None else rows[:cap], truncated, time.monotonic() - t
            except Exception:
                _conn_holder["conn"] = None
                try:
                    conn.close()
                except Exception:
                    pass
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    pass

    def _csv(columns, rows):
        import csv
        import io
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(rows)
        return out.getvalue()

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
                          f"or use omop-submit_query_job for a complete local export.]")
            logger.debug(f"query returned {len(rows)} rows in {elapsed:.1f}s")
            return ToolResult(content=[TextContent(type="text", text=text + footer)])
        except ToolError:
            raise
        except Exception as e:
            msg = str(e)
            hint = " Use omop-submit_query_job for slow queries or full exports; monitor omop-query_job_status."
            if re.search(r"LIMIT", msg, re.IGNORECASE):
                hint = " HINT: use `TOP n` instead of `LIMIT` (SQL Server)."
            elif "Invalid object name" in msg:
                hint = " HINT: check the table name with get_omop_schema (no args lists all tables)."
            elif "Invalid column name" in msg:
                hint = " HINT: check columns with get_omop_schema('<table>')."
            logger.error("Clinical query failed (%s)", type(e).__name__)
            raise ToolError(f"EHR query failed.{hint}") from None

    # ---------------------------- schema tool ----------------------------
    _schema_cache: dict = {"tables": None, "cols": {}}

    @mcp.tool(
        name="get_omop_schema",
        annotations=ToolAnnotations(
            title="Describe UCSF OMOP Schema", readOnlyHint=True,
            destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def get_omop_schema(
        table: Optional[str] = Field(default=None, description=(
            "Table name to describe (columns + types). Omit to list tables "
            "in the configured schema with approximate metadata row counts."))
    ) -> ToolResult:
        """Introspect the live OMOP schema. No args -> list tables with row counts
        (so you never query an empty table). With a table name -> its columns and
        types. Read live, so it reflects the current UCSF database."""
        try:
            if not table:
                if _schema_cache["tables"] is None:
                    cols, rows, _, _ = _run(f"""
                        SELECT t.name AS table_name, SUM(p.rows) AS row_count
                        FROM sys.tables t
                        JOIN sys.partitions p ON t.object_id=p.object_id AND p.index_id IN (0,1)
                        WHERE t.schema_id = SCHEMA_ID('{config.schema_name.replace("'", "''")}')
                        GROUP BY t.name ORDER BY SUM(p.rows) DESC""", cap=None)
                    _schema_cache["tables"] = [{"table": r[0], "row_count": int(r[1])} for r in rows]
                payload = {"database": config.database, "schema": config.schema_name,
                           "tables": _schema_cache["tables"]}
                return ToolResult(content=[TextContent(type="text",
                    text=json.dumps(payload, indent=2))])
            parts = table.split(".")
            if len(parts) > 2 or (len(parts) == 2 and parts[0] != config.schema_name):
                raise ToolError("Use a table in the configured OMOP schema.")
            tname = parts[-1]
            if tname not in _schema_cache["cols"]:
                cols, rows, _, _ = _run(f"""
                    SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = '{config.schema_name.replace("'", "''")}'
                      AND TABLE_NAME = '{tname.replace("'", "''")}'
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
            logger.error("Schema introspection failed (%s)", type(e).__name__)
            raise ToolError("Schema introspection failed; check database access and metadata permissions.") from None

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
            if not raw:
                raise ToolError("Provide a nonempty clinical term or vocabulary code.")
            # Tokens match literally; synonyms require a separate search.
            tokens = [t for t in re.split(r"\s+", raw) if t]
            name_match = " AND ".join(f"c.concept_name LIKE '%{_escape_like(t)}%'" for t in tokens) \
                or f"c.concept_name LIKE '%{_escape_like(raw)}%'"
            code_match = f"c.concept_code = '{raw.replace(chr(39), chr(39)*2)}'"  # also match a raw code like '4548-4'
            where = [f"(({name_match}) OR {code_match})", "c.invalid_reason IS NULL"]
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
        except ToolError:
            raise
        except Exception as e:
            logger.error("Concept search failed (%s)", type(e).__name__)
            raise ToolError("Concept search failed; check database access or try a narrower term.") from None

    # ---------------------------- lab/vital finder ----------------------------
    @mcp.tool(
        name="find_measurement",
        annotations=ToolAnnotations(
            title="Find Lab/Vital Concepts (bounded samples by unit)",
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def find_measurement(
        name: str = Field(..., description="Lab or vital name, e.g. 'hemoglobin a1c', 'creatinine', 'LDL', 'systolic blood pressure'."),
        max_results: int = Field(default=8, description="Max concepts to profile (1-15)."),
    ) -> ToolResult:
        """Find measurement concepts using bounded, nonrandom numeric samples.

        Samples establish presence and units, not prevalence, exhaustive coverage,
        or a representative value distribution. Exact profiling belongs in a job.
        """
        try:
            raw = name.strip()
            if not raw:
                raise ToolError("Provide a nonempty lab or vital name.")
            n = max(1, min(int(max_results), 15))
            tokens = re.split(r"\s+", raw)
            name_match = " AND ".join(
                f"concept_name LIKE '%{_escape_like(t)}%'" for t in tokens)
            exact = raw.replace("'", "''")
            candidate_sql = f"""
                SELECT TOP {n} concept_id, concept_name, vocabulary_id, standard_concept
                FROM concept
                WHERE domain_id='Measurement' AND invalid_reason IS NULL AND ({name_match})
                ORDER BY CASE WHEN concept_name = '{exact}' THEN 0 ELSE 1 END,
                         CASE WHEN vocabulary_id='LOINC' THEN 0 ELSE 1 END,
                         CASE WHEN standard_concept='S' THEN 0 ELSE 1 END,
                         LEN(concept_name), concept_id"""
            _, rows, _, _ = _run(candidate_sql, cap=n)
            if not rows:
                return ToolResult(content=[TextContent(type="text", text=(
                    f"No measurement concepts matched '{name}'. Try a simpler term or synonym."))])
            candidates = {int(r[0]): {"concept_id": int(r[0]), "concept_name": r[1],
                                    "vocabulary_id": r[2], "standard_concept": r[3]}
                          for r in rows}
            ids = ','.join(map(str, candidates))
            sample_sql = f"""
                SELECT c.concept_id, s.unit_concept_id, COUNT_BIG(*) AS sampled_rows,
                       MIN(s.value_as_number), MAX(s.value_as_number)
                FROM concept c
                CROSS APPLY (
                    SELECT TOP 100 unit_concept_id, value_as_number
                    FROM measurement m
                    WHERE m.measurement_concept_id = c.concept_id
                      AND m.value_as_number IS NOT NULL
                ) s
                WHERE c.concept_id IN ({ids})
                GROUP BY c.concept_id, s.unit_concept_id"""
            sample_status = 'completed'
            sample_error = None
            try:
                _, samples, _, elapsed = _run(sample_sql, cap=None)
            except Exception as error:
                # Vocabulary discovery is still useful when a sparse lab sample stalls.
                samples, elapsed = [], None
                sample_status = 'unavailable'
                sample_error = ('Numeric sampling failed (' + type(error).__name__ +
                                '); presence and units are unknown. Submit sample_query as a '
                                'monitored job instead of repeating this synchronous lookup.')
            for candidate in candidates.values():
                candidate['numeric_samples_by_unit'] = [] if sample_status == 'completed' else None
            for concept_id, unit_id, count, low, high in samples:
                candidates[int(concept_id)]['numeric_samples_by_unit'].append({
                    'unit_concept_id': unit_id, 'sampled_rows': int(count),
                    'sample_min': None if low is None else float(low),
                    'sample_max': None if high is None else float(high)})
            profile_sql = f"""SELECT measurement_concept_id, unit_concept_id,
                COUNT_BIG(*) AS numeric_rows, COUNT_BIG(DISTINCT person_id) AS patients,
                MIN(value_as_number) AS minimum, MAX(value_as_number) AS maximum
                FROM measurement WHERE measurement_concept_id IN ({ids})
                AND value_as_number IS NOT NULL
                GROUP BY measurement_concept_id, unit_concept_id"""
            payload = {
                'measurements': list(candidates.values()),
                'recommended_concept_ids': [key for key, value in candidates.items()
                                            if value['numeric_samples_by_unit']],
                'sample_limit_per_concept': 100,
                'sample_query_seconds': None if elapsed is None else round(elapsed, 1),
                'sample_status': sample_status,
                'sample_error': sample_error,
                'sample_query': sample_sql,
                'full_profile_query': profile_sql,
                'hint': ('Candidate IDs require clinical review: name matches can describe different '
                         'specimens or assays. Samples are nonrandom and neither exhaustive nor '
                         'population estimates. Filter each threshold by unit_concept_id; never mix '
                         'units. Submit full_profile_query with omop-submit_query_job for exact '
                         'coverage, and monitor omop-query_job_status. A TOP limit does not '
                         'guarantee cheap execution; if sampling times out, use a monitored job.')}
            return ToolResult(content=[TextContent(type='text', text=json.dumps(payload, indent=2))])
        except ToolError:
            raise
        except Exception as e:
            logger.error("Measurement discovery failed (%s)", type(e).__name__)
            raise ToolError("Measurement discovery failed. Use search_concepts for vocabulary "
                            "discovery and omop-submit_query_job for bounded, monitored profiling.") from None

    # ------------------ legacy list-tables (kept for back-compat) ------------------
    @mcp.tool(
        name="list_ucsf_omop_tables",
        annotations=ToolAnnotations(
            title="List UCSF OMOP Clinical Data Tables", readOnlyHint=True,
            destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def list_ucsf_omop_tables() -> ToolResult:
        """List clinical data tables with row counts (prefer get_omop_schema)."""
        return get_omop_schema(table=None)

    register_job_tools(mcp, "ucsfomopagent", config.model_dump(), prefix="omop-")
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

    mcp = create_ucsf_omop_server(config)
    mcp.run()


if __name__ == "__main__":
    main(
        username=os.getenv("CLINICAL_RECORDS_USERNAME"),
        password=os.getenv("CLINICAL_RECORDS_PASSWORD"),
        log_level=os.getenv("OMOP_LOG_LEVEL", "INFO"),
    )
