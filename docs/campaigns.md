# Visible multi-task campaigns

A visible campaign composes ordered Stage 2 substages under a planning Supervisor. It
does not contain or perform historical evaluation.

## Manifest authority

The schema-version-1 campaign manifest freezes:

- campaign ID/title and a visible package root;
- one human Supervisor policy file;
- Supervisor model, reasoning effort, and timeout;
- ordered task IDs/titles;
- each task's Stage 2 specification and optional visible context files;
- source repository ID, source commit/tree, and baseline archive SHA-256;
- a production-path classification; and
- no gold evaluator or artifact roots (legacy fields must be empty).

Every manifest, policy, context, Stage 2 contract, prompt, and visible acceptance script
must remain inside the visible package authority. Absolute locators, parent traversal,
offline-only path components, unsafe links, and confidentiality collisions fail before
a model launch.

## Lifecycle

For each task, the campaign obtains a bounded Supervisor action, runs the Stage 2
Worker/scope/tests/Auditor workflow, and requests a bounded repair or human pause only
through legal state transitions. Supervisor actions name exact required acceptance IDs
and cannot request changes to contract, scope, permission, acceptance, or convention
authority.

The campaign records one terminal model result per task. Before the terminal journal
transition, it seals the task's candidate bytes and portable evidence. When all task IDs
are terminal in manifest order, it atomically publishes `final-candidate/`.

## Commands

```bash
research-supervisor run-visible-campaign "campaign/visible-campaign.yaml" --runs-dir "runs/campaigns"
research-supervisor visible-campaign-status "runs/campaigns/CAMPAIGN-RUN" --json
research-supervisor resume-visible-campaign "runs/campaigns/CAMPAIGN-RUN"
```

Run is synchronous. Resume is only for a valid nonterminal interrupted state; it never
relaunches an action whose completion is uncertain. Human-pause decisions remain
explicit and external.

## Candidate contents

The canonical manifest pins every payload object's relative path, type, mode, byte
length, and SHA-256. `candidate.json` records campaign/task order and portable source,
baseline, visible-test, scope, patch, and terminal summary provenance. Each task has a
`changes.json` operation list and a `changed-files/` overlay for regular-file bytes.

Directories are sealed mode `0500` and files `0400`. Keep the whole candidate tree
together and do not chmod or edit it. Evaluation copies those bytes to disposable
workspaces.

## Completion and offline evaluation

Campaign reports intentionally record historical evaluation as not performed. Stop all
model processes after candidate publication, keep protected authority outside every
model-readable root, and use [direct replay](evaluation.md). Evaluation never changes
campaign state or feeds another model prompt.
