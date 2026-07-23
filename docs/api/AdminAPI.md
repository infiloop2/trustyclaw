# Admin API

The TrustyClaw admin API is served by the localhost admin service.

Base URL after port forwarding:

```text
http://127.0.0.1:7443
```

Every API request must include:

```text
Authorization: Bearer <admin-password>
```

API responses are JSON. Static UI assets are the exception: `GET /`,
`GET /oauth/callback`, the admin CSS/JavaScript/favicon paths, and installed app
UI assets under `/v1/apps/{app_id}/ui/` are served without the bearer password.
They return only static files and perform no state change. Every API route,
including `GET /v1/apps` and every app backend proxy request, requires the
bearer admin password.

## Errors

Every non-2xx response returns this JSON envelope:

```json
{
  "error": {
    "message": "Human-readable error message"
  }
}
```

Error response fields:

| Field | Required | Type | Values | Meaning |
| --- | --- | --- | --- | --- |
| `error.message` | Yes | string |  | Human-readable error message for logs and operator display. |

Error status codes:

| HTTP status | Meaning |
| --- | --- |
| `400` | Request JSON, query string, or field value is invalid. |
| `401` | Missing or invalid admin password. |
| `403` | An authenticated app bridge attempted to target a different app or an app backend attempted a disallowed host route. |
| `404` | Requested resource or route does not exist. |
| `409` | Request conflicts with current runtime, task, approval, or credential state. |
| `413` | Request body exceeds the 1 MiB admin API limit. |
| `502` | An installed app backend or delegated tools service is unavailable or returned an invalid response. |
| `500` | Host-side error. |

## Health

```text
GET /v1/health
```

Response:

```json
{
  "status": "ok",
  "agent_name": "trustyclaw-dev-agent",
  "agent_runtime": {
    "runtimes": [
      {
        "type": "codex",
        "status": "active",
        "active_task_ids": []
      },
      {
        "type": "claude_code",
        "status": "deactivated",
        "active_task_ids": []
      }
    ]
  },
  "network_controls": {
    "status": "active"
  },
  "version": {
    "status": "ok",
    "runtime": "x.y.z",
    "state": "x.y.z"
  },
  "upgrade": {
    "available": true,
    "latest": "x.y.z"
  },
  "host_runtime": {
    "cpu": {
      "usage_percent": 12.5
    },
    "memory": {
      "used_bytes": 980000000,
      "total_bytes": 2147483648
    },
    "filesystem": {
      "mounts": {
        "root": {
          "used_bytes": 6000000000,
          "total_bytes": 17179869184
        },
        "admin": {
          "used_bytes": 250000000,
          "total_bytes": 17179869184
        },
        "agent": {
          "used_bytes": 500000000,
          "total_bytes": 8589934592
        }
      }
    },
    "swap": {
      "allocated_bytes": 6442450944,
      "used_bytes": 536870912
    }
  }
}
```

Response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `status` | enum | `ok`, `degraded` | Overall host health. `ok` means the admin service, agent runtime supervisor, and network controls are reachable. `degraded` means the admin service is responding but at least one component is not healthy. |
| `agent_name` | string |  | Host name from the input config. |
| `agent_runtime.runtimes` | array |  | Status records for every supported runtime. |
| `agent_runtime.runtimes[].type` | enum | `codex`, `claude_code`, `hermes` | Agent runtime type. |
| `agent_runtime.runtimes[].status` | enum | `deactivated`, `loading`, `awaiting_login`, `active`, `error` | Current agent runtime supervisor state. |
| `agent_runtime.runtimes[].active_task_ids` | string array |  | Currently running task ids for this runtime. |
| `network_controls.status` | enum | `active`, `error` | Derived network policy enforcement state. |
| `version.status` | enum | `ok`, `mismatch`, `error` | Version health for the running root volume and preserved admin state. |
| `version.runtime` | string or null |  | TrustyClaw version from `/opt/trustyclaw-host/VERSION`. |
| `version.state` | string or null |  | TrustyClaw preserved-state version from admin disk `version.json`. |
| `upgrade.available` | boolean |  | Whether the public `infiloop2/trustyclaw` main-branch version is newer than the running version. This advisory check does not affect overall health. |
| `upgrade.latest` | string or null |  | Latest valid version returned by a successful public-repository check, or `null` until the first check succeeds after service start. A failed later check preserves the last successful value. |
| `host_runtime.cpu.usage_percent` | number | 0-100 | Current host CPU usage percentage. |
| `host_runtime.memory.used_bytes` | integer |  | Current host memory used, in bytes. |
| `host_runtime.memory.total_bytes` | integer |  | Total host memory, in bytes. |
| `host_runtime.filesystem.mounts.root.used_bytes` | integer |  | Current root filesystem used space, in bytes. |
| `host_runtime.filesystem.mounts.root.total_bytes` | integer |  | Total root filesystem capacity, in bytes. |
| `host_runtime.filesystem.mounts.admin.used_bytes` | integer | optional | Current admin data volume (`/mnt/trustyclaw-admin`) used space, in bytes. |
| `host_runtime.filesystem.mounts.admin.total_bytes` | integer | optional | Total admin data volume (`/mnt/trustyclaw-admin`) capacity, in bytes. |
| `host_runtime.filesystem.mounts.agent.used_bytes` | integer | optional | Current agent data volume (`/mnt/trustyclaw-agent`) used space, in bytes. |
| `host_runtime.filesystem.mounts.agent.total_bytes` | integer | optional | Total agent data volume (`/mnt/trustyclaw-agent`) capacity, in bytes. |
| `host_runtime.swap.allocated_bytes` | integer |  | Filesystem-backed RAM swap allocated to the host, in bytes. |
| `host_runtime.swap.used_bytes` | integer |  | Current filesystem-backed RAM swap used, in bytes. |

Runtime status is `deactivated` when that runtime's managed provider
integration is disabled, `loading` while the runtime is starting,
`awaiting_login` while an OAuth runtime needs operator login, `active` while it
can accept work, and `error` when the runtime supervisor cannot make it
healthy. Hermes has no OAuth flow: while Bedrock is enabled it is
`awaiting_login` until a validated credential is connected, then `active`.

`network_controls.status` is derived, not stored. It is `active` when the
persisted network policy is valid and the proxy process is listening. It is
`error` when the policy cannot be parsed or policy enforcement is not healthy.
The `error` state fails closed and denies all network access.

## Agent Runtime

```text
GET  /v1/agent-runtime/status
GET  /v1/agent-runtime/account
POST /v1/agent-runtime/refresh
POST /v1/agent-runtime/codex-oauth-login
GET  /v1/agent-runtime/codex-oauth-login
POST /v1/agent-runtime/claude-oauth-login
GET  /v1/agent-runtime/claude-oauth-login
POST /v1/agent-runtime/claude-oauth-login/complete
GET  /v1/agent-runtime/bedrock-credentials
POST /v1/agent-runtime/bedrock-credentials
DELETE /v1/agent-runtime/bedrock-credentials
POST /v1/agent-runtime/reset-linked-account
```

