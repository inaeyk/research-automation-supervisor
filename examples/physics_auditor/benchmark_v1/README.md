# Public Physics Auditor PA-5B benchmark v1

This directory contains 21 opaque, public synthetic fixtures and one separately stored
benchmark authority catalog.

- `fixtures/case_NNN/` contains only auditor-visible contracts, implementation/claim
  data, and bounded evidence.
- `oracles/benchmark_oracle.py` is a fixed PA-2 executable. PA-3 never projects it.
- `authority/catalog.json` contains expected routes, required semantic finding
  categories, repair eligibility, human-review authority, risk class, fixed repetition
  counts, and thresholds. It is never declared as task evidence or projected to a
  Physics Auditor.
- `authority/worker-repair-calibration.json` records the four bounded scripted PA-4
  repair-loop outcomes. It is mechanism evidence, not auditor input or a real-model
  score.

The identifiers and directory names are deliberately opaque. Scientific scoring uses
the closed PA-1 finding categories and authoritative PA-1 route; it never compares
report prose. Highest-risk cases have three fresh PA-3 runs and lower-risk mechanism
cases have one, for 41 predeclared benchmark runs.

The small suite measures bounded synthetic behavior. It does not establish broad
autonomous physics competence, answer open research questions, or authorize
publication-level conclusions.

The first real attempt is retained as a failed benchmark artifact. Independent audits
found that several auditor-visible evidence files disclosed the intended diagnosis,
so path separation did not provide semantic answer-key separation. Do not use the
recorded scores for qualification without a separately authorized remediation and a
new predeclared run.
