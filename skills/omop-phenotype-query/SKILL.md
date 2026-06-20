---
name: omop-phenotype-query
description: Query the UCSF OMOP CDM to identify patient phenotypes and clinical concepts, fast and robustly, using the agent's concept/schema/lab tools.
---

Use this skill when the user wants to define or query patient phenotypes using
the OMOP Common Data Model at UCSF.

## When to activate
- User asks about OMOP, phenotypes, standard concepts, or the OHDSI framework
- User wants to count patients with conditions/drugs/labs using standard vocabularies

## Fast, reliable workflow (use the dedicated tools — don't guess)
1. **Resolve concepts first.**
   - Diseases / drugs / procedures → `search_concepts("type 2 diabetes", domain="Condition")`.
   - Labs / vitals (the `measurement` table) → `find_measurement("hemoglobin a1c")`,
     and use its `recommended_concept_ids` verbatim.
2. **Build the cohort with hierarchy expansion.** For a disease or drug class,
   join `concept_ancestor` from the standard concept to include all subtypes:
   `JOIN concept_ancestor ca ON x.concept_id = ca.descendant_concept_id
    WHERE ca.ancestor_concept_id = <standard concept_id>`. Count patients with
   `COUNT(DISTINCT person_id)`.
3. **Run with `query_ucsf_omop`** (Microsoft SQL Server / T-SQL: use `TOP`, not
   `LIMIT`). Use `get_omop_schema` if unsure of a table's columns.
4. **Present results** with the concept_ids used and any assumptions, and offer
   broader/narrower definitions via concept ancestors.

## Notes
- OMOP uses standard concept IDs — avoid local source_value codes and never
  LIKE-scan source_value on the big event tables.
- Filter `measurement` DIRECTLY by measurement_concept_id; never ancestor-expand
  measurements. The nominally-standard lab concept often has no value — trust
  `find_measurement`.
- For "cohort with lab > threshold", use a CTE for the cohort, then filter
  measurement — avoid 4-table joins on the 1.24B-row measurement table.
- All queries are read-only on de-identified data. concept_id = 0 is unmapped —
  filter it out for clean phenotypes. Race/ethnicity are heavily "Unknown";
  report that share.
