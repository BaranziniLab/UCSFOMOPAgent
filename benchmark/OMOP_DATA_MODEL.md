# UCSF OMOP data model — structural overview (no statistics)

A structural map of what data modalities the agent can query. Absolute counts are
omitted (public repo); this is the qualitative shape the extension's context is
built from.

## CDM
OMOP CDM v5.4 on Microsoft SQL Server, default schema `omop`. Standard OHDSI
vocabularies are loaded: SNOMED (conditions), RxNorm / RxNorm Extension (drugs),
LOINC (labs/measurements), CPT4 / ICD10PCS / HCPCS (procedures), ICD10CM / ICD9CM
(source diagnoses), ATC (drug classes), plus concept infrastructure.

## Populated clinical modalities
- **Demographics** — `person` (gender, race, ethnicity, year_of_birth,
  birth_datetime). Race and ethnicity are heavily "Unknown".
- **Conditions** — `condition_occurrence` / `condition_era` (SNOMED standard,
  ICD10CM source).
- **Drugs** — `drug_exposure` / `drug_era` (RxNorm; `drug_era` is ingredient-level).
- **Measurements / labs & vitals** — `measurement` (LOINC; `value_as_number`,
  units). This is by far the largest table; vitals dominate it. Always filter by
  `measurement_concept_id`.
- **Procedures** — `procedure_occurrence` (CPT4 / SNOMED / ICD10PCS / HCPCS).
- **Observations** — `observation` (social history, language, etc.).
- **Visits** — `visit_occurrence` / `visit_detail` (office / outpatient /
  inpatient / etc.).
- **Death** — `death` (date populated; `cause_concept_id` essentially empty).
- **Vocabulary** — `concept`, `concept_ancestor`, `concept_relationship`,
  `concept_synonym`, `drug_strength`; plus UCSF curated `concept_recommended`,
  `concept_numeric`.

## UCSF-specific extensions
Every fact table has a row-for-row `*_extension` companion carrying source-EHR
lineage and a source key.

## Empty / unpopulated (the agent should not query these)
- Empty tables: `note`, `note_nlp`, `cohort`, `cohort_definition`, `cost`,
  `specimen`, `metadata`, `fact_relationship`, `payer_plan_period`, `dose_era`,
  `source_to_concept_map`.
- Unpopulated columns: `drug_exposure.days_supply` & `route_concept_id`,
  `condition_occurrence.condition_status_concept_id`, `person.location_id`
  (no geography), `death.cause_concept_id`.

## De-identification caveats
Per-patient date shifting produces impossible-future date tails; time-trend
queries should be bounded to a sane window. The real data mass spans roughly
2000–2025, ramping after the Epic go-live (~2011).
