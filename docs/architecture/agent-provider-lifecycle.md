# Agent provider lifecycle

How a Codex, Claude Code, Pi, or Hermes runtime moves between statuses, which
refreshes run when, how a provider account becomes anchored and pinned, and
what each operator action changes. Command-level provider interfaces live in
[Runtime harness dependencies](harness-dependencies.md); the proxy guards that
enforce the pins live in [Network controls](network-controls.md).

## Runtimes, statuses, and where state lives

Codex and Claude Code each carry one provider status. AWS Bedrock carries one
shared status that is projected into the Pi and Hermes runtime rows beside
their separate running-task counters. There is no Pi- or Hermes-specific
provider activation state. Status values are `deactivated`, `loading`,
`awaiting_login`, `active`, or `error`; `awaiting_login` applies only to the
OAuth providers, while enabled Bedrock is `awaiting_login` or `active`.

- **Status** is derived provider health, cached in orchestrator process memory. A fresh
  process reports `loading` until its first poll; nothing persists it.
- **The anchor** is the operator-approved provider account id, stored in the
  database. It is captured only through a completed operator login, is
  immutable afterwards, and outlives session expiry and deactivation until an
  operator reset clears it.
- **The proxy pin** is the per-request credential guard the network proxy
  enforces. It is published only by a refresh that commits `active` and is
  cleared by any refresh that commits anything else.
- **OAuth credentials** live in the agent user's provider auth files; the
  provider CLIs own them, including token refresh. The shared Bedrock IAM key
  lives encrypted in the admin database and never reaches an agent process.

## Anchor versus pin

The anchor and the pin answer two different questions, live in two different
places, and have two different lifetimes.

The anchor is the **decision**: which provider account the operator approved.
It is an account identity (the ChatGPT account id; the Claude account uuid the
provider attests for a token), captured once from operator-login evidence,
compared against every later probe, and cleared only by an operator reset.
The anchor cannot enforce anything by itself: it is a database row the
orchestrator consults.

The pin is the **enforcement** of that decision for the credential that most
recently proved it: the value the network proxy checks on every provider
request. Its shape is whatever each provider's requests actually expose —
OpenAI requests carry the ChatGPT account id, so the OpenAI pin is that id;
Claude requests carry only an opaque bearer token, so the Claude pin is
sha256 of the validated token.
The Claude pin therefore rotates with the token while the anchor never
changes; a rotated token must be re-attested to the same anchored account
before its hash becomes the new pin. The Bedrock harnesses have no pin and no
account anchor at all: the agent never holds a real AWS credential. It signs
with the dummy routing identity shared by the Bedrock launchers and the proxy
re-signs each allowed request with the operator's key. The routing identity is
not a Pi-versus-Hermes pin (each harness signs with its own fixed routing key
id, but only so the proxy attributes usage to the right runtime); the
connected credential (encrypted in the database, written only by the operator
API) is the approval. Pi and Hermes are separate task runtimes over one
provider, connected credential, working proxy credential, cached account
record, and validation lifecycle.

Publishing the pin is what the `active` transition *means*, which is why the
two are inseparable:

- A refresh that commits `active` has just live-validated a credential and
  checked it against the anchor. The pin write, the anchor check (or first
  capture), and the status record commit in one database transaction, so
  there is no window where the proxy admits traffic for a credential the
  orchestrator has not just validated — and no window where a slow probe
  republishes a pin for an account a concurrent operator reset just cleared.
- A refresh that commits anything else clears the pin in that same
  transaction: any non-active status means "no validated credential", so
  provider data-plane traffic fails closed at the proxy immediately (only the
  narrow pre-pin login bootstrap reads are exempt — see
  [Network controls](network-controls.md)). The anchor stays: the account is
  still approved, and the next login must still match it.

