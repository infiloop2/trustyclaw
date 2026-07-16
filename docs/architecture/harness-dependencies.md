# Runtime Harness Dependencies

TrustyClaw treats Codex and Claude Code as external runtime harnesses. The host
owns process supervision, task state, network policy, and privilege boundaries,
but it depends on specific CLI protocols, auth files, and network request
shapes from those harnesses. This document lists the expectations that can
break when a harness package is upgraded.

## Current harnesses

| Harness | Package | Pinned version | Runtime id | Adapter |
| --- | --- | --- | --- | --- |
| Codex | `@openai/codex` | `0.144.0` | `codex` | `host/runtime/codex_app_server.py` |
| Claude Code | `@anthropic-ai/claude-code` | `2.1.206` | `claude_code` | `host/runtime/claude_code.py` |

Bootstrap installs these packages globally with npm and verifies the exact CLI
version strings before completing. A version bump should be treated as an
interface review, not a package-only change.

## Shared expectations

Both harnesses must keep these properties:

- They can run non-interactively as `trustyclaw-agent` with `HOME` set to
  `/mnt/trustyclaw-agent/agent-home`.
- They store durable harness state under the agent home so redeploys and service
  restarts preserve login and conversation continuity. Most conversation and
  session state is opaque to TrustyClaw: the harness only needs to keep it
  compatible with its own future versions.
- They respect `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`,
  `NODE_EXTRA_CA_CERTS`, and the local proxy CA enough for all data-plane and
  auth traffic to traverse the TrustyClaw proxy.
- They can be launched through a root-owned sudo helper that immediately
  demotes to `trustyclaw-agent`.
- A running task can be stopped by closing stdin and, if needed, terminating the
  child process.
- Their account state can be checked without interactive prompts and without
  giving `trustyclaw-admin` direct read access to the agent home. Unlike
  conversation/session state, the account helpers do parse specific auth file
  locations and fields listed below.

If any of those properties changes, the runtime status poller, task workers,
network guards, or privilege boundary can fail.

## Codex harness expectations

### Process interface

TrustyClaw starts Codex through:

```text
codex app-server --listen stdio://
```

The app-server is expected to speak newline-delimited JSON-RPC over stdio.
TrustyClaw sends `initialize` followed by the `initialized` notification before
any account or task calls.

Expected methods:

| Method | Expected behavior |
| --- | --- |
| `account/read` | Accepts a `refreshToken` boolean and returns a JSON object with an `account` field. Normal status reads pass `false`; if the live usage probe fails for a pinned account, TrustyClaw retries with `true` so Codex validates or refreshes its credential before the UI reports connected. A ChatGPT account contains `email` and `planType`; any provider-specific account type is intentionally ignored. A falsey account means login is still required. |
| `account/rateLimits/read` | Returns Codex usage-limit snapshots. TrustyClaw exposes only the default `rateLimits` snapshot in admin API responses; per-limit `rateLimitsByLimitId` entries and duplicated snapshot identity fields are intentionally not returned. Rate-limit windows contain `usedPercent`, `windowDurationMins`, and `resetsAt`; the default snapshot may contain `credits`. |
| `account/login/start` | Accepts `{"type": "chatgptDeviceCode"}` and returns `type`, `loginId`, `verificationUrl`, and `userCode`. |
| `thread/start` | Accepts `cwd`, `approvalPolicy`, `sandbox`, developer instructions, and the selected `model`. TrustyClaw appends the validated app manifest instructions for app-scoped threads. Returns `thread.id`. |
| `thread/resume` | Accepts `threadId`, `cwd`, the selected `model`, and refreshed developer instructions. Returns a resumed `thread.id`, or fails when the thread cannot be resumed. |
| `turn/start` | Accepts `threadId`, text input, and the selected `model` and `effort`. Returns `turn.id`. It may emit notifications before the response. |
| `turn/steer` | Accepts `threadId`, `expectedTurnId`, and text input. A `no active turn` error is treated as transient during startup. |

The pinned Codex catalog must advertise `gpt-5.6-terra` and `gpt-5.6-sol`
with `high`, `max`, and `ultra`, plus `gpt-5.6-luna` with `high` and `max`.
TrustyClaw intentionally exposes only that small subset; the API rejects
unsupported pairs before a task is queued.