Agent runtime endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agent-runtime/status` | none | Agent runtime status response | Returns current state for every runtime. |
| `GET` | `/v1/agent-runtime/account` | none | Agent account response | Returns the current account status for every runtime. |
| `POST` | `/v1/agent-runtime/refresh` | Agent runtime refresh request | Agent account response | Attempts to refresh provider account/status for one runtime or all runtimes, then returns the current account response. |
| `POST` | `/v1/agent-runtime/codex-oauth-login` | none | Codex OAuth login response | Starts a Codex OAuth login flow and returns the device code and login link. |
| `GET` | `/v1/agent-runtime/codex-oauth-login` | none | Codex OAuth login response | Returns the current Codex OAuth device code and login link. |
| `POST` | `/v1/agent-runtime/claude-oauth-login` | none | Claude OAuth login response | Starts a Claude Code OAuth login process and returns the login link. |
| `GET` | `/v1/agent-runtime/claude-oauth-login` | none | Claude OAuth login response | Returns the current Claude Code OAuth login link. |
| `POST` | `/v1/agent-runtime/claude-oauth-login/complete` | `{"code": "..."}` | status response | Submits the browser login code back to the waiting Claude Code OAuth process. |
| `GET` | `/v1/agent-runtime/bedrock-credentials` | none | `{"connected": false}` or `{"connected": true, "access_key_id": "AKIA...", "region": "us-east-1"}` | Returns whether the Bedrock connection is stored plus its non-secret access key id and region. The secret is never returned. |
| `POST` | `/v1/agent-runtime/bedrock-credentials` | `{"access_key_id": "AKIA...", "secret_access_key": "...", "region": "us-east-1"}` | `{"status": "accepted"}` | Synchronously validates the Bedrock long-term IAM access key pair with STS, then stores the credential, region, and account metadata atomically. Validation runs even while Bedrock is disabled; a rejected candidate returns `400`, is not retained, and leaves any previous validated connection unchanged. AWS checks model-specific invocation permission and model access on the first real task, avoiding a paid setup invocation. Later AWS failures are reported by the task that encounters them; they do not create a stored credential-health state. The request accepts exactly these three fields; the secret is never returned. |
| `DELETE` | `/v1/agent-runtime/bedrock-credentials` | none | status response | Disconnects the AWS account, clears its credential, region, and account metadata, then fails running Hermes tasks. The live usage counters are retained: they record work already done. |
| `POST` | `/v1/agent-runtime/reset-linked-account` | `{"agent_runtime": "codex"\|"claude_code"}` | status response | Clears the selected OAuth runtime's linked account state. Bedrock uses the credential endpoint above because it uses an IAM credential instead of OAuth. |

The runtime-specific OAuth login endpoints work while that runtime's status is
`awaiting_login` or `error` — an errored runtime (changed account, malformed
local credentials) is recovered by simply logging in again. They return `409`
in any other state, including `deactivated`.
`POST /v1/agent-runtime/reset-linked-account` takes `{"agent_runtime": "codex"}`
or `{"agent_runtime": "claude_code"}` and deletes that runtime's linked-account
guard: the operator-approved anchor, its proxy pin, and any pending OAuth
approval. Use it to unlink the account, for example to switch a runtime to a
different provider account. It may be called in any
runtime status. It also moves the runtime out of `active`, clears local agent
auth files, closes that runtime's live processes, and fails its running tasks
so no process from the old linked account keeps executing. The runtime is then
ready for a fresh operator login that links an account again.
`GET /v1/agent-runtime/account` does not accept query parameters; it always returns
one account-status entry per runtime.
`POST /v1/agent-runtime/refresh` accepts `{}` to refresh all runtimes, or
`{"agent_runtime": "codex"}`, `{"agent_runtime": "claude_code"}`, or `{"agent_runtime": "hermes"}` to refresh one.
It forces a provider check instead of reusing a remembered live-validation
verdict. It returns the same response shape as
`GET /v1/agent-runtime/account`.

Agent runtime status response:

```json
{
  "runtimes": [
    {
      "type": "codex",
      "status": "active",
      "active_task_ids": []
    },
    {
      "type": "claude_code",
      "status": "deactivated",
      "active_task_ids": []
    }
  ]
}
```

Agent runtime status response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `runtimes[].type` | enum | `codex`, `claude_code`, `hermes` | Agent runtime type. |
| `runtimes[].status` | enum | `deactivated`, `loading`, `awaiting_login`, `active`, `error` | Current runtime state. Codex uses its rate-limit request and, if that fails, one Codex-owned forced refresh. Claude Code uses a `/usage` probe for the pinned token, or provider profile attestation for a new or rotated token. Bedrock is `active` when the integration is enabled and its synchronously validated credential/account row is present. AWS checks model-specific invocation permission and current credential validity on the first real task; later provider failures are task failures. |
| `runtimes[].active_task_ids` | string array |  | Currently running task ids for that runtime, in task order. Empty when no task is running. |
| `runtimes[].error_message` | string | optional | Present only while `status` is `error`: the underlying runtime failure message. |

Agent account response:

```json
{
  "accounts": [
    {
      "agent_runtime": "codex",
      "provider": "openai",
      "status": "active",
      "account_id": "acct_...",
      "email": "operator@example.com",
      "plan_type": "pro",
      "codex_usage": {
        "last_checked_at": "2026-06-29T23:10:00Z",
        "rate_limits": {
          "primary": {
            "used_percent": 60,
            "window_duration_mins": 300,
            "resets_at": 1782788896
          },
          "secondary": {
            "used_percent": 20,
            "window_duration_mins": 10080,
            "resets_at": 1783296254
          },
          "credits": {
            "has_credits": false,
            "unlimited": false,
            "balance": "0"
          }
        }
      }
    },
    {
      "agent_runtime": "claude_code",
      "provider": "claude",
      "status": "active",
      "account_id": "uuid...",
      "email": "operator@example.com",
      "plan_type": "pro",
      "claude_usage": {
        "current_session_used_percent": 0,
        "current_session_resets_at": 1782781800,
        "weekly_used_percent": 0,
        "weekly_resets_at": 1783094340,
        "fable_weekly_used_percent": 0,
        "fable_weekly_resets_at": 1783094340,
        "last_checked_at": "2026-06-29T23:10:00Z"
      }
    },
    {
      "provider": "bedrock",
      "agent_runtimes": ["hermes"],
      "status": "active",
      "account_id": "123456789012",
      "arn": "arn:aws:iam::123456789012:user/trustyclaw-bedrock",
      "bedrock_usage": {
        "month_to_date": 0.3102,
        "currency": "USD",
        "requests": 41,
        "metered_requests": 41,
        "input_tokens": 402118,
        "output_tokens": 31889,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0
      }
    }
  ]
}
```

Agent account response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `accounts[].agent_runtime` | enum | `codex`, `claude_code` | Runtime for an OAuth provider record. Absent on the Bedrock record. |
| `accounts[].agent_runtimes` | string array | `["hermes"]` | Runtime that uses the Bedrock provider. Present only on the Bedrock record. |
| `accounts[].provider` | enum | `openai`, `claude`, `bedrock` | Managed AI provider. |
| `accounts[].status` | enum | `deactivated`, `loading`, `awaiting_login`, `active`, `error` | Current provider account status. OAuth runtimes use `awaiting_login` when operator login is required. Bedrock has no OAuth flow: its status is `awaiting_login` until a synchronously validated credential is connected, then `active`. Later inference failures are reported on their tasks, not persisted as provider status. |
| `accounts[].account_id` | string | optional | The linked provider account id; for `bedrock` this is the 12-digit AWS account id. Present whenever a validated account identity is available. |
| `accounts[].email` | string | optional | Present when available from the linked account metadata. |
| `accounts[].arn` | string | optional | The STS-attested IAM identity of the connected AWS credential. Present only on the Bedrock provider record while its account is linked. |
| `accounts[].plan_type` | string | optional | Common plan name for the provider account. Present only while the runtime is active. |
| `accounts[].codex_usage` | object | optional | Codex-specific usage metadata. Present only for the Codex runtime when Codex reports rate limits. |
| `accounts[].codex_usage.last_checked_at` | string | optional | UTC timestamp when TrustyClaw last refreshed the cached Codex usage snapshot. Active runtimes are rechecked every 300 seconds. |
| `accounts[].codex_usage.rate_limits` | object | optional | Codex rate-limit snapshot. |
| `accounts[].codex_usage.rate_limits.primary` | object | optional | Codex 300-minute rate-limit window. |
| `accounts[].codex_usage.rate_limits.secondary` | object | optional | Codex 10080-minute rate-limit window. |
| `accounts[].codex_usage.rate_limits.primary.used_percent`, `accounts[].codex_usage.rate_limits.secondary.used_percent` | number | optional | Percent used for this window. |
| `accounts[].codex_usage.rate_limits.primary.window_duration_mins`, `accounts[].codex_usage.rate_limits.secondary.window_duration_mins` | number | optional | Window duration in minutes. |
| `accounts[].codex_usage.rate_limits.primary.resets_at`, `accounts[].codex_usage.rate_limits.secondary.resets_at` | number | optional | Unix timestamp when this window resets. |
| `accounts[].codex_usage.rate_limits.credits` | object | optional | Codex credit snapshot. |
| `accounts[].codex_usage.rate_limits.credits.has_credits` | boolean | optional | Whether the account has credits. |
| `accounts[].codex_usage.rate_limits.credits.unlimited` | boolean | optional | Codex `unlimited`. |
| `accounts[].codex_usage.rate_limits.credits.balance` | string | optional | Codex credit balance. |
| `accounts[].claude_usage` | object | optional | Claude Code usage metadata parsed from `claude -p "/usage" --output-format json`. Windows parse independently, so any subset of the fields below can be present. |
| `accounts[].claude_usage.current_session_used_percent` | number | optional | Percent used for the current Claude Code session. |
| `accounts[].claude_usage.current_session_resets_at` | number | optional | Unix timestamp when the current Claude Code session window resets. |
| `accounts[].claude_usage.weekly_used_percent` | number | optional | Percent used for the current Claude Code weekly window across all models. |
| `accounts[].claude_usage.weekly_resets_at` | number | optional | Unix timestamp when the Claude Code weekly window resets. |
| `accounts[].claude_usage.fable_weekly_used_percent` | number | optional | Percent used for the Fable-specific weekly window. |
| `accounts[].claude_usage.fable_weekly_resets_at` | number | optional | Unix timestamp when the Fable-specific weekly window resets. |
| `accounts[].claude_usage.last_checked_at` | string | optional | UTC timestamp of the provider read that produced this Claude usage snapshot. Active runtimes are rechecked every 300 seconds; the explicit refresh endpoint forces an immediate provider read. If no usage window parses, `claude_usage` is absent rather than stale. |
| `accounts[].bedrock_usage` | object | always on the `bedrock` record | Live Hermes month-to-date usage. For each allowed Bedrock response the network proxy records the token usage AWS reports and the USD it prices that response at, per model and UTC day; this sums the current month from those stored counters, so every accounts read is current with no AWS call. Usage survives credential resets: the counters record work already done. |
| `accounts[].bedrock_usage.month_to_date` | number |  | Current-month cost: the sum of the USD the proxy priced each metered response at when it recorded it, using the host's on-demand catalog rates. Final once recorded; a later rate edit does not rewrite it. An estimate of what AWS will bill, not the bill itself. |
| `accounts[].bedrock_usage.currency` | string |  | Always `USD` (the catalog rates' currency). |
| `accounts[].bedrock_usage.requests` | number |  | Allowed Bedrock invocations forwarded this month. |
| `accounts[].bedrock_usage.metered_requests` | number |  | Invocations whose response carried a parseable usage record. A gap below `requests` means AWS errors or unparsed responses, that is, possible undercounting. A model outside the price table is still metered; its tokens count but it adds nothing to `month_to_date`. |
| `accounts[].bedrock_usage.input_tokens`, `.output_tokens`, `.cache_read_tokens`, `.cache_write_tokens` | number |  | Month-to-date token totals as AWS reported them per response. The cached-token counters mirror the Converse usage shape but stay zero: Bedrock prompt caching covers only model families outside this catalog. |

Codex OAuth login response:

```json
{
  "status": "awaiting_login",
  "device_code": "ABCD-EFGH",
  "login_url": "https://auth.openai.com/activate",
  "expires_at": "2026-06-08T00:10:00Z"
}
```

Codex OAuth login response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `status` | enum | `awaiting_login` | Current Codex OAuth login state. |
| `device_code` | string |  | Code the operator enters on the Codex OAuth login page. |
| `login_url` | string |  | Operator URL for Codex OAuth login. |
| `expires_at` | string | RFC 3339 timestamp | Time when the device code expires. |

Claude OAuth login response:

```json
{
  "status": "awaiting_code",
  "login_url": "https://claude.com/cai/oauth/authorize?...",
  "expires_at": "2026-06-08T00:10:00Z"
}
```

After opening the URL and completing browser login, submit the displayed code to
`POST /v1/agent-runtime/claude-oauth-login/complete`:

```json
{
  "code": "..."
}
```

Agent runtime reset-linked-account response:

```json
{
  "status": "accepted"
}
```

Agent runtime reset-linked-account response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `status` | enum | `accepted` | The linked account was reset. |

### Tasks

```text
POST /v1/tasks
GET  /v1/tasks?last_seen_task_id=<task_id>
GET  /v1/tasks/{task_id}
PUT  /v1/tasks/{task_id}
POST /v1/tasks/{task_id}/steer
POST /v1/tasks/{task_id}/cancel
POST /v1/tasks/{task_id}/kill
GET  /v1/threads
GET  /v1/threads/{thread_id}/tasks
GET  /v1/threads/{thread_id}/events
```

Every task belongs to a client-chosen thread (`thread_id`). Tasks on the same
thread share one runtime conversation and run one at a time in creation order;
tasks on different threads run in parallel, up to 9 total and up to 3 per
runtime. Codex resumes the thread's provider conversation by id on a fresh
app-server; Claude Code and Hermes resume by their recorded provider
session ids. To start a fresh conversation with no prior
context, use a new `thread_id`. A `thread_id` belongs to the first runtime that
uses it; creating a task for the same `thread_id` with another runtime returns
`409`. `agent_runtime` chooses which runtime should execute the task. A task
runs only while its chosen runtime is `active`: a task claimed while the
runtime is `deactivated`, `loading`, `awaiting_login`, or `error` fails
immediately with that status in `error_message`. If a runtime leaves `active`
while tasks are running because its provider is disabled, its login expires,
or its health check fails, the host closes that runtime's live processes and
marks those running tasks `failed`.

Task endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `POST` | `/v1/tasks` | Create task request | Task response | Creates a task for the agent runtime. Returns `409` when 1,000 tasks are already queued. |
| `GET` | `/v1/tasks?last_seen_task_id=<task_id>` | `last_seen_task_id` query parameter is optional | Task list response | Lists up to 5 current and pending tasks with their status, in execution order. |
| `GET` | `/v1/tasks/{task_id}` | none | Task response | Returns one task. |
| `PUT` | `/v1/tasks/{task_id}` | Update task request | Task response | Updates one pending task. Only tasks with status `queued` can be updated. |
| `POST` | `/v1/tasks/{task_id}/steer` | Steer task request | Steer task response | Sends additional steering to one running Codex or Claude Code task. Hermes does not support steering; create a new task on the same `thread_id` instead. |
| `POST` | `/v1/tasks/{task_id}/cancel` | none | Task cancel response | Requests cancellation for one pending task. Only tasks with status `queued` can be cancelled. |
| `POST` | `/v1/tasks/{task_id}/kill` | none | Task kill response | Kills one running task: its runtime process is terminated and the task becomes `cancelled`. Only tasks with status `running` can be killed; returns `409` otherwise. The thread itself survives — a later task on the same `thread_id` resumes the conversation. |
| `GET` | `/v1/threads` | none | Thread list response | Lists recent runtime threads, including active queued/running work and retained runtime session mappings. |
| `GET` | `/v1/threads/{thread_id}/tasks` | Optional `limit` and `message_bytes` query parameters | Task list response | Lists retained tasks for one thread, newest first by `updated_at` with task id as a tiebreaker. `limit` defaults to 1,000 and is capped there. When supplied, `message_bytes` truncates each input, output, and error string to that many encoded bytes, capped at 200,000, before the response crosses a proxy boundary. |
| `GET` | `/v1/threads/{thread_id}/events?since=<seq>&limit=<n>` | `since` and `limit` query parameters are optional | Event list response | Streams one thread's task events across all of its tasks, oldest first, with `seq > since`. `limit` defaults to 100 and is capped there. |

Create task request:

```json
{
  "agent_runtime": "codex",
  "model": "gpt-5.6-terra",
  "effort": "high",
  "input_message": "Implement this change and report the result.",
  "thread_id": "feature-chat-1"
}
```

Create task request fields:

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `agent_runtime` | New thread | enum | Runtime to execute the task: `codex`, `claude_code`, or `hermes`. Supply it together with `model` and `effort`. An existing thread accepts all three when they exactly match its fixed configuration, or none of them. |
| `model` | New thread | enum | Model for this session. Codex accepts `gpt-5.6-terra`, `gpt-5.6-sol`, or `gpt-5.6-luna`; Claude Code accepts `opus`, `fable`, or `sonnet`; Hermes accepts the Bedrock model ids `deepseek.v3.2`, `qwen.qwen3-coder-next`, or `moonshotai.kimi-k2.5`. Must be supplied together with `agent_runtime` and `effort`. |
| `effort` | New thread | enum | Effort for this session. Codex accepts `high`, `max`, or `ultra`, except Luna accepts only `high` or `max`. Claude Code accepts `high`, `max`, or `ultracode`; `ultracode` enables its xhigh effort plus dynamic workflow orchestration. Hermes accepts `high` (its headless CLI exposes no effort control). Must be supplied together with `agent_runtime` and `model`. |
| `input_message` | Yes | string | Task message for the agent runtime. Must be 1 to 50,000 characters. |
| `thread_id` | Yes | string | Client-generated conversation id this task belongs to. Must be 1 to 64 characters of `A-Z`, `a-z`, `0-9`, `-`, or `_`. The first task requires and fixes the runtime, model, and effort on the thread. Later tasks may omit all three or repeat the complete matching triple; a partial or conflicting configuration returns `400`. Omitting them for an unknown thread also returns `400`. Thread rows referenced by retained tasks are preserved; otherwise the host retains the 100,000 most recently used mappings per runtime. Once a thread is no longer retained, supplying a configuration starts a fresh provider conversation. |

Follow-up task request:

```json
{
  "input_message": "Continue with the implementation.",
  "thread_id": "feature-chat-1"
}
```

Task response:

```json
{
  "task_id": "task_123",
  "status": "completed",
  "agent_runtime": "codex",
  "model": "gpt-5.6-terra",
  "effort": "high",
  "thread_id": "feature-chat-1",
  "input_message": "Implement this change and report the result.",
  "output_message": "Implemented the change and pushed the PR update.",
  "created_at": "2026-06-08T00:00:00Z",
  "updated_at": "2026-06-08T00:00:00Z"
}
```

Task response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `task_id` | string |  | Host-generated task id. |
| `status` | enum | `queued`, `running`, `completed`, `failed`, `cancelled` | Current task status. |
| `agent_runtime` | enum | `codex`, `claude_code`, `hermes` | Runtime assigned to this task. |
| `model` | enum | See create request | Model assigned to this task and its session. |
| `effort` | enum | See create request | Effort assigned to this task and its session. |
| `thread_id` | string |  | Conversation thread this task belongs to. |
| `input_message` | string |  | Task message for the agent runtime. |
| `output_message` | string |  | Final output message from the agent runtime. Present only when `status` is `completed`. |
| `error_message` | string |  | Human-readable failure message. Present only when `status` is `failed`. |
| `created_at` | string | RFC 3339 timestamp | Task creation time. |
| `updated_at` | string | RFC 3339 timestamp | Last task update time. |

Task list response:

Task list query parameters:

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `last_seen_task_id` | No | string | Last task id from the previous page. When present, the response starts after this task in execution order. |

```json
{
  "tasks": [
    {
      "task_id": "task_123",
      "status": "running",
      "queue_position": 0,
      "agent_runtime": "codex",
      "model": "gpt-5.6-terra",
      "effort": "high",
      "thread_id": "feature-chat-1",
      "input_message": "Implement this change and report the result.",
      "created_at": "2026-06-08T00:00:00Z",
      "updated_at": "2026-06-08T00:00:00Z"
    },
    {
      "task_id": "task_124",
      "status": "queued",
      "queue_position": 1,
      "agent_runtime": "claude_code",
      "model": "fable",
      "effort": "ultracode",
      "thread_id": "docs-chat",
      "input_message": "Add the follow-up documentation update.",
      "created_at": "2026-06-08T00:01:00Z",
      "updated_at": "2026-06-08T00:01:00Z"
    }
  ]
}
```

Task list response fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `tasks` | Task response array | Up to 5 tasks. The first page starts with the running tasks followed by pending tasks in creation order. Later pages continue after `last_seen_task_id`. Completed, failed, and cancelled tasks are not included. |
| `tasks[].queue_position` | integer | Queue position for this task. `0` marks every currently running task (up to 9 total and up to 3 per runtime run in parallel). Pending tasks use `1`, `2`, `3`, and so on in creation order. If no task is running, pending tasks still start at `1`. A pending task can run ahead of an earlier one when the earlier task waits on a busy thread or when an earlier task's runtime is already at its per-runtime cap. |

Thread list response:

```json
{
  "threads": [
    {
      "thread_id": "feature-chat-1",
      "agent_runtime": "codex",
      "model": "gpt-5.6-terra",
      "effort": "high",
      "last_used_at": "2026-06-08T00:05:00Z",
      "active_tasks": [{"task_id": "task_125", "status": "running"}],
      "task_count": 4
    }
  ]
}
```

Thread list response fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `threads` | thread array | Recent known threads sorted by `last_used_at` descending. |
| `threads[].thread_id` | string | Client-generated conversation id. |
| `threads[].agent_runtime` | enum | Runtime for this thread entry: `codex`, `claude_code`, or `hermes`. |
| `threads[].model` | enum | Model fixed for this session. |
| `threads[].effort` | enum | Effort fixed for this session. |
| `threads[].last_used_at` | string | Latest retained task update or runtime session use timestamp known for this thread/runtime. |
| `threads[].active_tasks` | array | Queued or running retained tasks on this thread/runtime. Empty when no task is currently active. |
| `threads[].task_count` | integer | Number of retained task records for this thread/runtime. Older finished tasks can be pruned. |

Thread task list response fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `tasks` | Task response array | Up to 1,000 retained tasks for the selected thread, newest first by `updated_at` with task id as a tiebreaker. The host keeps active tasks and the 100,000 most recently updated finished tasks globally before pruning older task records. |

Update task request:

```json
{
  "input_message": "Use this updated task message."
}
```

Update task request fields:

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `input_message` | Yes | string | Replacement task message for the agent runtime. Must be 1 to 50,000 characters. |

`PUT /v1/tasks/{task_id}` returns `409` when the task is not pending.

Steer task request:

```json
{
  "steer_message": "Focus only on the API documentation change."
}
```

Steer task request fields:

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `steer_message` | Yes | string | Additional steering message for a running Codex or Claude Code task. Must be 1 to 50,000 characters. A task holds at most 20 undelivered steer messages at a time; delivered steers leave the queue (their content is preserved as `task.message` events). Hermes rejects steering because its headless process has no mid-turn input channel. |

Steer task response:

```json
{
  "status": "accepted"
}
```

Steer task response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `status` | enum | `accepted` | Steering message was accepted and will be applied asynchronously. |

`POST /v1/tasks/{task_id}/steer` returns `409` when the task is not running,
when the task uses Hermes, or when the task already holds 20 undelivered steer
messages. A later Hermes instruction is a new task on the same `thread_id`,
which resumes the stored provider conversation.

`accepted` means the message was recorded, not that the agent acted on it: a
steer that lands in the instant between the turn's final steering check and
the task completing is recorded but never delivered. If the task finishes
right after a steer, check the task output and start a follow-up task if the
steering still matters.

Task cancel response:

```json
{
  "status": "accepted"
}
```

Task cancel response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `status` | enum | `accepted` | Cancellation request was accepted and will be applied asynchronously. |

Task kill response:

```json
{
  "status": "accepted"
}
```

Task kill response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `status` | enum | `accepted` | The task was cancelled and its runtime process is being terminated. |

Task statuses:

```text
queued
running
completed
failed
cancelled
```

### Events

```text
GET /v1/tasks/{task_id}/events?since=<seq>
GET /v1/events?before=<seq>&limit=<n>
```

Event endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/tasks/{task_id}/events?since=<seq>` | `since` query parameter is optional | Event list response | Streams up to 5 events for one task, oldest first. |
| `GET` | `/v1/events?before=<seq>&limit=<n>` | query parameters optional | Event list response | Lists newest agent events before an optional sequence cursor. |

