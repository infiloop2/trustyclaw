# Runtime Harness Dependencies

TrustyClaw treats Codex and Claude Code as external runtime harnesses. The host
owns process supervision, task state, network policy, and privilege boundaries,
but it depends on specific CLI protocols, auth files, and network request
shapes from those harnesses. This document lists the expectations that can
break when a harness package is upgraded.

## Current harnesses

| Harness | Package | Pinned version | Runtime id | Adapter |
| --- | --- | --- | --- | --- |
| Codex | `@openai/codex` | `0.140.0` | `codex` | `host/runtime/codex_app_server.py` |
| Claude Code | `@anthropic-ai/claude-code` | `2.1.177` | `claude_code` | `host/runtime/claude_code.py` |

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
| `account/read` | Accepts `{"refreshToken": false}` and returns a JSON object with an `account` field. A ChatGPT account contains `email` and `planType`; any provider-specific account type is intentionally ignored. A falsey `account` means login is still required. |
| `account/rateLimits/read` | Returns Codex usage-limit snapshots. TrustyClaw exposes only the default `rateLimits` snapshot in admin API responses; per-limit `rateLimitsByLimitId` entries and duplicated snapshot identity fields are intentionally not returned. Rate-limit windows contain `usedPercent`, `windowDurationMins`, and `resetsAt`; the default snapshot may contain `credits`. |
| `account/login/start` | Accepts `{"type": "chatgptDeviceCode"}` and returns `type`, `loginId`, `verificationUrl`, and `userCode`. |
| `thread/start` | Accepts `cwd`, `approvalPolicy`, `sandbox`, and developer instructions. Returns `thread.id`. |
| `thread/resume` | Accepts `threadId` and `cwd`. Returns a resumed `thread.id`, or fails when the thread cannot be resumed. |
| `turn/start` | Accepts `threadId` and text input. Returns `turn.id`. It may emit notifications before the response. |
| `turn/steer` | Accepts `threadId`, `expectedTurnId`, and text input. A `no active turn` error is treated as transient during startup. |

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
until a later account check reports active login or a new login starts.

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

An active Codex account without one of those account-id sources is treated as a
runtime error, because the proxy cannot pin OpenAI data-plane traffic to the
logged-in account. TrustyClaw stores only the derived account id in admin and
proxy state, not the tokens needed to recreate Codex auth files.

TrustyClaw keeps the observed `account/read` identity fields (`email` and
`planType`) in admin state. The Admin API exposes the Codex plan as the
common `plan_type` field. It reads Codex usage limits from
`account/rateLimits/read`, not from `account/read`, and exposes only the default
snapshot's `primary`, `secondary`, and `credits` fields under `codex_usage`.
The proxy still receives only the account id needed for account pinning.

### Network request shape

The OpenAI managed provider policy depends on Codex/OpenAI traffic keeping these
request shapes:

- `auth.openai.com` is the only managed OpenAI auth domain. It is allowed for
  `GET` and `POST` and is not account-pinned.
- `api.openai.com` is a managed OpenAI data-plane domain. It is allowed only for
  `POST`, requires `ChatGPT-Account-Id`, and applies the live-web-search guard.
- `chatgpt.com` is a managed ChatGPT/Codex data-plane domain. It is allowed for
  `GET` and `POST`, requires `ChatGPT-Account-Id`, and applies the
  live-web-search guard.
- Codex must not require additional OpenAI, ChatGPT, or wildcard ChatGPT
  domains without updating the managed provider policy.
- ChatGPT/Codex data-plane requests carry `ChatGPT-Account-Id` matching the
  account id inferred from local auth files.
- Data-plane request bodies expose OpenAI tool declarations in parseable JSON
  when web search is requested.
- Cached web search uses `{"type": "web_search", "external_web_access": false}`.
- Live web search either uses `web_search_preview` or `web_search` with
  external access enabled or omitted, so the proxy can deny it.

Bootstrap also installs `/etc/codex/requirements.toml` to pin Codex web search
behavior to cached search. The proxy guard is still required as the enforcement
layer.

## Claude Code harness expectations

### Process interface

TrustyClaw starts one Claude Code process per task turn through:

```text
claude -p --input-format stream-json --output-format stream-json --verbose \
  --permission-mode bypassPermissions --safe-mode --strict-mcp-config
```

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
| `accountId`, `account_id`, `userId`, `userID`, `user_id` | Optional account id sources. If none is present, email is used only as user-facing account metadata. |

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

TrustyClaw also extracts the observed `subscriptionType` value from
`claude auth status --json` into the common Admin API `plan_type` field.
Claude usage is read with:

```text
claude -p "/usage" --output-format json
```

On observed Claude Code `2.1.195`, the command returns a JSON object whose
`result` string contains lines like:

```text
Current session: 0% used
Current week (all models): 0% used · resets Jul 3, 3:59pm (UTC)
```

TrustyClaw parses only that observed text shape into
`claude_usage.current_session_used_percent`, `claude_usage.weekly_used_percent`,
and `claude_usage.weekly_resets_at_text`. If the CLI response changes shape or
the command fails, TrustyClaw omits `claude_usage` instead of guessing.

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
   without broadening `trustyclaw-admin` filesystem access.
5. Verify a task can start, stream messages, accept steering, complete, and be
   killed.
6. Verify thread/session resume still works after a second task on the same
   `thread_id`.
7. Verify managed provider network events still show the expected account guard
   behavior and no unexpected denied bootstrap traffic.
8. Update this document, adapter tests, managed provider policy, and stage smoke
   expectations in the same change if any interface changes.