Expected notifications:

| Notification | Expected behavior |
| --- | --- |
| `item/agentMessage/delta` | Carries partial assistant text in `params.delta`. |
| `item/completed` | Carries completed assistant messages as `params.item.type == "agentMessage"` with text in `params.item.text`. |
| `turn/completed` | Ends the turn. `params.turn.status == "completed"` is success; any other status must include enough error detail to fail the task. |

The adapter relies on responses and notifications being interleavable: a
notification may arrive while a request is waiting for its response, and the
client must be able to keep it for the task event stream.

### Auth and account identity

Codex device-code login must continue polling while the app-server process that
started login stays alive. TrustyClaw therefore keeps that app-server alive
until its completed login is captured as the trusted account, a new login
starts, or an operator resets the linked account.
Linked-account reset also clears local Codex auth files and closes live Codex
runtime processes, so a new login flow starts from an unlinked local auth
state.
First-account capture also requires that same parked app-server to emit a
successful `account/login/completed` notification with the matching `loginId`.
That notification (like `account/read`) carries only `loginId`, `success`, and
`error`; it does not include a ChatGPT account id. So the completion only
attests that the operator's device login for that `loginId` succeeded on the
app-server TrustyClaw started; the account id itself is read from the login
tokens through `read-codex-account-id` (the provider-signed `chatgpt_account_id`
claim) promptly after completion. An active `account/read` result by itself is
not operator approval for the stored device-code flow. The residual window
between the CLI writing `~/.codex/auth.json` and that read matches the Claude
first-capture path, and the linked account is shown to the operator once pinned.
The resulting OpenAI provider account row is tagged with
`operator_approval: "codex_device_login"`; rows without that marker are
legacy/unapproved state and never publish a proxy pin.

`account/read` is not assumed to expose the ChatGPT account id. TrustyClaw reads
the account id through `read-codex-account-id`, which parses a small part of
Codex auth state at:

```text
~/.codex/auth.json
```

Supported account-id sources, in order:

1. Top-level JSON object `tokens`, field `account_id`.
2. Top-level JSON object `tokens`, field `access_token`, parsed as a JWT. The
   decoded payload must contain object `https://api.openai.com/auth` with string
   field `chatgpt_account_id`.
3. Top-level JSON object `tokens`, field `id_token`, parsed as a JWT. The
   decoded payload must contain object `https://api.openai.com/auth` with string
   field `chatgpt_account_id`.

For steady-state status refresh, an active Codex account without one of those
account-id sources is treated as a runtime error, because the proxy cannot pin
OpenAI data-plane traffic to the logged-in account. TrustyClaw stores only the
operator-approved account id in admin and proxy state, not the tokens needed to
recreate Codex auth files.

TrustyClaw keeps the observed `account/read` identity fields (`email`, and
`planType` stored as the common `plan_type` field) in admin state; the Admin
API exposes stored account metadata as is, so sanitization happens once, at
capture. It reads Codex usage limits from
`account/rateLimits/read`, not from `account/read`, and exposes only the default
snapshot's `primary`, `secondary`, and `credits` fields under `codex_usage`.
For a pinned account, failure of that live read triggers one forced
Codex-owned credential refresh; an authentication failure becomes
`awaiting_login`, while a successful refresh may remain active without usage
metadata. An account that is not pinned yet cannot reach the guarded usage
endpoint at all, so its usage-read failure is routine: it stays a readable
account awaiting operator approval and its refresh token is never rotated.
The refresh verdict is remembered: an authentication failure stands, with no
further provider traffic, until an operator login or reset replaces the
credential, and any other failure is retried on the next scheduled recheck.
The proxy still receives only the account id needed for account pinning.

### Network request shape

The OpenAI managed provider policy depends on Codex/OpenAI traffic keeping these
request shapes:

- `auth.openai.com` is the only managed OpenAI auth domain. It is allowed for
  `GET` and `POST` and is not account-pinned.
- `api.openai.com` is a managed OpenAI data-plane domain. It is allowed only for
  `POST`, requires `ChatGPT-Account-Id`, and applies the external URL request
  guard.