The two endpoints serve different access patterns. Task events tail one task
while it runs: with `since` present, the response holds events with
`seq > since`, oldest first; use the highest returned `seq` as the next
`since`, and keep the same `since` when a response is empty.

The audit log across all tasks pages newest-first like the network audit log:
the first request returns the newest events, and `before=<seq>` continues with
events whose `seq` is lower than that cursor. `limit=<n>` is optional,
defaults to 100, and must be between 1 and 100.

The host retains only the most recent 1,000,000 agent events; older events are
discarded and can no longer be listed.

Event list response:

```json
{
  "events": [
    {
      "event_id": "event_123",
      "seq": 42,
      "timestamp": "2026-06-08T00:00:00Z",
      "event_type": "task.message",
      "task_id": "task_123",
      "payload": {
        "message": "Task update from the agent.",
        "source": "agent"
      }
    }
  ]
}
```

Event list response fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `events` | event array | Events ordered by `seq`: oldest first for task events, newest first for `/v1/events`. Empty when no matching events are available. |

Event fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `event_id` | string |  | Stable event id. |
| `seq` | integer |  | Monotonic host-local event sequence number. |
| `timestamp` | string | RFC 3339 timestamp | Event time. |
| `event_type` | enum | See event types below. | Event type. |
| `task_id` | string or null |  | Related task id for task events, or `null` for agent runtime events. |
| `payload` | object |  | Event-specific JSON payload. |

