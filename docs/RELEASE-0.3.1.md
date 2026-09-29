# UCSFOMOPAgent 0.3.1

Measurement discovery now retains vocabulary candidates when the numeric sample
query fails. The response explicitly marks sample presence and units as unknown,
returns the sample query for a monitored background job, and avoids treating a
failed sample as evidence that a measurement is absent. Raw driver errors remain
redacted, and failed connections are discarded.

The OMOP skill now explains this fallback. Query-job guidance includes valid
BioRouter JavaScript namespace access for hyphenated tool names, exact schema
arguments, and single-statement read-only SQL.

Validation: the offline suite includes a forced sampling-timeout regression that
checks candidate preservation, unknown units, job routing, connection cleanup and
error redaction. Live BioRouter testing with UCSF Versa GPT-5.5 confirmed the
patched extension loads, returns measurement discovery, and completes a monitored
background query. The live lookup completed on the validation attempt; the timeout
fallback itself is covered by the regression test, not claimed as a reproduced
live timeout.
