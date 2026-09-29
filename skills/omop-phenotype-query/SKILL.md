---
name: omop-phenotype-query
description: Define and query UCSF OMOP phenotypes using vocabulary discovery, unit-aware lab samples, and monitored jobs for expensive queries and exports.
---

Use for OMOP phenotypes, standardized clinical concepts, or patient cohorts.

1. Inspect `get_omop_schema` for current tables and columns. Table row counts are
   approximate metadata, not exact cohort sizes. Never assume historical empty
   tables or population counts remain current.
2. Resolve conditions, drugs and procedures with `search_concepts`. Search words
   are literal, not semantic synonyms. For ATC classes set `standard_only=false`.
   Use `concept_ancestor` for disease/drug-class descendants; count distinct patients
   or use EXISTS to avoid multiplying event rows. `drug_era` is ingredient-level.
3. Discover labs with `find_measurement`. It samples up to 100 numeric rows per
   candidate concept and groups samples by unit. Samples are nonrandom, not
   prevalence estimates or exhaustive coverage. Review specimen and assay before
   using `recommended_concept_ids`; a name match does not imply equivalence.
   If `sample_status` is `unavailable`, vocabulary candidates remain valid but
   measurement presence and units are unknown, not absent. Submit `sample_query`
   as a monitored job; do not repeat the same synchronous sampling call.
   Thresholds must include `unit_concept_id`, or explicitly convert compatible
   units. Coded results require `value_as_concept_id` instead of numeric thresholds.
4. Use `query_ucsf_omop` for inexpensive bounded previews. SQL Server uses `TOP`,
   not `LIMIT`. Select explicit columns and filter by concepts, dates and patients.
   CTEs do not force materialization, and TOP does not guarantee a cheap query.
5. Use `omop-submit_query_job` for full exports, costly aggregates, previous
   timeouts or uncertain query costs. For exact numeric lab coverage submit the
   discovery tool's `full_profile_query`. Consider `mode="explain"` first if cost
   is uncertain and SHOWPLAN permission exists. Avoid an expensive exact count
   solely to estimate cost. Reuse known estimates, clearly labelled as estimates.
6. Poll `omop-query_job_status` at `recommended_poll_seconds`. Report phase,
   elapsed time, rows and bytes; ETA may be unknown until rows stream. Submit
   once, avoid duplicate queries, and do not consider a partial file complete.
   Only `completed` supplies a complete export. Process it locally, showing
   summaries in chat. On failure inspect the cause and revise before retrying;
   cancel unwanted work with `omop-cancel_query_job` and monitor terminal status.
7. State concepts, units, cohort filters, reference date and assumptions. De-ID
   shifts dates per patient; validate date ranges. Age from birth year is approximate.
   Report unmapped concepts and Unknown demographic shares when relevant.

## Large fact-table studies

Require staged extraction before a full study across large event tables. Freeze a
private study manifest: database/schema, extraction start time, resolved concept
IDs and descendant lists, inclusion/exclusion rules, temporal windows and boundary
conventions, index-date selection, units, missing-data rules, censoring, output
grain and query-template version or hash.

Compute the minimal seed cohort once and save distinct identifiers privately. If
eligibility depends on later facts, freeze a candidate superset and apply the
remaining rules after extracting those facts. The seed count is not necessarily
the eligible population. Avoid repeating the complete eligibility CTE graph in
each domain query or batch.

Extract required domains using disjoint identifier batches with selective concept
and date predicates. Pilot a small batch before scaling. Bind parameters through
private local parameter files; respect SQL Server's parameter limit including
other predicates. Keep identifiers and patient data out of chat. Reduce batch
size or change an equivalent execution plan when needed; never silently remove a
phenotype rule, narrow follow-up, or discard costly patients to finish sooner.

Reconcile every planned batch, including completed batches with zero matching
rows. Apply cross-domain eligibility and temporal relationships using the frozen
definition. Preserve event identifiers and distinguish events from distinct
patients. A frozen manifest is not a database snapshot: separate connections can
observe changes during extraction. Record any explicitly authorized change to
phenotype semantics as a new manifest version.

Credentials are injected by BioRouter or retrieved through the CLI credential
profile. Never request secrets in tool arguments or print credentials. Follow the
`ucsfomopagent-query-jobs` skill for CLI monitoring and export budgets. Neither MCP
nor background jobs guarantee that arbitrary queries can finish within resource limits.