Event types:

```text
task.started
task.message
task.completed
task.failed
task.cancelled
agent_runtime.active
agent_runtime.login_completed
agent_runtime.linked_account_reset
agent_runtime.deactivated
```

`task.started` uses the top-level `task_id` field and an empty payload `{}`.

`task.message` payload fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `message` | string |  | Message text from the task, including first input, intermediate, and result messages. |
| `source` | enum | `agent`, `user` | Message source. |

`task.completed` uses the top-level `task_id` field and an empty payload `{}`.

`task.failed` payload fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `error_message` | string |  | Human-readable task failure message. |

`task.cancelled` uses the top-level `task_id` field and an empty payload `{}`.
It is emitted when a queued task is cancelled or a running task is killed.

`agent_runtime.active` uses `task_id: null` and payload
`{"agent_runtime": "codex"}`, `{"agent_runtime": "claude_code"}`, or `{"agent_runtime": "hermes"}`.

`agent_runtime.login_completed` uses `task_id: null` and payload
`{"agent_runtime": "codex"}` or `{"agent_runtime": "claude_code"}`. Hermes has
no login flow.

`agent_runtime.linked_account_reset` uses `task_id: null` and payload
`{"agent_runtime": "codex"}`, `{"agent_runtime": "claude_code"}`, or `{"agent_runtime": "hermes"}` when an
operator reset cleared that runtime's linked account (the audit record of the
reset-linked-account endpoint).