The anchor is the opposite: no refresh can ever rewrite it, from any state.
The refresh's account save re-writes the stored row's metadata every active
commit, but the identity in that row always comes from the anchor itself or
from an attestation that must equal it — a credential whose attested identity
differs fails the refresh with "account changed; reset the linked account"
(status `error`, pin cleared) instead of re-anchoring, and first capture
refuses to run while an anchor exists. The only path to a different account
is an operator reset (clears the anchor) followed by a fresh operator login
(captures the new one). The database enforces the same rule below the code:
a trigger on the account row refuses any single write that moves an anchored
account id, strips its approval marker, or deletes the row (see
[Admin state storage](admin-state-storage.md)).

An `active` commit does not require a non-active starting state, and the pin
is not write-once: every refresh writes the pin as part of its commit, and
the refresh commit is the pin's only writer besides the reset that clears it.
A steady recheck of an already-active runtime rewrites the same value; a
recheck that finds Claude Code rotated its token — routine CLI behavior —
attests the new token against the anchor and atomically replaces the pin
with the new token hash, all while the status stays `active` throughout.
Rotation therefore never passes through `awaiting_login`. Between the CLI
writing a rotated token and the refresh that replaces the pin, the old pin
denies the new token's traffic (fail closed); the scheduled recheck and the
pre-task Claude refresh are the convergence points that close that window.
Because the pre-task refresh converges before the turn's process spawns, a
task never starts inside that window: rotation can only fail a turn already
in flight whose token expires mid-turn (its CLI refreshes and retries against
the old pin). That failure is one-time and retryable — the next convergence
point replaces the pin — and it is rare in practice, because the five-minute
usage probe is itself a CLI invocation: an expired token normally refreshes
inside a refresh cycle, which detects the new hash and repins in the same
commit. There is deliberately no mid-turn repin machinery for the residual
case. The OpenAI pin is the anchored account id, so its active-state
rewrites never change the value.

## Status meaning and cadence

| Status | Meaning | Recheck cadence |
| --- | --- | --- |
| `deactivated` | The provider is disabled in the network policy. The proxy rejects its requests, processes close, and running tasks fail; queued tasks fail at their next claim. The operator-approved account or connected credential remains, so re-enabling can return directly to `active` with no new login. Disabling Bedrock projects `deactivated` into both harness rows and stops both runtime process pools. | every 5 seconds (a backstop: enabling the provider refreshes immediately) |
| `loading` | No poll has completed yet (process start). | every 5 seconds |
| `awaiting_login` | An OAuth runtime needs operator login, or enabled Bedrock needs its shared credential connected. OAuth proxy enforcement state is cleared; the anchor, if any, remains. | every 5 seconds |
| `active` | An operator-approved OAuth credential with live validation, or an enabled Bedrock provider with a synchronously validated credential row. | every 5 minutes |
| `error` | The last OAuth check failed, or stored Bedrock state is internally inconsistent. `error_message` carries the cause. Provider errors encountered during a Bedrock task fail that task and do not change this derived status. | every 5 seconds |

Tasks run only against an `active` runtime: a worker that claims a task while
the runtime is anything else fails it immediately with that status as the
error message, after re-checking the current network policy directly so a
just-disabled provider can never start a task. Queued work never parks behind
a missing login.

## Refresh triggers

Every trigger funnels into the same provider-connection refresh. One Bedrock
refresh updates the shared status shown by both Pi and Hermes:

| Trigger | When |
| --- | --- |
| Background poller | Per the cadence table above. |
| Policy change | Disabled providers deactivate synchronously; re-enabled ones refresh in the background. |
| Operator refresh | `POST /v1/agent-runtime/refresh` (the top-bar refresh button). |
| Login completion | Claude code submission refreshes directly; Codex device-login completion is observed by the next poll. |
| Credential connect | `POST /v1/agent-runtime/bedrock-credentials` synchronously validates STS identity, atomically stores only a successful key and its metadata, then locally refreshes enabled Bedrock runtimes. |
| Account reset | OAuth runtimes use `POST /v1/agent-runtime/reset-linked-account`; Bedrock uses `DELETE /v1/agent-runtime/bedrock-credentials`. |
| Task claim | Claude Code only, before each task turn (see below). |