- `chatgpt.com` is a managed ChatGPT/Codex data-plane domain. It is allowed for
  `GET` and `POST`, requires `ChatGPT-Account-Id`, and applies the
  external URL request guard.
- Codex must not require additional OpenAI, ChatGPT, or wildcard ChatGPT
  domains without updating the managed provider policy.
- ChatGPT/Codex data-plane requests carry `ChatGPT-Account-Id` matching the
  account id inferred from local auth files.
- Data-plane request bodies expose OpenAI tool declarations in parseable JSON
  when web search is requested.
- Cached web search uses `{"type": "web_search", "external_web_access": false}`
  with `indexed_web_access` false or absent, or on the standalone Codex search
  endpoints a body with `settings.external_web_access: false` and no
  `settings.indexed_web_access: true`. This is the only web-tool shape forwarded;
  everything else is denied (fail-closed).
- Non-cached web access — `web_search` with `external_web_access` enabled or
  omitted, `indexed_web_access: true` (Codex `indexed` mode: OpenAI fetches
  server-approved external URLs), `web_search_preview` (including dated
  variants), a bare `web`/`web_fetch`/`browser`/`computer_use`/`code_interpreter`
  tool, any tool carrying a truthy `*_web_access` flag, Chat Completions
  `web_search_options`/search models, or a standalone search request without the
  cached setting — is denied by the proxy. New or renamed web tools fail closed:
  a Codex upgrade that adds a web/browse tool type not matched here still needs a
  guard re-audit, but is denied by default rather than forwarded.
- Remote MCP tools are declared as parseable `type: mcp` tool objects (with a
  `server_url` or hosted `connector_id`), so the proxy can deny them.

Bootstrap also installs `/etc/codex/requirements.toml` to pin Codex web search
to cached (`allowed_web_search_modes = ["cached"]`, which excludes `live` and
`indexed`) and disable Codex app/plugin/browse feature surfaces (`apps`,
`plugins`, `tool_search`, `tool_suggest`, `computer_use`, `remote_plugin`,
`plugin_sharing`) so the agent does not attempt a proxy-denied tool, plus
`/mnt/trustyclaw-agent/agent-home/.codex/config.toml` from
`host/bootstrap/agent-home/.codex/config.toml`. That file must set
`approval_policy = "never"`, `sandbox_mode = "danger-full-access"`, and trust
`/mnt/trustyclaw-agent/agent-home`. Bootstrap installs it root-owned, readable,
and immutable so the agent cannot edit or delete the active policy file. The
proxy guard is still required as the web-search enforcement layer. The root-owned
managed config layer `/etc/codex/managed_config.toml` also registers the bundled
tools MCP server (`mcp_servers.trustyclaw` spawning `host.runtime.tools_mcp_shim`),
so Codex must keep reading both root-owned `/etc/codex` layers and spawning
configured stdio MCP servers as the runtime user.

## Claude Code harness expectations

### Process interface

TrustyClaw starts one Claude Code process per task turn through:

```text
claude -p --input-format stream-json --output-format stream-json --verbose \
  --model <model> --effort <effort> \
  --setting-sources user --strict-mcp-config \
  --mcp-config <inline JSON for the bundled tools MCP shim> \
  [--append-system-prompt <validated app instructions>]
```

TrustyClaw passes the session selection on every new and resumed process.
Claude Code `2.1.206` accepts the exposed model aliases `opus`, `fable`, and
`sonnet`; it also accepts `high`, `max`, and the session-only `ultracode`
effort. `ultracode` combines xhigh effort with dynamic workflow orchestration,
so an older CLI that silently ignores that value is not compatible.

Bootstrap installs `/mnt/trustyclaw-agent/agent-home/.claude/settings.json`
from `host/bootstrap/agent-home/.claude/settings.json` (root-owned, readable,
immutable). It sets `permissions.defaultMode = "bypassPermissions"` and
`skipDangerousModePermissionPrompt = true`; `--setting-sources user` keeps stale
local or project settings out of the task harness while still allowing
`CLAUDE.md` instructions to load, and makes this file the only loaded settings
source.

