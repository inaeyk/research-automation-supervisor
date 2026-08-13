# Campaign Custodian and zero-shell UX (PA-5C4)

PA-5C4 adds a local, low-privilege Campaign Custodian around the qualified Supervisor.
It changes the operator experience, not scientific authority. A person can create,
start, supervise, resume, and inspect a campaign without a terminal, virtual
environment, Git branch operation, run identity, configuration file, or direct Codex
command.

## Ordinary-user installation and launch

On Windows with WSL available:

1. An administrator performs the one-time Core Authority Service installation with
   `scripts/install-core-authority-service.sh`, naming the ordinary operator account.
   This creates the non-login `research-supervisor-core` identity, a service-only
   authority store, and the authenticated local socket. No later campaign action
   needs administrator authorization.
2. Double-click **Research Supervisor** (`Research Supervisor.vbs`) in the qualified
   project folder. It is a hidden-window Windows Script Host entry point; no terminal
   is opened. `first-run-research-supervisor.cmd` remains a one-time compatibility
   bootstrap only.
3. The launcher locates the supported default WSL backend without asking for a distro name,
   creates a managed environment below the user's local data directory, installs the
   qualified package, waits for loopback readiness, and opens the local browser UI.
4. Double-click **Research Supervisor** thereafter. It reuses an existing
   healthy backend and updates the managed installation only when the qualified source
   commit changes.

The scripts never switch the user's Git branch. If WSL, Python, or an OS-owned
dependency needs administrator action, the launcher displays a Windows message and
stops before campaign launch. Backend diagnostics are written locally and are not the
primary user message.

## Exact zero-shell workflow

The default path is:

1. Choose **New Campaign**.
2. Choose an existing Git repository using the folder picker, or paste a
   credential-free HTTPS Git URL. Unsupported transports fail closed.
3. Paste or drop the Research Contract and Research Plan.
4. Enter an ordinary-language campaign name and Initial Task. Supporting files are
   optional.
5. Review the repository version, input identities, acceptance profile, editable
   areas, environment readiness, and immutability warning.
6. Press **Start** once. The Custodian sends the complete canonical request over the
   authenticated local socket. The Core Authority Service imports exactly one sanitized
   repository snapshot, freezes the complete input bundle, and fsyncs all objects before
   atomically publishing the Start receipt. An identical retry with the same request
   identity returns the exact intent; any changed supplied field fails closed.
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

The Custodian may prepare its replaceable UI directories, ask the Core service for a
non-authoritative repository preview, present inputs, inspect environment readiness,
invoke a fixed qualified-runner
operation, read operator-safe projections, store the user's exact exchange response,
display core-allowlisted evidence, export verified results, open a repository folder,
and send a local browser notification.

The Custodian module has no import or callable surface for a workflow engine, PA-5A
implementation, Worker, Auditor, model adapter, PA-2, PA-3, PA-5C2 scorer, or hidden
fixture authority. It has no durable campaign-state or journal filename. It cannot
write campaign state, journal, proof, completion, or model action records. Its only
campaign-affecting process target is `qualified_runner`, with the allowlisted
operations `start`, `status`, `resume`, `respond`,
`artifact`, `repository`, and `export`.

Custodian-owned data is physically separate:

```text
custodian-state/
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

/var/lib/research-supervisor-core/
  authority/                       # mode 0700, service identity only
    store-key-v1
    objects/<sha-prefix>/<launch-intent-sha>.json
    receipts/<start-request-sha>.json
    frozen-inputs/<launch-intent-sha>.json
  snapshots/
    receipts/
    workspaces/

/run/research-supervisor-core/
  authority.sock                   # mode 0660, peer UID checked
```

The Custodian and operator-exchange roots are explicitly non-authoritative. The core
authority root is owned by the non-login `research-supervisor-core` identity and mode
0700. The ordinary Custodian UID cannot traverse, read, write, rename, or replace its
key, objects, receipts, or frozen inputs. A card contains only the public campaign ID,
bundle SHA-256, and opaque HMAC-bound immutable intent ID; it contains no authoritative
scientific bytes.
A Custodian card or process exit can never create a completed projection.

## Core IPC and atomic Start

