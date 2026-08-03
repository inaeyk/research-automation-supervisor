# Public synthetic PA-3 fixtures

These files are mechanism-calibration inputs for the standalone Physics Auditor. They
contain no protected or historical material.

- `clean/` is the declared implementation and trusted standard-library oracle source.
- `sign_error/` contains an obvious sign reversal.
- `correct_alternative/` is structurally different but satisfies the contract.
- `reports/` contains strict expected reports for deterministic scripted-model tests,
  including clean, repair, insufficient-evidence, convention, gauge/constraint,
  unsupported-claim, and adversarial cases.
- `execution-config.yaml` selects only the Codex CLI fresh read-only policy.
- `prompt-golden.json` freezes the canonical missing-evidence prompt.

First copy one implementation directory plus `clean/oracle.py` into a new Git
worktree. Create a trusted PA-2 catalog for `force_oracle` using the exact system
Python and program hashes, then run `research-supervisor run-physics-oracle`. The
standalone command in `docs/physics_auditor_execution.md` consumes that finalized PA-2
action. For the insufficient-evidence case, supply a new empty evidence directory;
the safe index records `force_oracle` as explicitly missing.

These cases calibrate the action mechanism only. They do not establish broad Physics
Auditor quality.
