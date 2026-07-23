# Deterministic Codex adapter

Stage 1 has one narrow trust boundary: a human writes a prompt file and a YAML
request, and the adapter transports the exact UTF-8 prompt bytes to one
non-interactive `codex exec` process. The adapter does not generate prompts,
reason about workflow stages, resume sessions, retry runs, create Git objects,
run handoffs, or advance a workflow.

## Request

`research-supervisor validate-codex-request PATH` validates without writing or
launching Codex. `research-supervisor run-codex PATH` performs the run. Both
commands also support `--json`; `run-codex` accepts `--runs-dir`, which defaults
to `runs/codex` under the caller's current directory.

The request has exactly these fields:

```yaml
schema_version: 1
run_id: minimal-worker-001
role: worker
workspace: ../..
prompt_path: ../prompts/minimal-worker.md
model: gpt-5.6-sol
reasoning_effort: high
timeout_seconds: 1800
```

Relative workspace and prompt paths are resolved from the request file's
directory. The workspace must be a directory in a Git worktree. The prompt must
be a nonempty regular UTF-8 file of at most 1 MiB. It is read once before launch
and only its byte count and SHA-256 are recorded.

Before successful validation output, the adapter calls `would_redact_text`,
defined as whether `redact_text` changes the input, for every exact request
structure it will render: the request locator, run ID, role, model, reasoning
effort, resolved workspace and prompt, prompt hash, and fixed role-policy
strings. A run also checks the
supplied and resolved runs directory and the exact prospective
`<runs-dir>/<run_id>` artifact directory. Detection therefore covers removed
environment literals, Authorization/Bearer forms, built-in API-token prefixes,
and supported sensitive assignments without maintaining a second pattern list.
Any value that redaction would modify is rejected as a generic input error (exit
2), without rendering the value or surrounding locator and before creating the
run directory. Accepted runs retain an exact, existing artifact directory
rather than redacting, hashing, or renaming a required locator.

## Fixed policy and command

Role policy is adapter-owned:

| Role | Sandbox | Approval | Ephemeral |
|---|---|---|---|
| `supervisor` | `read-only` | `never` | no |
| `worker` | `workspace-write` | `never` | no |
| `auditor` | `read-only` | `never` | yes |

The adapter uses a shell-free argument vector equivalent to:

```text
codex --ask-for-approval never
  exec --json
  --output-last-message <adapter temporary path>
  --model <validated model>
  -c model_reasoning_effort=<validated effort>
  -c web_search="disabled"
  -c sandbox_workspace_write.network_access=false
  -c features.skill_mcp_dependency_install=false
  --sandbox <role policy>
  --ignore-user-config --ignore-rules --strict-config
  --cd <resolved workspace>
  [--ephemeral for auditors]
  -
```

The approval option is global and therefore appears before `exec`; no
`--ask-for-approval` occurrence is passed after `exec`. The process also uses
the resolved workspace as its working directory. Prompt
bytes go only to standard input. The adapter does not add search, extra writable
directories, Git-check bypasses, full-auto, or sandbox-bypass flags. Environment
variables with credential-shaped names are removed from a copied environment;
only their names are recorded.

## Lifecycle and artifacts

Each run exclusively creates `<runs-dir>/<run_id>` and never reuses it. Codex is
started in a new process session. Standard output is streamed as JSONL and
standard error is bounded. At the hard timeout, or when stdout exceeds 100 MiB
or stderr exceeds 10 MiB, the whole process group receives termination, a fixed
two-second grace period, and then a force-kill if required. A normally exiting
leader is also followed by a process-group check; remaining descendants receive
the same TERM/grace/KILL cleanup before the adapter returns, without changing
the leader's observed exit status or retrying it. There are no retries.

The run directory contains:

- `request.normalized.json`: resolved request and fixed role policy;
- `prompt.sha256`: prompt hash, never prompt text;
- `events.jsonl`: canonical redacted JSON objects;
- `stderr.log`: bounded redacted diagnostics;
- `final-message.md`: redacted last response;
- `metadata.json`: command policy, timing, process, event, and environment-name
  metadata;
- `result.json`: the normalized outcome returned by the CLI.

JSONL is decoded as strict UTF-8. Malformed JSON, non-object values, and invalid
UTF-8 lines are not retained; metadata stores only their count and hashes of the
original line bytes. Canonical events and JSON artifacts use ASCII-safe JSON
escaping, so an escaped lone surrogate cannot cause a later UTF-8 encoding
failure. Any unexpected per-line parsing, recursive-redaction, or canonical
serialization error is handled as a malformed line using the same hash-only
policy. Metadata and result finalization use atomic replacement, and failure
runs retain the same useful artifact structure. The persisted, returned, and
CLI-rendered result are the same type-checked sanitized value.

## Outcomes and exit codes

Statuses, in classification order, are `launch_failed`, `timed_out`,
`output_limit_exceeded`, `permission_blocked`, `malformed_event_stream`,
`process_failed`, `missing_final_message`, and `succeeded`.

CLI exit codes are:

- `0`: succeeded;
- `2`: invalid request, YAML, path, prompt, or CLI input;
- `3`: missing or unusable local dependency;
- `4`: launch or process failure;
- `5`: timeout;
- `6`: conservative permission or sandbox denial;
- `7`: malformed events, missing final response, or an output limit;
- `1`: unexpected internal failure.

Permission classification only uses allowlisted phrases in stderr or explicit
structured failure/denial events. Assistant prose cannot trigger it.

## Redaction and tests

Redaction is recursive and idempotent. It covers authorization bearer headers,
common API-token prefixes, credential-shaped assignments and JSON keys, and
values removed from the subprocess environment. A composite value owned by a
sensitive key retains its mapping/list shape but every descendant string is
replaced. Non-string JSON scalars retain their types, and existing placeholders
are protected across repeated passes. Removed literals are matched together in
deterministic longest-first order; overlapping spans are merged before one
replacement is made. A standalone `<REDACTED>` is protected, while a longer
sensitive value containing that complete placeholder is replaced in full.
Redaction is a best-effort content control, not a proof that an arbitrary
unknown secret shape can be recognized. The human prompt remains the
authoritative input and is never rewritten or printed by the CLI.

Tests launch a parser-aware `tests/fixtures/fake_codex.py` (or inject discovery,
environment, clock, and version boundaries). The fake rejects approval options
placed after `exec`, exercises broken stdin and descendant containment, and does
not invoke a real model, inspect user Codex configuration, log in, install
software, or access the network.

## Stage 2 adapter extension

The workflow engine may supply one of two fixed engine-owned output schemas and
may request `exec resume` with one explicit worker thread ID. Neither control is
available in a human substage specification. Resume moves workspace and sandbox
policy to global CLI options where necessary, retains every Stage 1 safety
configuration, passes the exact ID immediately after `exec resume`, and still
delivers the prompt only through stdin. `--last` and `--all` are rejected.

Metadata now retains every distinct ID seen in an explicit structured
`thread.started` event, the resume ID (when present), and the output-schema hash.
The workflow requires one unambiguous initial worker ID and the same ID on every
resume. Auditors continue to use fresh non-resumed ephemeral runs.
