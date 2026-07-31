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

See the official [Codex approval and security guide](https://learn.chatgpt.com/docs/agent-approvals-security.md)
for current CLI sandbox and approval behavior.
