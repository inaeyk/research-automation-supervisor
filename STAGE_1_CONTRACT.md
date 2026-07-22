# Stage 1 Contract — Deterministic Codex Process Adapter

Status: human-approved implementation contract
Contract schema version: 1
Stage ID: `AUTOMATION-1`

## Goal

Implement a deterministic, testable adapter that launches one non-interactive
Codex process from an exact human-written prompt file, captures its JSONL event
stream and final response, enforces fixed safety and timeout policies, writes a
durable run record, and returns a normalized result.

Stage 1 is transport and process control only. It does not decide what prompt to
send or what workflow step should happen next.

## Preconditions

- Stage 0 is complete and tagged `stage0-complete`.
- The existing Stage 0 CLI, contract validation, doctor diagnostics, and tests
  remain passing.
- The installed Codex CLI supports:
  - `codex exec`;
  - prompt input from standard input using `-`;
  - `--json`;
  - `--output-last-message`;
  - `--sandbox`;
  - `--ask-for-approval`;
  - `--ephemeral`;
  - `--ignore-user-config`;
  - `--ignore-rules`.

## Non-goals

Do not implement any of the following in Stage 1:

- model-generated prompts;
- supervisor reasoning or stage planning;
- worker-to-auditor handoffs;
- session resume or repair conversations;
- automatic retries;
- Git branches, commits, tags, or worktrees;
- acceptance-test execution outside Codex;
- notifications, email, browser control, or background services;
- scheduling or parallel runs;
- stage advancement or checkpoint decisions;
- OpenAI API calls or API-key management;
- network-enabled Codex runs;
- `danger-full-access`, `--yolo`, or sandbox bypasses;
- output-schema-driven model responses;
- project-specific Gregory–Laflamme logic.

## Required repository changes

Create at least:

```text
src/research_automation_supervisor/
    codex_adapter.py
    codex_models.py
    redaction.py
tests/
    fixtures/fake_codex.py
    test_codex_adapter.py
    test_codex_models.py
    test_redaction.py
examples/codex_requests/minimal-worker.yaml
examples/prompts/minimal-worker.md
docs/codex_adapter.md
```

Update `cli.py` and other existing files only as required. Additional small,
well-separated modules are allowed.

Do not edit:

- `STAGE_0_CONTRACT.md`
- `CODEX_STAGE_0_PROMPT.md`
- `STAGE_1_CONTRACT.md`
- `CODEX_STAGE_1_PROMPT.md`

## Required CLI

The installed command remains `research-supervisor`.

### `research-supervisor validate-codex-request PATH [--json]`

Load and validate a Codex run request. It must perform no writes and must not
launch Codex.

### `research-supervisor run-codex PATH [--runs-dir PATH] [--json]`

Validate the request, launch Codex, write the run artifacts, print a concise
normalized result, and exit using the Stage 1 exit-code contract.

`--runs-dir` defaults to `runs/codex` relative to the caller's current
directory. The adapter must create `<runs-dir>/<run_id>` exclusively and refuse
to overwrite or reuse an existing run directory.

Human-readable output is the default. `--json` must emit stable JSON and no
unstructured traceback.

## Codex run request model

Implement a strict typed request containing exactly:

- `schema_version: int`, currently exactly `1`;
- `run_id: str`;
- `role: supervisor | worker | auditor`;
- `workspace: str`;
- `prompt_path: str`;
- `model: str`;
- `reasoning_effort: low | medium | high | xhigh`;
- `timeout_seconds: int`.

Validation requirements:

- unknown fields are rejected;
- required strings remain non-empty after trimming;
- `run_id` uses a conservative identifier format and is at most 80 characters;
- `model` uses a conservative model-name format and is at most 80 characters;
- timeout is between 30 and 14,400 seconds inclusive;
- request YAML uses the existing strict safe loader, including duplicate-key
  rejection and useful sanitized errors;
- `workspace` and `prompt_path` resolve relative to the request file's parent
  when not absolute;
- the resolved workspace exists, is a directory, and belongs to a Git worktree;
- the resolved prompt is a regular UTF-8 file, is nonempty after trimming, and
  is at most 1 MiB;
- path-resolution and decoding failures become sanitized input errors;
- the prompt is read once before process launch and its SHA-256 is recorded.

The request must not contain executable paths, approval policies, arbitrary
Codex flags, environment variables, output paths, or sandbox overrides.

## Fixed role policy

The adapter, not the request, owns these policies:

| Role | Sandbox | Approval | Session persistence |
|---|---|---|---|
| `supervisor` | `read-only` | `never` | persistent |
| `worker` | `workspace-write` | `never` | persistent |
| `auditor` | `read-only` | `never` | ephemeral |

"Persistent" means the adapter omits `--ephemeral`. Stage 1 records any session
or thread identifier exposed by structured events but does not resume it.

No role may use `danger-full-access`, additional writable directories, approval
reviewers, automatic approval, or network access.

## Exact Codex invocation policy