## The refresh pipeline

1. **Policy gate.** A runtime whose provider is disabled marks `deactivated`:
   one mutation records the status, clears any pending OAuth record, clears
   the pin; live processes close and running tasks fail.
2. **Local account read.** The provider reports its own view: Claude Code via
   `claude auth status` plus the credential-file hash, Codex via
   `account/read {"refreshToken": false}` on a short-lived app-server, and
   Bedrock via its validated credential/account rows. A missing credential is
   `awaiting_login`. A present Bedrock row is already validated and becomes
   `active` without another AWS identity call.
3. **OAuth live validation.** A locally cached OAuth credential is never sufficient:
   - *Claude, steady token* (anchored, hash unchanged): run the
     `claude -p /usage` probe. The CLI authenticates through the proxy and
     owns refreshing an expired access token; the credential hash is re-read
     afterwards, so a refresh-rotation detected here continues to attestation
     instead of misclassifying as a broken login.
   - *Claude, new or rotated token*: skip the probe (the old pin, or no pin,
     denies it) — the profile attestation in step 5 is its live validation.
   - *Codex*: the `account/rateLimits/read` usage read authenticates live.
     If it fails for a pinned account, one `account/read
     {"refreshToken": true}` asks Codex to validate or refresh through the
     unpinned auth endpoint. An unpinned account never triggers that forced
     refresh: it cannot reach the guarded usage endpoint by construction, and
     its fate belongs to the approval flow, not credential recovery.
4. **Login capture.** The poll is the sole reader of a parked Codex device
   login; a completion observed here records the provider-signed account id
   for anchoring in this same refresh. Claude records sha256 of the token its
   completed login produced.
5. **Attestation (Claude).** A token hash not already on the anchor is
   attested: a read-only profile fetch returns the account uuid the provider
   itself binds to that token. First capture additionally requires the token
   hash recorded by the completed operator login, so agent-swapped
   credentials never inherit approval.
6. **Trust check and commit.** In one mutation, re-check the policy (a
   disable that landed during the slow probe wins), validate the probed
   account against the anchor — reject a changed account, capture a first
   anchor only from step 4/5 evidence — then save the account, publish or
   clear the pin, and record the status. Bedrock has no anchor or pin; it only
   records the local status.
7. **Usage backfill (Claude).** A first-capture or just-rotated token could
   not run the usage probe (its pin only went live at the commit), so the
   refresh reads usage once now; the admin UI shows usage immediately after
   login.

## What each recheck actually runs

Every trigger runs the same pipeline; what differs is only the state the
pipeline finds. Per state, the work and the provider traffic are:

| State found | Local work every recheck | Provider network calls |
| --- | --- | --- |
| `deactivated` | Policy read; re-assert the deactivated status, cleared OAuth record, cleared pin (idempotent writes). | None. |
| `awaiting_login`, no credential | Claude: `claude auth status` (reads the local credential file). Codex: `account/read {"refreshToken": false}` on the app-server (local account state); while a device login is in flight, drain its completion notifications. | None from the refresh itself. A parked Codex device login polls the provider's device-code endpoint on its own — that is the OAuth device flow the Codex CLI owns, bounded by the login's expiry, not part of the refresh. |
| `awaiting_login`, operator login in flight | Same local reads; the poll additionally drains the parked Codex login server's completion notifications (local IPC). The pending login record backs the UI's login card. | None from the refresh. The parked Codex server polls the provider's device-code endpoint until the login is approved or its code expires (the provider-prescribed device flow); Claude's browser flow makes no provider calls until the operator submits the code. |
| `awaiting_login`, login expired unfinished | The expired login record is dropped the next time anything reads it (the poll's login capture, or the UI's card fetch, which then shows Start login again); status is unchanged. | None; the device-code polling ends with the code's expiry. Starting a new login replaces the old parked flow. |
| `awaiting_login`, rejected credential still cached | Same local reads; the verdict memory answers without probing. | None from automatic checks. An explicit operator refresh probes once; login or reset replaces the credential. |
| `awaiting_login` → login just completed | Same local reads discover the new credential; Codex also reads the completed login's provider-signed account id (local helper). | One Claude profile attestation (root egress) to bind the new token to its account; Codex validates on its next recheck through the usage read. |
| `active` (five-minute recheck) | Policy read, local account read, credential hash re-read, commit. | Exactly one authenticated round trip: Claude runs `claude -p /usage` (the CLI may additionally call its OAuth token endpoint if the access token expired — that refresh is the point); Codex runs `account/rateLimits/read`. A Codex failure on a pinned account adds one `account/read {"refreshToken": true}`. A detected Claude rotation adds one profile attestation and one post-commit usage read. |
| `error` | Same local reads; the verdict memory answers until it expires. | None while the verdict holds; one normal live validation when it expires. |