WebSearch availability follows the operator's
`managed_network_integrations.claude.web_search` toggle (default off) and is
applied at launch, not written to disk. The orchestrator — the only side with a
database role — reads the toggle in `run()` and states the decision to the
launcher as its required first argument (`web-search=on`/`web-search=off`). The
launcher (`host/bootstrap/helpers/run-claude-code.sh`) is authoritative for the
enforcement: on `web-search=off` it appends
`--settings '{"permissions":{"deny":["WebSearch"]}}'` to the Claude invocation
itself, so the deny is built and verifiable in one place rather than trusted
from its caller. That CLI settings override is always loaded regardless of
`--setting-sources`, a `deny` rule applies in every mode (including
`bypassPermissions`) and wins first-match, and the agent cannot influence the
launched command — so there is no file for the agent to tamper with and no way
to re-enable the tool. Non-agent maintenance calls (auth, usage) run no model
turn, so they pass `web-search=off` and keep the deny-by-default posture.
`WebFetch`
and `Bash` stay enabled — their egress is client-side and already gated by the
domain allow-list. The launcher also sets
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (telemetry/feedback/auto-update
suppression; it does not affect WebSearch/WebFetch) for every invocation except
the host-owned `/usage` probe. Claude Code `2.1.206` classifies its account-limit
fetch as nonessential, so the probe must omit this environment variable or it
exits successfully without returning any usage windows.

The network proxy is the ultimate layer and enforces the same toggle
independently: `anthropic_external_url_request_guard` on `api.anthropic.com`
always denies server-side `web_fetch`/`code_execution`/remote-MCP declarations,
and denies `web_search` unless `anthropic_allow_web_search` (expanded from the
toggle) is set. So even if the harness settings layer were bypassed, web search
stays off unless the operator enabled it.

`--strict-mcp-config` plus the inline `--mcp-config` make the bundled tools
shim (`host.runtime.tools_mcp_shim`, spawned as `trustyclaw-agent`) the only
MCP server; with no tools enabled it lists nothing. The invocation
deliberately does not pass `--safe-mode`: the pinned CLI drops every non-SDK
MCP server in safe mode, which would disable the bundled tools entirely. The
agent's isolation comes from the OS boundaries (dedicated user, nftables,
policy proxy), not from harness flags, and `--strict-mcp-config` already
ignores any MCP configuration outside the host-supplied one.

Bootstrap installs the same `host/bootstrap/agent-home/agents_claude.md`
contents to `/mnt/trustyclaw-agent/agent-home/AGENTS.md` and
`/mnt/trustyclaw-agent/agent-home/CLAUDE.md`. That source file must tell agents
they are running on a TrustyClaw host with full local shell/file permissions,
must not prompt for local approvals, and must use TrustyClaw network-policy
failures as operator allowlist requests. The installed host files are also
root-owned, readable, and immutable.

When resuming a thread, TrustyClaw appends:

```text
--resume <session_id>
```

Expected stdin shape:

```json
{"type":"user","message":{"role":"user","content":"..."},"parent_tool_use_id":null}
```

Expected stdout messages:

| Message | Expected behavior |
| --- | --- |
| `type == "assistant"` | Assistant text is read from `message.content[]` blocks where `type == "text"`. |
| `type == "result"` | Ends one submitted user message when `subtype == "success"` and `is_error` is not true. |
| `session_id` | May appear on assistant or result messages; the final turn must provide a session id so TrustyClaw can resume future tasks. |

Steering is implemented by writing more user messages to the same stream while
the process is running. The adapter waits for one successful `result` per user
message submitted to that process, including steers.

### Auth and account identity

TrustyClaw requires Claude Code to use Claude.ai OAuth, not another auth method.
Account status is checked through:

```text
claude auth status --json
```

Expected status fields:

| Field | Expected behavior |
| --- | --- |
| `loggedIn` | `true` means the CLI believes it is logged in. Missing or false means `awaiting_login`. |
| `authMethod` | Must be `claude.ai`; any other value is an error. |
| `email`, `orgId` | Used as optional metadata when helper-read auth files do not already expose equivalent values. |
| `accountId`, `account_id`, `userId`, `userID`, `user_id` | Optional provisional account-id sources within a single status probe. The identity that gets anchored and displayed always comes from token attestation (below), never from these agent-writable fields. Legacy stored Claude rows without `identity_attestation: "anthropic_oauth_profile"` are treated as no anchor (like an unapproved OpenAI row), so a plain operator re-login re-captures them through the first-capture attestation gate; no separate reset is required. |