`agent_runtime.deactivated` uses `task_id: null` and payload
`{"agent_runtime": "codex"}`, `{"agent_runtime": "claude_code"}`, or `{"agent_runtime": "hermes"}` when a
runtime is disabled because its managed provider integration is disabled.

## Agent Files

```text
GET /v1/agent-files?path=<path>
GET /v1/agent-files/read?path=<path>
POST /v1/agent-files/upload?filename=<name>
```

Agent file endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agent-files?path=<path>` | `path` query parameter is optional; default `/` | Agent file list response | Lists one directory under the agent home, including hidden entries. |
| `GET` | `/v1/agent-files/read?path=<path>` | `path` query parameter is optional; default `/` | Agent file read response | Reads one regular file under the agent home as a UTF-8 text preview. |
| `POST` | `/v1/agent-files/upload?filename=<name>` | Raw file bytes; `Content-Length` is required | `{"file": {...}}` | Uploads one file into the agent home's `user-files/` directory. The body is capped at 25 MiB. |

The API treats `/` as `/mnt/trustyclaw-agent/agent-home`. Paths that resolve
outside that home are rejected. Symlinks are not supported: directory listings
omit symlink entries, and direct requests for symlink paths return a validation
error.

Directory listings inspect and return at most 1,000 entries. Returned entries
are sorted. If the scan hits the cap, `truncated` is `true`.

Agent file list response:

```json
{
  "path": "/workspace",
  "truncated": false,
  "entries": [
    {
      "name": ".env",
      "path": "/workspace/.env",
      "type": "file",
      "size_bytes": 123,
      "modified_at": "2026-06-08T00:00:00Z"
    }
  ]
}
```

Agent file read response:

```json
{
  "path": "/workspace/README.md",
  "size_bytes": 123,
  "truncated": false,
  "encoding": "utf-8-replacement",
  "content": "File contents..."
}
```

File reads are capped at 1 MiB. If the file is larger, `truncated` is `true`
and `content` contains the first 1 MiB decoded with replacement characters for
invalid UTF-8 bytes.

Upload `filename` is the original basename, not a path. It must be non-empty,
at most 200 UTF-8 bytes, and contain no slash, backslash, NUL, or control
character. The host publishes a completed upload atomically under
`user-files/` and prefixes its stored name with a sortable UTC timestamp. It
never overwrites an existing file. Incomplete uploads are removed.

```json
{
  "file": {
    "path": "user-files/20260722T120000.123456Z_reference.png",
    "name": "20260722T120000.123456Z_reference.png",
    "original_name": "reference.png",
    "size_bytes": 12345,
    "uploaded_at": "2026-07-22T12:00:00.123456Z"
  }
}
```

`path` is relative to `/mnt/trustyclaw-agent/agent-home`, which is also the
agent runtime's working directory. Uploads are durable workspace data and are
not pruned automatically.

## Agent Processes

```text
GET /v1/agent-processes
```

Returns a read-only diagnostic snapshot of Codex, Claude Code, and processes
spawned by those runtimes. This is process state, not task state: short-lived turn
processes may exit before the next snapshot. The response contains at most 1,000 processes; when
more matching processes exist, `truncated` is `true`.

Response:

```json
{
  "truncated": false,
  "processes": [
    {
      "pid": 1234,
      "state": "S",
      "name": "codex",
      "cmdline": "codex app-server --listen stdio://",
      "rss_bytes": 92274688,
      "elapsed_seconds": 184
    }
  ]
}
```

## Network

```text
GET    /v1/network/policy
PUT    /v1/network/policy
GET    /v1/network-tools/github-credential
PUT    /v1/network-tools/github-credential
DELETE /v1/network-tools/github-credential
POST   /v1/network-tools/github-audit
GET    /v1/network-tools/github-pending-pushes
POST   /v1/network-tools/github-pending-pushes/<id>/approve
POST   /v1/network-tools/github-pending-pushes/<id>/reject
GET    /v1/network/events?before=<seq>&decision=<allowed|denied|all>&limit=<n>
```

Network endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/network/policy` | none | Network policy response | Returns active network policy. |
| `PUT` | `/v1/network/policy` | Network policy request | Network policy response | Replaces network policy atomically. Disabling a managed provider integration deactivates its runtime, clears its account pin, closes its live runtime processes, and fails its running tasks. |
| `GET` | `/v1/network-tools/github-credential` | none | GitHub credential metadata | Returns credential metadata only; never the token. |
| `PUT` | `/v1/network-tools/github-credential` | GitHub credential request | GitHub credential metadata | Stores or replaces the single fixed GitHub token. The `token` field is write-only. |
| `DELETE` | `/v1/network-tools/github-credential` | none | GitHub credential metadata | Removes the stored credential and withdraws the proxy-injected working token. |
| `POST` | `/v1/network-tools/github-audit` | none | GitHub credential metadata | Force-refreshes the per-repository audits and returns the updated metadata (including `repository_audits`). |
| `GET` | `/v1/network-tools/github-pending-pushes` | none | `{pending_pushes: [...]}` | Lists pushes held by the `.github` approval gate: `id`, `owner`, `repo`, `ref_updates`, `changed_paths`, `requested_at`, `status`. |
| `POST` | `/v1/network-tools/github-pending-pushes/<id>/approve` | none | `{pending_push: {...}}` | Replays the held push to GitHub with the working token through the `approve-github-push` root helper and marks it approved. `404` if unknown, `409` if already resolved, another resolution is in progress, no working token is available (the row stays pending), or the replay fails. Replay failures mark the row `failed` after one best-effort cleanup. |
| `POST` | `/v1/network-tools/github-pending-pushes/<id>/reject` | none | `{pending_push: {...}}` | Cleans up pending quarantine refs (best-effort) and marks the held push rejected. `404`/`409` as above. |
| `GET` | `/v1/network/events?before=<seq>&decision=<allowed\|denied\|all>&limit=<n>` | query parameters optional | Network event response | Lists newest network decision events before an optional sequence cursor. |