Construct an argument vector. Never invoke a shell.

The command must be semantically equivalent to:

```text
codex exec
  --json
  --output-last-message <adapter-controlled temporary path>
  --model <validated model>
  -c model_reasoning_effort=<validated effort>
  -c web_search="disabled"
  -c sandbox_workspace_write.network_access=false
  -c features.skill_mcp_dependency_install=false
  --sandbox <role-derived sandbox>
  --ask-for-approval never
  --ignore-user-config
  --ignore-rules
  --strict-config
  --cd <resolved workspace>
  [--ephemeral for auditor only]
  -
```

Requirements:

- pass the exact prompt bytes to standard input;
- the prompt content must not appear in the process argument vector;
- do not use `--skip-git-repo-check`;
- do not use `--add-dir`;
- do not use `--search`;
- do not use `--full-auto`;
- do not use sandbox-bypass flags;
- set the process working directory to the resolved workspace as well as passing
  the explicit Codex `--cd` value;
- use explicit UTF-8 handling with replacement for subprocess diagnostics;
- use a discovered `codex` executable in production and dependency injection
  for tests;
- do not run `codex login`, install software, or access the network.

## Subprocess environment

Build a copied environment rather than mutating `os.environ`.

Before launch, remove variables whose names case-insensitively contain any of:

```text
TOKEN
SECRET
PASSWORD
PASSWD
API_KEY
APIKEY
CREDENTIAL
COOKIE
SESSION
AUTHORIZATION
```

Do not remove `HOME`, `PATH`, locale variables, or `CODEX_HOME` merely because
they are needed for ordinary CLI operation. Never read or copy Codex
authentication files.

Record only the names of removed variables, never their values. Never record a
complete environment dump.

## Process lifecycle

The implementation target for Stage 1 is Linux, macOS, and WSL2.

Requirements:

- launch in a new process session/group;
- stream standard output rather than buffering the entire run in memory;
- capture standard error with a bounded implementation;
- enforce the hard request timeout;
- on timeout, terminate the whole process group;
- wait a short fixed grace period, then force-kill the group if needed;
- reap the process and record the observed exit status;
- handle launch errors, broken pipes, invalid subprocess bytes, and interrupted
  reads without an unstructured traceback;
- use monotonic time for duration and timeout decisions;
- use UTC timestamps for records;
- never automatically rerun a failed or timed-out process.

The adapter must cap accepted JSONL stdout at 100 MiB and captured stderr at
10 MiB. Exceeding either limit terminates the process group and returns a
normalized failure without retaining excess content.

## JSONL event handling

Codex `--json` standard output is newline-delimited JSON.

Requirements:

- parse each nonblank line as one JSON object;
- reject non-object JSON values;
- write one canonical, redacted JSON object per line to `events.jsonl`;
- never write the unredacted event stream;
- count valid events;
- record malformed-line count and SHA-256 hashes of malformed lines, but do not
  persist their raw contents;
- extract a session/thread identifier only from an explicit structured field;
- do not infer success solely from model-written natural language;
- tolerate unknown event types while preserving their redacted JSON structure.

## Redaction

Apply redaction before writing event strings, stderr, final output, or rendered
diagnostics.

At minimum redact:

- `Authorization: Bearer ...`;
- common bearer/API-token forms beginning with `sk-`, `ghp_`, `github_pat_`,
  `xoxb-`, or `xoxp-`;
- values assigned to case-insensitive keys containing `token`, `secret`,
  `password`, `passwd`, `api_key`, `apikey`, `credential`, `cookie`,
  `session`, or `authorization`;
- sensitive environment-variable values removed before launch, if they appear
  in captured output.

Use deterministic placeholders. Redaction must work recursively on JSON objects
without changing non-string scalar types. It must be idempotent.

The authoritative prompt file is human-controlled input and is not rewritten by
the redactor. Its contents must not be printed by the CLI.

## Run artifacts

Create `<runs-dir>/<run_id>` before launch using exclusive creation. Write at
least:

```text
request.normalized.json
prompt.sha256
events.jsonl
stderr.log
final-message.md
metadata.json
result.json
```

Requirements:

- `request.normalized.json` contains resolved paths and fixed role policy but no
  secret values;
- `prompt.sha256` contains the prompt hash, not the prompt text;
- `events.jsonl`, `stderr.log`, and `final-message.md` contain only redacted
  content;
- `metadata.json` records:
  - package version;
  - run ID and role;
  - resolved workspace and prompt path;
  - prompt hash and byte count;
  - model and reasoning effort;
  - sandbox, approval policy, and ephemeral setting;
  - sanitized command argument vector;
  - removed environment-variable names;
  - start/end UTC timestamps and monotonic duration;
  - Codex executable path and sanitized version when available;
  - process exit code or terminating signal;
  - valid and malformed event counts;
  - stdout/stderr byte counts;
  - session/thread identifier when explicitly available;
