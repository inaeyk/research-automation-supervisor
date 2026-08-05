# Public Physics Auditor PA-5C-remediated benchmark machinery

This directory contains 21 neutral, public synthetic fixtures and physically separate
scorer authority. It is qualified with scripted agents only; it is not a real-model
benchmark result.

- `fixtures/case_NNN/` contains only auditor-visible contracts, candidate source/claim
  data, and raw bounded observations. It contains no seeded diagnosis or expected route.
- `oracles/benchmark_oracle.py` is a fixed PA-2 executable. PA-3 never projects it.
- `authority/catalog.json` and `authority/fixture-authority.json` are scorer-only.
  Together they bind source hashes, canonical contract hashes, seeded defects, routes,
  required/acceptable/forbidden categories, severity, and independent approval records.
  Neither file is copied into a fixture, prompt, evidence index, oracle summary, exact
  projection, or Bubblewrap namespace.
- `authority/worker-repair-calibration.json` records the four bounded scripted PA-4
  repair-loop outcomes. It is mechanism evidence, not auditor input or a real-model
  score.

The only production task identifiers are `case_001` through `case_021`. Scientific
scoring uses closed PA-1 facts and never compares report prose. It separately scores
category recognition, severity, route, evidence validity, required categories,
acceptable alternatives, and forbidden categories/routes.

The small suite measures bounded synthetic behavior. It does not establish broad
autonomous physics competence, answer open research questions, or authorize
publication-level conclusions.

Case 004 now unambiguously declares contravariant cylindrical-vector components; the
prior covector/vector mismatch is removed. The failed PA-5B real attempt remains a
historical invalid artifact and is not reused. A fresh real run, if authorized, is PA-5D.
