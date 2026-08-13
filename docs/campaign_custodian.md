# Campaign Custodian and zero-shell UX (PA-5C4)

PA-5C4 adds a local, low-privilege Campaign Custodian around the qualified Supervisor.
It changes the operator experience, not scientific authority. A person can create,
start, supervise, resume, and inspect a campaign without a terminal, virtual
environment, Git branch operation, run identity, configuration file, or direct Codex
command.

## Ordinary-user installation and launch

On Windows with WSL available:

1. Double-click `first-run-research-supervisor.cmd` in the qualified project folder.
2. The launcher locates the default WSL backend without asking for a distro name,
   creates a managed environment below the user's local data directory, installs the
   qualified package, waits for loopback readiness, and opens the local browser UI.
3. Double-click `launch-research-supervisor.cmd` thereafter. It reuses an existing
   healthy backend and updates the managed installation only when the qualified source
   commit changes.

The scripts never switch the user's Git branch. If WSL, Python, or an OS-owned
dependency needs administrator action, the launcher displays a Windows message and
stops before campaign launch. Backend diagnostics are written locally and are not the
primary user message.

## Exact zero-shell workflow

The default path is:

1. Choose **New Campaign**.
2. Choose an existing Git repository using the folder picker, or paste an HTTPS/SSH
   Git URL. The Custodian creates a separate detached worktree automatically.
3. Paste or drop the Research Contract and Research Plan.
4. Enter an ordinary-language campaign name and Initial Task. Supporting files are
   optional.
5. Review the repository version, input identities, acceptance profile, editable
   areas, environment readiness, and immutability warning.
6. Press **Start** once. The exact bundle is frozen. Duplicate browser submissions
   return the original public campaign card instead of creating another campaign.
7. Follow the dashboard. If qualified core authority needs a human decision, inspect
   safe evidence, choose an allowed response, add a note/file if appropriate, and
   submit it. The Custodian never preselects an answer.
8. If interrupted, choose **Continue**. The button requests qualified recovery; it
   does not reset or infer a safe boundary.
9. After verified durable completion, open the Scientific Report, Worker Reports,
   Auditor Reports, changed-file evidence, or provenance; open the prepared repository;
   or export the verified campaign bundle.

No user step contains a shell command or YAML/JSON editing.

## Progressive disclosure

The dashboard deliberately defaults to human state:

> Campaign is running  
> Stage: Implementing task-M2-C  
> Last activity: Auditor reviewing Worker changes

The default projection does not contain internal workflow-state names, journal
sequences, run tokens, session IDs, or proof hashes. Stable technical reason codes are
available only under **Technical details**. Detailed campaign proof and journal
artifacts remain in the qualified evidence tree and are not the default result page.

## Custodian permission boundary

The Custodian may prepare directories and detached worktrees, clone a repository,
freeze an input bundle, inspect environment readiness, invoke a fixed qualified-runner
operation, read operator-safe projections, store the user's exact exchange response,
display core-allowlisted evidence, export verified results, open a repository folder,
and send a local browser notification.

The Custodian module has no import or callable surface for a workflow engine, PA-5A
implementation, Worker, Auditor, model adapter, PA-2, PA-3, PA-5C2 scorer, or hidden
fixture authority. It has no durable campaign-state or journal filename. It cannot
write campaign state, journal, proof, completion, or model action records. Its only
campaign-affecting process target is `qualified_runner`, with the allowlisted
operations `start`, `status`, `resume`, `respond`, `artifact`, and `export`.

Custodian-owned data is physically separate:

```text
custodian-state/
  bundles/
  campaigns/
  previews/
  runner-logs/
operator-exchange/
  campaign-…/
    requests/
    responses/
    notifications/
    uploads/
qualified-campaigns/
workspaces/
```

The first two roots are explicitly non-authoritative. A Custodian card or process exit
can never create a completed projection.

## CampaignInputBundleV1

`CampaignInputBundleV1` is strict, frozen, extra-field-forbidden, and canonically
self-hashed. It contains:

- `campaign_public_id` and `human_name`;
- `repository`: source kind/display, credential-free locator hash, prepared workspace,
  exact baseline commit/tree, and repository ID;
- `research_contract`, `research_plan`, and `initial_task`: display name, media type,
  canonical base64 bytes, byte count, and SHA-256;