`loggedIn` is only Claude Code's local credential state, so it is not enough to
publish an active runtime. Once a token is pinned, every steady-state status
refresh also runs:

```text
claude -p /usage --output-format json
```

This live probe makes Claude Code authenticate and gives the CLI ownership of
refreshing an expired access token. TrustyClaw reads the credential hash again
after the probe. If refresh rotated it, the old proxy pin can safely deny that
probe's first retry; the orchestrator notices the new hash, attests it through
the profile endpoint below, and atomically replaces the pin. First capture and
a rotation already visible at the start of a check use that same live profile
attestation directly; the refresh then reads usage once, right after the new
pin is published, so usage metadata is available immediately after login. A
steady-token authentication failure becomes `awaiting_login`; another steady
probe failure becomes `error`.

The probe's verdict is memoized per token hash. Active runtimes are rechecked
every five minutes and immediately before each Claude task, but only a
recheck whose memo has expired runs the probe, so the pre-task check is
normally memory-only. An `awaiting_login` verdict never expires: that token
is rejected and no background retry can fix it. An explicit refresh probes
once; an operator login (which mints a new token) or an account reset replaces
the credential. An `error`
verdict expires with the memo, so infrastructure failures recover on the next
scheduled recheck. Attestation results are memoized per token hash the same
way: a token's attested identity never changes, so one successful profile
fetch answers every later recheck of that token. The explicit operator refresh
bypasses verdict memory and probes immediately.

Login starts with:

```text
claude auth login --claudeai
```

The login command must print a line matching:

```text
If the browser didn't open, visit: <https-url>
```

TrustyClaw returns that URL to the admin UI, then later writes the browser code
to the same process stdin.

The proxy guard pins Anthropic data-plane traffic on the OAuth bearer token
hash. `read-claude-account` parses a small part of Claude Code auth state from
one of these locations. In production, both the Claude launcher and account
helper set `CLAUDE_CONFIG_DIR=/mnt/trustyclaw-agent/agent-home/.claude`.

| Data | Expected locations |
| --- | --- |
| OAuth account metadata | `/mnt/trustyclaw-agent/agent-home/.claude/.claude.json`, `/mnt/trustyclaw-agent/agent-home/.claude.json`, or `~/.claude.json` |
| OAuth tokens | `/mnt/trustyclaw-agent/agent-home/.claude/.credentials.json` or `~/.claude/.credentials.json` |

The credentials file must contain `claudeAiOauth.accessToken`. The helper stores
only `sha256(accessToken)` plus optional `account_id`, `organization_id`, and
`email`; it never copies the bearer token into admin or proxy state.

When the operator submits the browser code, TrustyClaw reads that hash once
right after the login command finishes and records it on the completed OAuth
row. First account capture only accepts an attestation of that exact token: the
admin API passes the approved hash to `read-claude-account --attest`, and the
helper verifies the current credential hash before any profile request. Agent
credentials swapped after completion do not inherit the operator's approval;
the remaining swap window is the moment between the CLI writing the file and
this read, and the linked account is shown in the admin UI once pinned.

### Account identity attestation

The Anthropic proxy pin is the bearer-token hash and follows token rotation, so
the durable account anchor is attested against the token itself instead of
being read from agent-writable files. Whenever the observed token hash differs
from the anchored one — first operator login and every token rotation —
`read-claude-account --attest` calls:

```text
GET https://api.anthropic.com/api/oauth/profile
Authorization: Bearer <claudeAiOauth.accessToken>
```

Expected response fields:

| Field | Expected behavior |
| --- | --- |
| `account.uuid` | Required. The account the token belongs to. Must match the anchored account id; on first capture during an operator OAuth login it becomes the anchor. |
| `account.email`, `organization.uuid` | Optional identity metadata stored alongside the anchor. |

Properties this depends on:

- This is the same private endpoint Claude Code itself calls during login
  bootstrap — it is one of the pre-pin allowlisted paths in
  `host/runtime/network_policy.py` — so the pinned harness version already
  requires it to exist and accept the OAuth bearer.
