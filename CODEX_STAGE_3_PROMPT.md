Implement Stage 3 of the Research Automation Supervisor.

Read `STAGE_3_CONTRACT.md`, the completed Stage 0/1/2 implementation and tests,
`pyproject.toml`, and current documentation. Implement the frozen contract
exactly. Do not edit any Stage 0/1/2/3 contract or implementation-prompt file.

Stage 3 is retrospective blind calibration only:

- consume an existing verified Stage 2 run;
- reconstruct decision points from evidence available at each point;
- give a persistent read-only supervisor policy/context/contract/evidence but
  not the authoritative human prompt;
- finalize the supervisor proposal before adding authoritative comparison
  material;
- never send proposals to workers or auditors;
- semantic quality is recorded only through structured human reviews;
- readiness is informational and never enables automation.

Use exact-ID supervisor resume, fixed output schemas, network-disabled Codex,
strict integrity, and fake-agent tests.

Do not add live hooks, auto-handoff, model-generated contracts, Git automation,
notifications, background services, network access, API calls, or
project-specific logic.

Use existing dependencies. Do not install packages or invoke a real model in
tests. Run ruff, mypy, and pytest and report all required details.