- `supporting_files`: a bounded tuple with the same byte manifest;
- `requested_settings`: named profile, exact Worker/Auditor/Supervisor models and
  reasoning, repair limit, and normalized editable areas;
- `bundle_sha256`: SHA-256 of canonical JSON excluding only the self-hash field.

The qualified core copies and revalidates the bundle before materializing the existing
strict visible-campaign schemas. Every later operation reloads the self-hashed bundle.
An edit after Start is detected; the UI exposes no edit operation. A scientific input
change therefore requires a new campaign or a core-issued qualified human-action path.

## HumanActionRequestV1 and HumanActionResponseV1

A core-issued `HumanActionRequestV1` binds:

- the public campaign and frozen input-bundle identities;
- human stage/substage, request ID, plain reason, and exact question;
- response type and bounded options with deterministic consequences when known;
- opaque allowlisted safe-evidence tokens;
- the safe-state flag;
- exact durable authority: state and journal file identities, journal head, and frozen
  policy identity;
- a canonical request self-hash.

A user-authored `HumanActionResponseV1` repeats the campaign, request, bundle, and exact
durable-authority bindings; contains only an allowed selection, bounded note, and/or
hash-bound exchange uploads; and has its own canonical self-hash. Requests and
responses are create-once regular files. Symlinks, traversal, replacement, duplicate
submission, stale heads, cross-campaign substitution, and request replay fail closed.
The qualified core revalidates every binding before translating the exact response to
the existing human-decision ingress.

## Environment bootstrap behavior

The first-run launcher safely creates only user-owned data directories and a managed
Python environment. The backend then verifies Python/package identity, Git, Codex
version, Codex authentication, Bubblewrap, WSL/Linux backend, and atomic rename and
hard-link filesystem capabilities. Repository selection creates a clean detached
worktree at the previewed commit, without switching the source branch. HTTPS Git URLs
with embedded credentials and unsafe paths are rejected.

Missing login, administrator permission, isolation, or filesystem capability produces
an Action Needed card. The card states that the campaign has not started. Codex sign-in
is launched only through the qualified runner; the Custodian does not invoke Codex.

## Recovery delegation and failure UX

**Continue** enters `resume_qualified_campaign`. For an interrupted current child it
first asks PA-5A to build a plan. Only PA-5A `auto_resume` and
`finish_finalization` dispositions are executed. Active matching processes remain
running; ambiguous, stale, reused, foreign, changed-authority, and unsafe plans become
plain blocked cards. The outer visible campaign resumes only after that qualified
child result. PA-5C3 remains the only Physics benchmark campaign recovery authority;
the Custodian neither calls PA-2/PA-3 nor approximates PA-5C3 routing.

Expected setup and core errors are mapped to a title, explanation, safe-state statement,
and stable technical code. Unexpected exceptions receive an opaque local error ID and
are logged below `custodian-state`; a traceback is never the primary browser message.

## Notifications and results

Browser notifications are opt-in. The UI notifies on human input, blocked
infrastructure, or verified completion. A completion notification is schema-invalid
unless the qualified projection has `completion_verified=true`; that projection is
built only after the existing campaign status verifier revalidates durable completion
and candidate authority. Process exit is never a completion source.

The completed screen defaults to outcome, repository, final stage/commit when
available, Worker/Auditor/repair/human-decision counts, and a concise executive
summary. Core-derived safe artifacts back the report buttons. Export is a qualified
operation and includes only the frozen input bundle, operator results, verified
campaign report, and final candidate—never raw sessions, credentials, hidden expected
routes, scorer-only inputs, or model logs.

## Security and acceptance coverage

Focused tests cover strict models/self-hashes, frozen input tamper detection,
create-once exchange behavior, traversal and symlink rejection, stale/duplicate and
cross-campaign responses, completion-notification proof binding, environment leakage,
CSRF/loopback browser isolation, static Custodian authority imports, unsafe recovery
cards, duplicate Start, browser/Custodian restart, authentication setup, result export,
and refusal to synthesize completed status from an unverified state file.

The scripted browser acceptance starts with no run ID, configuration file, terminal
operation, or activated user environment. It performs the ordinary wizard, preview,
Start, injected recoverable interruption, UI Continue, Human Action inspection and
response, verified completion, notification derivation, report access, and export.
