# Security and permissions

## Trust model

The operator trusts frozen human authority, the local package, Git, selected fixed
tests, and the selected Codex installation. Worker, Auditor, Supervisor, test, and
evaluator output are untrusted inputs. Deterministic schemas and state rules constrain
what those outputs can change, but cannot prove their semantic truth.

Use a dedicated clean worktree with only the project and visible control inputs needed
for the task. Narrow `allowed_paths`; protect contracts, prompts, tests, configuration,
credentials, and unrelated source. Never place private prepared campaigns or hidden
evaluation material below a model workspace or an added writable/readable root.

## Codex process policy

The package runs Worker with workspace-write and Supervisor/Auditor with read-only,
all with approval `never`, web search and workspace network disabled, skill dependency
installation disabled, and user config/rules ignored. Approval `never` is safe here
only in combination with frozen role policy and bounded roots; it is not a general
recommendation for interactive Codex use.

For ordinary direct Codex use, prefer the normal workspace sandbox and on-request
approvals. Official guidance distinguishes the sandbox (technical access) from the
approval policy (when a human must confirm). The `--yolo` /
`--dangerously-bypass-approvals-and-sandbox` flag removes both boundaries. Use it only
inside an externally hardened, disposable environment whose entire accessible
filesystem and network are trusted. Research Automation Supervisor never adds it.

## Credentials and logs

Credential-shaped environment names are removed before child processes. Structural
values that the mandatory redactor would change are rejected before run creation.
Stored model events and diagnostics are bounded and redacted; exact prompt bytes are
hashed but not persisted by the workflow.

Do not put secrets in YAML, prompts, command arguments, worktree paths, Git remotes,
fixtures, or test stdout. Redaction reduces accidental disclosure; it is not a reason
to supply unnecessary credentials.

## Protected evaluation boundary

The visible campaign receives no gold, hidden tests, exact references, historical
evaluator paths, or evaluation package locator. A completed immutable candidate is the
one-way handoff. Stop all model processes before a separate host process receives both
candidate and prepared campaign.

The direct replay command does not start models. It uses disposable workspaces and
compares input fingerprints afterward. Its raw stdout/stderr artifacts are private
untrusted data and must not enter model context or public distribution.

Model/evaluator process separation is mandatory. Filesystem packaging, Bubblewrap, or
Docker may add defense in depth, but no container makes it acceptable to evaluate while
a model process can reach protected material.

## Filesystem rules

- Do not edit or chmod immutable candidates or prepared campaigns.
- Keep runtime `runs/`, candidates, private reports, prepared campaigns, gold, and
  fixtures outside distributions and source commits.
- Use `--keep-workspaces` only for private diagnosis, then remove retained trees after
  review.
- Do not follow symlinks from visible control authority or candidate overlays.
- Review every fixed acceptance command: the Stage 2 runner does not provide arbitrary
  test executables with a separate OS-level network namespace.

## PA-5C4 prelaunch and repository boundary

Started scientific inputs live only in the protected core prelaunch-authority store.
Custodian previews/cards are replaceable projections and cannot authorize a campaign.
The Start receipt is committed before any environment or repository action. Every
retry must present the exact store-key HMAC launch token and matching campaign/intent
identity; corrupt, missing, stale, and cross-campaign objects fail closed.

Selected repositories are untrusted before Bubblewrap. SafeGit permits only direct
audited Git built-ins for identity/object inspection and an HTTPS or local-source
`--no-checkout` clone. It supplies a sterile HOME, disables system/global config,
hooks, fsmonitor, external diff, attributes files, credential helpers and prompts, and
denies command-executing/unsupported transports. Checkout and preparation commit run
only inside Bubblewrap `--unshare-all`. A content-hashed receipt preserves the exact
argv/environment/isolation proof.

The PA-2 Physics Oracle runner has a narrower, distinct policy. It accepts no CLI argv
and executes only a trusted catalog's hash-pinned system-Python intent. Bubblewrap
`--unshare-all` enforces disabled networking, the workspace is a read-only bind mount,
and scratch is the sole writable host mount. The child receives a new fixed environment
rather than inherited variables. If this exact capability is unavailable, PA-2 fails
closed without launching the oracle. See [trusted Physics Oracle
execution](physics_oracle_execution.md). This does not change the ordinary Stage 2
acceptance-test limitation above.

PA-3 and PA-4 add a separate Physics Auditor boundary. Every audit uses a fresh,
ephemeral, approval-never Codex session inside an exact read-only Bubblewrap projection.
The original worktree, Git metadata, PA-2 oracle programs, PA-2 evidence root, protected
paths, credentials, and other model sessions are absent. Only declared candidate and
derivation objects plus engine-owned bounded evidence are projected. PA-4 verifies the
standalone PA-3 action proof and refuses reused provider thread IDs or any workspace
identity drift. See [physics workflow integration](physics_workflow_integration.md).

See the official [Codex approval and security guide](https://learn.chatgpt.com/docs/agent-approvals-security.md)
for current CLI sandbox and approval behavior.