The service accepts one strict JSON request per Unix-socket connection and authenticates
the peer with Linux `SO_PEERCRED`. Envelope and operation payload models all use
`extra="forbid"`; unknown operations and fields fail closed. The only operations are
`inspect_repository`, `create_start_intent`, `get_start_intent`,
`list_operator_campaigns`, `verify_start_intent`, and
`consume_start_intent_for_qualified_launch`. There is no file, command, or generic data
operation.

At **Start**, `create_start_intent` locks the store and validates the complete
`CampaignLaunchRequestV1`: request and preview identities; name; repository locator hash,
captured device/inode, commit, and tree; exact contract, plan, task, and every support
file; and every profile, model, reasoning, repair, editable-area, and execution choice.

While holding a no-follow repository directory descriptor, core imports one sanitized
snapshot, revalidates device/inode, builds the complete `CampaignInputBundleV1`, and
publishes content-addressed request, intent, and frozen-input objects. Every file and
directory is fsynced. The request-keyed receipt is the sole atomic commit point. Crashes
before it mean no Start; crashes after it mean exactly one immutable Start. Receipt
enumeration reconstructs cards after a lost response, deleted/replaced Custodian state,
or restart. Reuse compares the full canonical request hash; there are no weakly bound
fields.

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

The Core service derives `FrozenCampaignInputV1`/`CampaignInputBundleV1` during atomic
Start, before publishing the receipt. The qualified runner consumes those bytes only by
immutable intent ID. The installed runner exposes
`start --launch-intent <immutable-id>` and has no `start --bundle` option. Every later
runner operation re-verifies the core intent and exact local bundle binding before
entering the existing strict visible-campaign schemas.
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

The one-time administrator installer creates only the service identity, shared socket
group, managed service environment, service-owned authority/snapshot roots, and systemd
unit. The ordinary launcher creates user-owned UI data and its managed Python
environment; it fails closed if the Core socket is absent or inaccessible. The backend
then verifies Python/package identity, Git, Codex
version, Codex authentication, Bubblewrap, WSL/Linux backend, and atomic rename and
hard-link filesystem capabilities. Preview performs only sterile identity inspection.
Repository preparation uses `/usr/bin/git` with system/global config disabled, a
sterile HOME, hooks and fsmonitor disabled, external diff/textconv/helper paths
neutralized, prompts disabled, and `ext`, file, SSH, and git protocols denied. All
production Git calls in Custodian/core/bootstrap/setup and resume are mechanically
confined to one SafeGit implementation or occur inside the already-qualified sandbox. New
remote repositories use only credential-free HTTPS and
`clone --no-checkout --no-tags --no-recurse-submodules`. Existing roots are opened once
with no-follow directory flags, bound into import by descriptor, and revalidated by
device/inode after import. They use the same no-checkout clone after exact commit/tree
revalidation. Checkout and the preparation
commit run only under Bubblewrap `--unshare-all`; the receipt records every exact
argv/environment and asserts `checkout_outside_isolation=false`. The clone-generated
`.git/config` is replaced by core's minimal sterile configuration. After receipt
publication no production path returns to the original repository.

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

Focused tests cover authenticated/schema-closed IPC, actual two-UID store denial in the
privileged installation job, every atomic Start crash boundary, complete-field reuse,
deleted/replaced Custodian state, arbitrary-bundle removal, stale/cross-campaign intents,
descriptor/inode path swaps, original mutation after snapshot, hostile Git configuration,
and a mechanical production Git-call inventory, in addition to strict models/self-hashes,
frozen input tamper detection,
create-once exchange behavior, traversal and symlink rejection, stale/duplicate and
cross-campaign responses, completion-notification proof binding, environment leakage,
CSRF/loopback browser isolation, static Custodian authority imports, unsafe recovery
cards, duplicate Start, browser/Custodian restart, authentication setup, result export,
and refusal to synthesize completed status from an unverified state file.

`tests/real_windows_browser_acceptance.py` starts Windows Script Host on the actual
`Research Supervisor.vbs`, which starts the real WSL backend and Windows Chrome. The
test attaches Playwright to that Windows browser and performs no operator backend API
calls. It starts with no run ID, terminal, activated environment, distro name, branch,
or port entered by a user. It performs the ordinary wizard, preview, Start, injected
recoverable interruption, UI Continue, Human Action inspection and response, verified
completion status, report access, and export. It also stops and restarts the backend,
restarts the browser page, reuses an already-running backend, and exercises the Windows
plain-language WSL failure path. The machine-readable result is
`docs/validation/pa5c4-real-browser-evidence.json`.