Bedrock is simpler than this OAuth table. Its five-minute recheck reads policy,
the credential id, and the account metadata locally, and makes no AWS call at
all: STS is not called again after credential setup, and the cost display
needs no provider read because the proxy meters token usage out of each
Bedrock response as it happens (see the network controls doc).

Pre-task Claude refreshes run this same table and stay memory-only within the
verdict window. The operator refresh button deliberately bypasses verdict
memory and performs the provider check immediately.

## How a login lands

Starting a login is not a refresh — the login flow runs beside the poller,
and the refresh is what converts its completion into an anchor, a pin, and
`active`:

1. The operator starts a login (`POST /v1/agent-runtime/{codex,claude}-oauth-login`,
   available while the runtime is `awaiting_login` or `error`). Codex parks a
   device-code app-server that drives the provider's device flow; Claude
   starts a CLI login process and shows the login URL.
2. The provider-side flow completes outside TrustyClaw: the operator approves
   in the browser (Codex) or submits the code
   (`POST /v1/agent-runtime/claude-oauth-login/complete`, which finishes the
   CLI login and records sha256 of the token that login wrote).
3. The refresh observes the completion. The Claude complete endpoint triggers
   a refresh directly; the Codex completion is drained by the next
   five-second poll, which is the parked login server's only reader. That
   refresh anchors the identity from login evidence (the provider-signed
   Codex account id; the attested Claude account uuid for the recorded token
   hash), publishes the pin, and commits `active` — within one or two poll
   ticks of the login finishing.

## OAuth live-validation verdict memory

Every OAuth live-validation verdict is remembered in process memory, keyed by the
credential it judged, so validation generates provider traffic at most once
per scheduled recheck regardless of how often the five-second poll or task
claims re-enter the refresh. An explicit operator refresh bypasses this memory:

- An **authentication failure is final for automatic checks**: the runtime
  stays `awaiting_login` with zero background provider traffic. An explicit
  refresh probes once; an operator login or account reset replaces the
  credential and clears the verdict.
- Any **other failure** is `error` and expires after four minutes, under the
  five-minute recheck, so infrastructure failures recover on the next
  scheduled poll without a five-second retry loop.
- A **fresh `active` verdict** (with its usage snapshot) is reused until it
  expires, so the pre-task Claude refresh is normally memory-only.
- **Claude attestations** are memoized per token hash: a token's attested
  identity never changes, so one successful profile fetch answers every later
  recheck (including a runtime parked in account-mismatch `error`).
- Verdicts are process memory on purpose: a restart revalidates each OAuth
  credential once from scratch. Bedrock has no verdict memo; its validated row
  is durable provider state.

## The Bedrock provider lifecycle

The Pi and Hermes runtimes use one Bedrock provider pipeline and credential: one
static IAM access key pair the operator pastes, instead of an OAuth flow a
CLI owns. The differences, step by step:

