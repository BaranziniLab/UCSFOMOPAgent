# UCSFOMOPAgent 0.3.2

This guidance-only release directs large fact-table studies to staged extraction:
freeze a minimal seed cohort or candidate superset once, then retrieve required
facts in bounded, parameterized batches instead of repeating a complex eligibility
CTE graph. Pilot batches guide execution choices without changing clinical rules.

The server instructions, bundled phenotype and query-job skills, and README now
require private study/batch manifests, explicit phenotype semantics, complete
batch reconciliation and authoritative retry outputs. They distinguish seed
cohorts from eligible patients, events from patient counts, worker heartbeats
from database progress, and zero streamed rows from completed empty results.
ETA remains provisional; a frozen manifest does not create a database snapshot.

The runtime and tool schemas are unchanged. Staging and manifests are an explicit
agent workflow, not a new automatic scheduler or query-resume capability. No
clinical counts, patient records, credentials or private study paths are included.

Validation: the existing offline suite, wheel build and deterministic BRXT rebuild
passed locally. This release was not installed into the running live experiment;
that installation remains on 0.3.1. No new live-database performance claim is made.