The `github-credential` routes work whether or not
`network_integrations.github` is enabled, so the credential can be
staged before the integration is turned on; the proxy-injected working token
is only ever published while GitHub is enabled.

Network policy request:

```json
{
  "network_integrations": {
    "openai": {"enabled": true},
    "github": {
      "enabled": true,
      "write_repositories": [
        {"owner": "infiloop2", "repo": "trustyclaw"}
      ]
    },
    "custom": {
      "domains": {
        "example.com": {
          "allow_http_methods": ["GET", "HEAD"],
          "path_guards": ["^/$", "^/docs(?:/.*)?$"]
        }
      }
    }
  }
}
```

The request body is the replacement runtime network controls object using the
schema from [`NetworkControls.md`](NetworkControls.md).

When `PUT /v1/network/policy` is accepted, the replacement policy has been
validated and atomically written. Concurrent replacements are last-writer-wins:
the stored policy is always exactly one submitted body, never a blend.

Network policy response:

The API response uses the operator-facing network controls shape. Managed
integration domains are not listed under the custom integration; the proxy maps
each public field directly to its typed integration config, and credential
secrets are never included.

```json
{
  "network_controls": {
    "network_integrations": {
      "openai": {"enabled": true},
      "github": {
        "enabled": true,
        "write_repositories": [
          {"owner": "infiloop2", "repo": "trustyclaw"}
        ]
      },
      "custom": {
        "domains": {
          "example.com": {
            "allow_http_methods": ["GET", "HEAD"],
            "path_guards": ["^/$", "^/docs(?:/.*)?$"]
          }
        }
      }
    }
  },
  "updated_at": "2026-06-08T00:00:00Z"
}
```

Network policy response fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `network_controls` | object | Runtime network controls using the schema from [`NetworkControls.md`](NetworkControls.md). |
| `updated_at` | string | RFC 3339 timestamp for the last policy update. Present in responses only. |

GitHub credential request — fine-grained PAT mode:

```json
{
  "mode": "pat",
  "token": "github_pat_..."
}
```