- The attest call runs as root over direct host egress, not through the proxy:
  the agent uid can only reach the local proxy (whose account guard would
  reject a just-rotated token mid-attest), and the admin uid has no egress.
  The bearer token never leaves the helper process; only its hash and the
  attested identity are returned to admin code.
- Anchored tokens skip the call entirely, so steady-state status refreshes
  make no extra network requests.

If Anthropic changes this endpoint's auth or response shape, Claude token
rotations degrade to a retryable runtime `error` (with the attestation failure
in the message) until this integration is updated; unchanged tokens keep
working. Treat the endpoint like the other harness interfaces in this document
during upgrade reviews.

TrustyClaw also extracts the observed `subscriptionType` value from
`claude auth status --json` into the common Admin API `plan_type` field.
Claude usage is read with:

```text
claude -p "/usage" --output-format json
```

On pinned Claude Code `2.1.206`, the command returns a JSON object whose
`result` string contains lines like:

```text
Current session: 0% used · resets Jul 11, 1am (UTC)
Current week (all models): 0% used · resets Jul 3, 3:59pm (UTC)
Current week (Fable): 0% used · resets Jul 3, 3:59pm (UTC)
```

TrustyClaw parses each window line independently: `Current session` into
`claude_usage.current_session_*`, `Current week (all models)` into
`claude_usage.weekly_*`, and `Current week (Fable)` into
`claude_usage.fable_weekly_*`; other model-specific week lines are ignored.
Each window carries `used_percent` and,
when its reset time parses, `resets_at`. Reset times are Unix timestamps;
TrustyClaw converts the provider's UTC text while capturing the snapshot; a
reset in any other timezone label drops only that window's `resets_at`. A line
that does not match contributes nothing, and the snapshot keeps whatever did
parse. When no usage window parses, `claude_usage` is absent; TrustyClaw never
presents percentages from an older provider read as the current snapshot.

TrustyClaw therefore cannot recreate Claude Code auth files from admin state.
Doing that would require storing refresh/access tokens or equivalent provider
secrets, tracking the harness's private auth file format, and taking
responsibility for token refresh.

### Network request shape

The Claude managed provider policy depends on Claude Code traffic keeping these
request shapes:

- `platform.claude.com` is the only managed Claude OAuth domain. It is allowed
  for `GET` and `POST`, and only for paths matching `^/v1/oauth(?:/.*)?$`.
- `api.anthropic.com` is the only managed Anthropic API domain. It is allowed
  for `GET` and `POST` and is account-pinned with the Claude OAuth bearer-token
  hash.
- Claude Code must not require `claude.ai`, `claude.com`, wildcard Anthropic
  domains, or additional Anthropic API domains without updating the managed
  provider policy.
- Data-plane Anthropic API calls carry `Authorization: Bearer <token>` matching
  the OAuth token hash read from Claude Code credentials.
- Before the token hash is known, only the narrow Claude Code bootstrap profile
  and settings endpoints listed in `host/runtime/network_policy.py` are allowed.

If Claude Code changes its auth domain, token storage, bearer-token use, or
pre-pin bootstrap endpoints, the managed Claude policy must be updated with the
harness upgrade.

## Upgrade review checklist

Before changing a harness version:

1. Confirm bootstrap installs the intended package and verifies the exact
   version string.
2. Run the unit tests for the changed adapter and network policy guards.
3. Verify login status and login start flows on a real host for every changed
   harness.
4. Verify the account helper still reads the account id or bearer-token hash
   without broadening `trustyclaw-admin` filesystem access, and that
   `read-claude-account --attest --expected-token-sha256 <hash>` rejects a
   mismatched local token before egress and still resolves the expected token to
   the expected `account.uuid` through the profile endpoint.
5. Verify a task can start, stream messages, accept steering, complete, and be
   killed.
6. Verify thread/session resume still works after a second task on the same
   `thread_id`.
7. Verify managed provider network events still show the expected account guard
   behavior and no unexpected denied bootstrap traffic.
8. Update this document, adapter tests, managed provider policy, and stage smoke
   expectations in the same change if any interface changes.