- `result.json` contains the normalized final result;
- the command record uses a placeholder such as `<PROMPT_FROM_STDIN>` and never
  includes prompt content;
- no artifact may contain values removed from sensitive environment variables;
- metadata and result finalization use atomic replacement;
- a failure still leaves a useful, internally consistent run directory.

## Normalized result

Implement a strict immutable result with at least:

- `schema_version`;
- `run_id`;
- `status`;
- `exit_code`;
- `started_at`;
- `ended_at`;
- `duration_seconds`;
- `artifact_directory`;
- `event_count`;
- `malformed_event_count`;
- `final_message_present`;
- `permission_evidence`;
- `summary`;
- `error`.

Allowed status values:

- `succeeded`;
- `launch_failed`;
- `timed_out`;
- `output_limit_exceeded`;
- `permission_blocked`;
- `malformed_event_stream`;
- `process_failed`;
- `missing_final_message`.

Classification precedence:

1. launch failure;
2. timeout;
3. output limit exceeded;
4. permission/sandbox denial with conservative evidence;
5. malformed JSONL event stream;
6. nonzero process exit;
7. missing or empty final message;
8. succeeded.

A run succeeds only when:

- Codex exits `0`;
- every nonblank stdout line is a JSON object;
- no output limit was exceeded;
- a nonempty final-message file exists;
- no permission-block evidence exists.

## Permission-block classification

Classification must be conservative.

Set `permission_evidence=true` only when a non-successful run contains an
allowlisted permission/sandbox/network-denial signal in:

- standard error; or
- a structured event whose type explicitly denotes error, failure, denial, or
  command failure.

Do not classify from ordinary assistant-message text. Recognized evidence may
include normalized phrases such as:

- `permission denied`;
- `operation not permitted`;
- `sandbox denied`;
- `sandbox violation`;
- `approval required`;
- `network access disabled`;
- `network is disabled`;
- `read-only file system`.

If evidence is ambiguous, use `process_failed`, not `permission_blocked`.

## Exit-code contract

- `0`: succeeded;
- `2`: invalid request, path, prompt, YAML, or CLI input;
- `3`: missing or unsupported local environment dependency;
- `4`: Codex process failed;
- `5`: timeout;
- `6`: permission or sandbox blocked;
- `7`: malformed event stream, missing final message, or output limit exceeded;
- unexpected internal failure: `1`.

Human and JSON CLI output must agree with `result.json`.

## Required tests

Tests must use a fake Codex executable or injected process boundary. They must
not use a real model, login, network, or user Codex configuration.

Cover at least:

### Request and path validation

- valid request for each role;
- unknown and duplicate YAML fields;
- invalid identifiers, models, efforts, and timeout boundaries;
- relative-path resolution from the request directory;
- missing/non-directory/non-Git workspace;
- missing, non-regular, empty, oversized, and invalid-UTF-8 prompt;
- existing run-directory collision;
- symlink/path-resolution failures where applicable.

### Exact process construction

- prompt passed through stdin and absent from argv;
- exact role-derived sandbox, approval, and ephemeral policies;
- model and reasoning override;
- web and workspace network disabled;
- skill dependency installation disabled;
- ignored user config and rules;
- explicit workspace in argv and process cwd;
- no forbidden flags;
- sensitive environment names removed and only names recorded;
- production executable discovery and injected fake executable.

### Process outcomes

- successful JSONL and final response;
- nonzero process exit;
- launch failure;
- timeout with process-group termination;
- child process does not survive timeout;
- permission-blocked stderr;
- permission-blocked structured failure event;
- permission-like ordinary assistant text does not trigger classification;
- malformed JSON;
- non-object JSON;
- missing and empty final response;
- stdout and stderr size limits;
- invalid subprocess bytes;
- interrupted or broken-pipe behavior where practical.

### Artifacts and security

- complete artifact set on success;
- useful artifact set on each failure class;
- no prompt in argv, CLI output, metadata command, or stderr;
- recursive and idempotent redaction;
- sensitive environment values absent from every artifact;
- malformed-line raw text absent, with only hashes recorded;
- stable canonical JSONL;
- atomic metadata/result finalization;
- CLI human/JSON agreement and all Stage 1 exit codes;
- existing Stage 0 behavior remains passing.

## Documentation

`docs/codex_adapter.md` must explain:

- trust boundary and non-goals;
- request format;
- fixed role policies;
- exact subprocess policy;
- artifacts;
- status and exit-code meanings;
- timeout and process-group behavior;
- redaction limits;
- how tests use the fake executable;
- that Stage 1 does not generate prompts or advance workflows.

## Required quality gates

Before completion, all must pass:

```bash
ruff check .
mypy src
pytest -q
```

## Completion report

The worker's final response must state:

- files changed;
- architecture implemented;
- exact command policy;
- process and timeout behavior;
- artifact and redaction behavior;
- exact quality-gate results and test count;
- assumptions;
- every unmet requirement, deviation, or blocker.

A silent deviation is a failure.