GitHub credential request — GitHub App mode (the host mints
installation-wide tokens and refreshes them before their one-hour expiry;
the proxy's repo guard, not the token, is the per-repository boundary — see
[NetworkControls.md](NetworkControls.md#github-integration)):

```json
{
  "mode": "app",
  "app_id": "12345",
  "installation_id": "67890",
  "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\n..."
}
```

GitHub credential request fields (fields outside the chosen mode are
rejected):

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `mode` | string | Yes | `pat` or `app`. |
| `token` | string | `pat` mode only | The fine-grained personal access token. |
| `app_id` | string | `app` mode only | The numeric GitHub App id. |
| `installation_id` | string | `app` mode only | The numeric installation id. |
| `private_key_pem` | string | `app` mode only | The App's PEM private key. |

`token` and `private_key_pem` are write-only: they are encrypted at rest and
never returned by any endpoint or echoed by the UI. While GitHub is enabled
and a credential is stored, the network proxy injects the active token into
policy-approved GitHub requests — the agent never holds the credential —
and plain `git` and `gh` read any repository the token reaches and write to
the configured `write_repositories`; see
[`NetworkControls.md`](NetworkControls.md#github-credential).

GitHub credential metadata response — returned by all three
`github-credential` methods and by `POST /v1/network-tools/github-audit`:

```json
{
  "configured": true,
  "mode": "app",
  "app_id": "12345",
  "installation_id": "67890",
  "app_token_expires_at": "2026-06-08T01:00:00Z",
  "updated_at": "2026-06-08T00:00:00Z",
  "validation": {"status": "ok", "checked_at": "2026-06-08T00:00:00Z"},
  "repository_audits": [
    {
      "owner": "infiloop2",
      "repo": "trustyclaw",
      "audited_at": "2026-06-08T00:00:00Z",
      "warnings": [
        {
          "code": "unprotected_default_branch",
          "severity": "critical",
          "message": "The token can push and the default branch is unprotected: ..."
        }
      ]
    }
  ]
}
```

GitHub credential metadata response fields:

| Field | Type | Present | Meaning |
| --- | --- | --- | --- |
| `configured` | boolean | Always | Whether a credential is stored. |
| `mode` | string | When configured | `pat` or `app`. |
| `updated_at` | string | When configured | RFC 3339 time the credential was last stored. |
| `app_id` | string | `app` mode only | The stored GitHub App id. |
| `installation_id` | string | `app` mode only | The stored installation id. |
| `app_token_expires_at` | string | `app` mode, once the host has minted an installation token | Expiry of the current minted token (the host re-mints before it passes). Absent until the first successful mint. |
| `validation` | object | When configured | Credential health: `{"status": "not_checked"}` before the first check, `{"status": "ok", "checked_at": ...}` after a success, `{"status": "error", "message": ..., "checked_at": ...}` after a failure — on failure the working token is withdrawn (fail closed) and the poller retries. |
| `repository_audits` | array | When the policy lists `write_repositories` | One entry per listed write repository, in policy order. Audits warn, never gate: a failed or missing audit never blocks the credential or a policy publish. If no credential is configured, each repository reports an incomplete-audit warning. |

`repository_audits[]` entry fields:

| Field | Type | Present | Meaning |
| --- | --- | --- | --- |
| `owner` | string | Always | The write repository's owner. |
| `repo` | string | Always | The write repository's name. |
| `audited_at` | string | Once an audit attempt has been stored; absent while the first attempt is still pending | RFC 3339 time of the last audit attempt (success or failure). |
| `warnings` | array | Always | Operator warnings, each `{"code", "severity", "message"}` with `severity` `critical` or `warning` — for example a public write repository, an unprotected default branch the token can push, workflows whose triggers expose secrets to PR-influenced code, or an incomplete audit when TrustyClaw lacks enough information. Empty means a clean audit. |
| `error` | string | When the last audit attempt failed | Raw failure detail for diagnostics; the same condition also appears as a warning and the next poller pass retries it. |

Network event response:

Network event endpoints return newest-first events. Pass `before=<seq>` to
continue with events whose `seq` is lower than that cursor. `decision=allowed`
or `decision=denied` filters the listed events; `decision=all` is equivalent
to omitting the filter. `limit=<n>` is optional, defaults to 100, and must be
between 1 and 100.

Network events are only defined for HTTP, HTTPS, WebSocket, and secure WebSocket
requests. SSH and other non-HTTP traffic are not represented by this endpoint.

The host retains only the most recent 1,000,000 network events; older events are
discarded and can no longer be listed.

```json
{
  "events": [
    {
      "seq": 42,
      "timestamp": "2026-06-08T00:00:00Z",
      "protocol": "https",
      "method": "GET",
      "host": "api.github.com",
      "port": 443,
      "path": "/repos/infiversehq/trustyclaw-host",
      "query": "per_page=5",
      "decision": "allowed"
    }
  ]
}
```

A denied event additionally carries a stable snake_case `reason_code` (e.g.
`host_not_allowed`, `openai_web_tool_denied`) identifying the denial class;
allowed events omit it. The same code is the proxy's 403 response body, and
the agent-facing `recent_network_denials` tool maps it to guidance.

Network event response fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `events` | network event array | Up to `limit` newest-first events. |

Network event fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `seq` | integer |  | Monotonic host-local network event sequence number. |
| `timestamp` | string | RFC 3339 timestamp | Decision time. |
| `protocol` | enum | `http`, `https`, `ws`, `wss` | Request protocol. |
| `method` | enum | `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, `CONNECT` | HTTP method. For WebSocket requests, this is the handshake method. `CONNECT` appears only on denied HTTPS or secure WebSocket tunnels that were refused before an inner request was read; allowed tunnels are logged with the method of the inner request. |
| `host` | string |  | Requested host. |
| `port` | integer |  | Requested TCP port. |
| `path` | string |  | Request path without the query string. |
| `query` | string |  | Request query string without the leading `?`, or an empty string when no query was present. |
| `decision` | enum | `allowed`, `denied` | Network decision. |
| `reason_code` | string | optional | Present only on denied events: the stable snake_case code for the denial class. The agent-facing `recent_network_denials` tool joins it against per-integration guidance. |

## Apps

```text
GET                 /v1/apps
GET                 /v1/apps/{app_id}/ui/{asset_path}
GET|POST|PUT|DELETE /v1/apps/{app_id}/api/{backend_path}
```

`GET /v1/apps` lists the app packages installed with this host release and the
host-derived resources assigned to each one:

```json
{
  "apps": [
    {
      "id": "agent_chat",
      "title": "Agent Chat",
      "release_stage": "stable",
      "backend": {
        "api_route": "/v1/apps/agent_chat/api/"
      },
      "ui": {
        "iframe_src": "/v1/apps/agent_chat/ui/index.html",
        "sandbox": ["allow-scripts", "allow-forms", "allow-modals"]
      }
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `apps[].id`, `title` | Stable manifest id and operator-facing title. |
| `apps[].release_stage` | Required manifest stage: `stable` or `beta`. The admin shell places stable non-hero apps in the always-visible Apps section and beta apps in a collapsed Apps (Beta) group; this field grants no additional authority. |
| `apps[].backend.api_route` | Authenticated admin API prefix that reverse-proxies to this app backend. |
| `apps[].ui.iframe_src` | Static entry point mounted by the admin API. |
| `apps[].ui.sandbox` | iframe permissions the admin shell applies. `allow-same-origin` is deliberately absent, so the app frame has an opaque origin. |

App UI assets under `/v1/apps/{app_id}/ui/` are static and do not require the
admin bearer. They carry a restrictive CSP and no-store cache headers, expose
no state by themselves, and cannot make browser network connections directly.
The admin shell loads the entry point in a sandboxed iframe.

App backend routes require the normal admin bearer. The admin API forwards the
JSON request and query string to the app's host-assigned loopback port with an
`X-TrustyClaw-App-Proxy` marker, strips the operator bearer, and accepts a JSON
response of at most 1 MiB. The browser app bridge pins a request to its own `app_id`;
attempting to bridge to another app returns `403`. App backend failures are
returned through the standard error envelope. App-backend-to-host calls use a
separate peer-authenticated Unix socket and narrow task/thread allowlist,
documented in [Apps architecture](../architecture/apps/apps.md).

## Tools

```text
GET  /v1/tools
PUT  /v1/tools/{tool_id}/config
POST /v1/tools/{tool_id}/enable
POST /v1/tools/{tool_id}/disable
POST /v1/tools/{tool_id}/oauth_connect/start
POST /v1/tools/{tool_id}/oauth_connect/complete
POST /v1/tools/{tool_id}/oauth_connect/disconnect
GET  /v1/tools/{tool_id}/approvals
GET  /v1/tools/{tool_id}/approvals/{approval_id}
POST /v1/tools/{tool_id}/approvals/{approval_id}/approve
POST /v1/tools/{tool_id}/approvals/{approval_id}/deny
GET  /v1/tools/events
GET  /v1/tools/events/{seq}
```

Bundled tool packages the agent can call once the operator enables them; see
the [tool contract](../architecture/tools/tool-contract.md) and
[host integration](../architecture/tools/host-integration.md) for how calls,
state, and approvals flow through the host.

Tool endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/tools` | none | Tool list response | Lists every bundled tool with its manifest (actions with per-action data policy, config requirements), enablement, per-tool config status, and OAuth connection account. Responses never include config values, tokens, or client secrets. |
| `PUT` | `/v1/tools/{tool_id}/config` | `{"key", "value"}` | `{"tool_id", "key", "set"}` | Sets one config value declared by that tool's manifest. Config is scoped per tool (a repeated key name holds an independent value per tool) and every value is a secret: write-only, stored encrypted at rest (secretbox); an empty `value` clears the key. `400` when `key` is not declared by `{tool_id}`. |
| `POST` | `/v1/tools/{tool_id}/enable` | none | `{"tool_id", "enabled"}` | Enables the tool for agent calls. Not gated on config: a tool can be enabled with partial or no config set (per-key config status is reported by `GET /v1/tools`); an action that needs an unset key fails when the tool reads it. |
| `POST` | `/v1/tools/{tool_id}/disable` | none | `{"tool_id", "enabled"}` | Disables the tool. Stored connections and credentials are kept; use disconnect to remove them. |
| `POST` | `/v1/tools/{tool_id}/oauth_connect/start` | `{"redirect_uri"}` | `{"authorization_url", "state"}` | Starts the tool's OAuth connect flow (OAuth tools only, `409` otherwise or when disabled). The UI uses `<admin origin>/oauth/callback` as the redirect URI; register that URL with the OAuth provider. Reached over SSH-forwarded localhost it is a loopback URL such as `http://localhost:7443/oauth/callback` (providers accept loopback without HTTPS); reached over a Cloudflare Access hostname it is that HTTPS origin's `/oauth/callback`. Building the URL needs no egress and runs in the admin service; the later code exchange (`oauth_connect/complete`) runs in the dedicated tools service. |
| `POST` | `/v1/tools/{tool_id}/oauth_connect/complete` | `{"code", "state", "redirect_uri"}` | `{"account": {...}}` | Completes the OAuth flow with the provider callback values and stores tokens in the tool credential store. Returns the connected `account` (see `ConnectionAccount` below); `400` for an invalid or expired `state`. |
| `POST` | `/v1/tools/{tool_id}/oauth_connect/disconnect` | none | `{"tool_id", "connected": false}` | Revokes third-party tokens where possible and deletes the stored credential. |
| `GET` | `/v1/tools/{tool_id}/approvals` | none | Approval list response | Lists `{tool_id}`'s action approvals as a bounded working set: pending first (so open decisions surface at the top), then newest decided ones as bounded history. Approvals are addressed under their tool so the operator UI shows each tool's approvals in its own row. Payload is omitted from the list; fetch it per approval. The paginated audit trail is `/v1/tools/events`. |
| `GET` | `/v1/tools/{tool_id}/approvals/{approval_id}` | none | `{"approval"}` | The full approval record for `{approval_id}`, including its (up to 64 KiB) payload. `404` when `{approval_id}` is not an approval of `{tool_id}`. |
| `POST` | `/v1/tools/{tool_id}/approvals/{approval_id}/approve` | none | `{"approval", "result"}` | Approves a pending approval and immediately executes the recorded payload exactly once; the response carries the terminal approval record (`executed` or `failed`) and the execution result. `404` when `{approval_id}` is not an approval of `{tool_id}`; `409` when it is not pending. |
| `POST` | `/v1/tools/{tool_id}/approvals/{approval_id}/deny` | none | `{"approval"}` | Denies a pending approval; terminal. `404` when `{approval_id}` is not an approval of `{tool_id}`; `409` when it is not pending. |
| `GET` | `/v1/tools/events` | `?before=&limit=` | `{"events": [...]}` | The tool audit log, newest first: tool calls, approval decisions, connect/disconnect, enable/disable, and config set/clear events. Pages with the same `before` (an event `seq`) and `limit` cursor model as `/v1/events` and `/v1/network/events`. |
| `GET` | `/v1/tools/events/{seq}` | none | `{"event": {...}}` | Loads one tool event with its exact action `arguments`. The paginated list returns only `has_arguments`, so live refreshes do not repeatedly transfer up to 64 KiB per event. `404` when `{seq}` does not exist. |

Tool list response:

```json
{
  "tools": [
    {
      "tool_id": "example_tool",
      "display_name": "Example Tool",
      "description": "Read and act on a connected third-party account. Sensitive actions are approval-gated.",
      "connection": "oauth",
      "enabled": true,
      "actions": [
        {
          "id": "search_items",
          "description": "Search items.",
          "data_policy": "Read-only. Sends the query to Example and returns item ids and metadata. Runs directly with no approval.",
          "approval": "direct",
          "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
          "output_schema": {"type": "object", "required": ["status"], "properties": {"status": {"type": "string"}}}
        },
        {
          "id": "send_item",
          "description": "Queue approval to send an item.",
          "data_policy": "Sends an item through the connected account. Queued for approval before any third-party state changes.",
          "approval": "operator",
          "input_schema": {"type": "object", "properties": {"item_id": {"type": "string"}}, "required": ["item_id"]},
          "output_schema": {}
        }
      ],
      "config": [
        {"key": "EXAMPLE_CLIENT_ID", "description": "Third-party client id for the hosting deployment.", "set": true},
        {"key": "EXAMPLE_CLIENT_SECRET", "description": "Third-party client secret for the hosting deployment.", "set": true}
      ],
      "protections": [
        "OAuth tokens stay in the host credential store and are never exposed to the agent.",
        "Writes wait for explicit operator approval."
      ],
      "setup_steps": [
        {
          "title": "Create an OAuth client",
          "description": "Create a Web application client and register the exact TrustyClaw callback URI.",
          "link_url": "https://provider.example/oauth-guide",
          "link_label": "View provider instructions",
          "image_path": "/guide-assets/provider-oauth-client.png",
          "image_alt": "Provider Web application client form.",
          "show_callback": true,
          "show_config": false
        }
      ],
      "data_summary": {
        "cards": [
          {
            "title": "What leaves this host",
            "description": "Only the query text, filters, and resource ids an action uses.",
            "points": [],
            "links": []
          },
          {
            "title": "Where it can go",
            "description": "Only to Provider's API service.",
            "points": [],
            "links": []
          },
          {
            "title": "What Provider can do with it",
            "description": "Provider processes request data under its privacy policy.",
            "points": [{"label": "Before connecting", "text": "Review the connected account's data settings."}],
            "links": [{"label": "Provider privacy policy", "url": "https://provider.example/privacy"}]
          },
          {
            "title": "How long Provider retains it",
            "description": "Provider retains request records for at most 90 days.",
            "points": [],
            "links": [{"label": "Provider privacy policy", "url": "https://provider.example/privacy"}]
          }
        ]
      },
      "connection_status": {"connected": true, "account": {"id": "provider-sub-1", "label": "operator@example.com", "scopes": ["..."]}}
    }
  ]
}
```

The example above is illustrative; the fields are the same for every bundled
tool. Each tool object has:

| Field | Meaning |
| --- | --- |
| `tool_id` | Stable package identifier; keys config, credentials, approvals, and audit records. |
| `display_name`, `description` | Operator-facing name and one-line summary from the manifest. |
| `connection` | `oauth` (operator third-party auth) or `enable_only` (deployment key only). |
| `enabled` | Whether the operator has enabled the tool for agent calls. |
| `actions[]` | Each action's stable `id`, `description`, per-action `data_policy`, `approval` (`direct` or `operator`), `input_schema`, and `output_schema` (empty `{}` for approval-gated actions, which return a user-visible message rather than a JSON result). |
| `config[]` | This tool's declared config keys with `description` and `set`. All config is secret and scoped per tool; values are never returned (see `PUT /v1/tools/{tool_id}/config`). |
| `protections[]` | Short operator-facing safeguards rendered in the tool's info popover and full Integration Guides entry. |
| `setup_steps[]` | Ordered provider-side and TrustyClaw setup steps. A step may include a provider documentation link and a local audited screenshot with alt text; `show_callback`/`show_config` render this host's OAuth callback URI or the tool's config keys inside that step. |
| `data_summary` | The operator-facing data story as exactly four `cards`, in order: what leaves this host, where it can go, what the third party can do with it, and how long it retains it. Each card has a `description` and/or labeled `points`, plus authoritative policy `links`. |
| `connection_status` | OAuth tools only: `{"connected": bool, "account"?: ConnectionAccount}`; never contains tokens or client secrets. |

`ConnectionAccount` is the explicit connected-account structure every OAuth tool
returns and the host stores/displays: `{"id", "label", "scopes"}` — `id` is the
stable provider account identifier (e.g. a Google `sub`) used to bind approvals
to the connected account, `label` is the human-readable account (an email), and
`scopes` are the granted OAuth scopes.

Approval record:

```json
{
  "approval_id": "approval_7.Xr9K2unguessable-token",
  "tool_id": "gmail",
  "action_id": "send_email",
  "status": "pending",
  "summary": "Send Gmail message to billing@example.com with subject \"Invoice\".",
  "payload": {"...": "the exact JSON the tool executes if approved"},
  "result": "",
  "created_at": 1782200000,
  "decided_at": 0
}
```

| Field | Type | Value |
| --- | --- | --- |
| `approval_id` | string | Host-assigned id `approval_<number>.<token>`: the sequential number plus an unguessable capability token, so the id itself is the agent's poll capability and a guessed number never resolves. |
| `tool_id` | string | The tool the approval belongs to. |
| `action_id` | string | The manifest action id (`ActionSpec.id`) the approval will execute. |
| `status` | string | One of `pending`, `approved`, `denied`, `expired`, `executed`, `failed`. Terminal states are `denied`, `expired`, `executed`, `failed`. |
| `summary` | string | Redacted, operator-displayable description of the proposed action (1-500 UTF-8 bytes). |
| `payload` | object | The exact JSON the tool executes if approved (up to 64 KiB). Omitted from the list response; returned by `GET /v1/tools/{tool_id}/approvals/{approval_id}`. |
| `result` | string | The terminal outcome text: the executed action's user-visible `ApprovalExecuted.message`, or the failure error. Empty until `executed` or `failed`. |
| `created_at` | integer | Unix seconds when the approval was created. |
| `decided_at` | integer | Unix seconds when the approval reached a terminal state; `0` while `pending`. |

Every approval is single-use, and `pending` approvals expire after 24 hours. New
approval creation is capped while too many are already pending, so a runaway agent
cannot grow admin storage without bound or hide older decisions from the operator.
Agents queue approvals by calling approval-gated actions through the tools MCP
surface; the pending call response carries the token-bearing `approval_id`, which
`check_tool_approval` verifies before returning the summary or terminal result, so
another agent process cannot enumerate old approvals by guessing sequential ids.
Only these admin endpoints decide approvals.

Tool event summary (from `/v1/tools/events`):

```json
{
  "seq": 412,
  "timestamp": "2026-07-08T01:15:00Z",
  "event_id": "tool_event_412",
  "tool_id": "example_tool",
  "action_id": "send_item",
  "outcome": "executed",
  "detail": "approval_7",
  "has_arguments": true
}
```

| Field | Type | Value |
| --- | --- | --- |
| `seq` | integer | Monotonic event id, returned newest-first. Page older events by setting `before` to the oldest `seq` you have. |
| `timestamp` | string | ISO 8601 UTC time the event was recorded. |
| `event_id` | string | `tool_event_<seq>`. |
| `tool_id` | string | The tool the event concerns. |
| `action_id` | string | The manifest action id (`ActionSpec.id`) for a call; `oauth_connect` for a connect/disconnect, `enablement` for an enable/disable, or `config` for a config change. |
| `outcome` | string | For a tool call: `executed`, `pending_approval`, or `failed`. For an approval decision: `executed`, `failed`, or `denied`. For a connection change: `connected` or `disconnected`. For an enablement change: `enabled` or `disabled`. For a config change: `set` or `cleared`. |
| `detail` | string | Short context string: an error message, the related `approval_id`, the connected account label, or the config key that changed. May be empty. |
| `has_arguments` | boolean | `true` for an accepted tool call or approval decision, including calls whose exact argument object is `{}`. `false` for config, enablement, and connection lifecycle events. |

`GET /v1/tools/events/{seq}` returns the same fields plus `arguments`, either
the exact schema-validated tool input, the exact approved/denied payload, or
`null` for a lifecycle event. Argument objects are capped at 64 KiB. They are
stored in the local Postgres `tool_events.arguments` column. The tools service
writes tool-call, approval, and OAuth events through its scoped database role;
the admin service writes config and enablement events and reads the table for
the Tool Audit Log. The UI loads arguments only after the operator expands an
event. Tool config values and OAuth callback parameters are never stored as
event arguments.

## Host Runtime

```text
POST /v1/host-runtime/reboot
```

Host runtime endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `POST` | `/v1/host-runtime/reboot` | none | Host runtime mutation response | Reboots the host machine. |

Host runtime mutation response:

```json
{
  "status": "accepted"
}
```

Host runtime mutation response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `status` | enum | `accepted` | Host runtime operation was accepted and will be applied asynchronously. |