- **Two useful runtime states.** While Bedrock is enabled, both runtime rows
  show `awaiting_login` until the shared credential is connected, then
  `active`. There is no separate checking, staged, or credential-error state.
- **One validated connection.** The connect endpoint passes the credential
  candidate directly to `sts:GetCallerIdentity`. Only after it succeeds does
  one transaction store the encrypted credential and selected region with its
  AWS account and IAM ARN. A rejected replacement never enters the database
  and leaves any previous validated connection unchanged.
- **Operator approval is the stored credential.** Only the operator API writes
  `bedrock_credentials`, and the agent has no database access, so the validated
  row is the approval. There is no second account anchor, working copy, error
  latch, or in-memory verdict.
- **The proxy reads that row directly.** It first enforces Bedrock policy and
  the fixed dummy routing identity, then decrypts the key only to re-sign the
  allowed request. Disabling is a soft product state and leaves the row intact.
  Connecting another key and region validates them from scratch and atomically
  replaces the connection and metadata.
- **No per-task convergence.** Static keys never rotate, so like Codex the
  cached status decides at task claim; there is no pre-task refresh.
- **Identity is checked at submission.** STS proves the key pair; a rejection
  returns directly from the credential request with no database change.
  `bedrock:InvokeModel*` and model access cannot be proven without a billable,
  model-specific call, so AWS checks those on the first task turn. Any later
  AWS rejection is the task's descriptive provider error; it does not create
  stored credential health. Setup does not silently invoke a model or require
  one probe model.
- **Cost is metered live, never polled.** The operator-facing month-to-date
  estimate comes from the token usage AWS reports in every Bedrock response,
  counted at the proxy per runtime, model, and UTC day, and priced at the
  host's on-demand catalog rates (see the network controls doc). No billing
  API is called, no billing IAM permission is required, and the display is
  current the moment a response completes; Pi and Hermes each show their own
  meter. AWS remains the authoritative bill.

The admin service does read and decrypt the credential directly from its
database, but it has no network egress. It injects the key pair into the
single-shot root-owned `read-aws-account` helper through its environment so
the helper can make the STS request over direct egress. This keeps the
existing host boundary intact: neither the admin service nor the agent gains
internet access, the agent-facing proxy exposes no STS route, and the
plaintext never touches disk.

## The pre-task Claude refresh

A Claude task claimed while the cached status is `active` runs the refresh
once more before the turn starts; a cached non-active status fails the task
directly, with no refresh. The refresh is not an extra auth probe — the
verdict memory answers it — but it is the repin convergence point: Claude
Code rotates its token during normal operation, and this refresh detects the
new hash, attests it, and republishes the pin before the turn's traffic would
hit the proxy. Codex turns carry the account id the proxy already pins, so
Codex has no per-task convergence and its cached status decides.

## Operator actions

- **Login** (`POST /v1/agent-runtime/{codex,claude}-oauth-login`): available
  while the runtime is `awaiting_login` or `error`. Completion produces the
  first-capture evidence in steps 4–5 and clears any remembered failure
  verdict.
- **Connect credentials** (`POST /v1/agent-runtime/bedrock-credentials`): the
  Bedrock connection action. It synchronously validates STS identity, even
  while Bedrock is disabled, and stores only a successful candidate. The
  write returns only `accepted`; enabled Bedrock becomes active from the
  stored validated row.
- **Disconnect** (`DELETE /v1/agent-runtime/bedrock-credentials`): one mutation
  deletes the shared credential and cached account metadata; both
  harnesses' live processes close and running tasks fail. There is no on-disk
  agent auth or remembered Bedrock verdict to clear. The next credential
  connect starts from scratch. The live usage counters are retained: they
  record work already done.
- **Refresh** (`POST /v1/agent-runtime/refresh`): re-derives status through
  the pipeline and forces OAuth provider probes. Bedrock has nothing extra to
  force: its status is local and its usage meters update as responses arrive.
