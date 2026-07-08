# Admin API

The TrustyClaw admin API is served by the localhost admin service.

Base URL after port forwarding:

```text
http://127.0.0.1:7443
```

Every request must include:

```text
Authorization: Bearer <admin-password>
```

Every mutating request (`POST`, `PUT`, and `DELETE`) must include:

```text
Idempotency-Key: <client-generated-id>
```

`Idempotency-Key` must be 1 to 128 characters and match
`^[A-Za-z0-9._:-]+$`.

Replaying a mutating request with the same `Idempotency-Key`, method, and path returns
the original successful response without executing the request again. Keys are retained
for 24 hours (a replay after that window re-executes). Reusing a key for a different
method or path returns `400`. A replay that arrives while the first request with that
key is still executing returns `409`.

All responses are JSON. `GET /` serves the bundled admin UI page; every other route
requires the bearer admin password.

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
| `404` | Requested task or route does not exist. |
| `409` | Request conflicts with current agent runtime or task state. |
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
  "host_runtime": {
    "cpu": {
      "usage_percent": 12.5
    },
    "memory": {
      "used_bytes": 980000000,
      "total_bytes": 2147483648
    },
    "filesystem": {
      "used_bytes": 6000000000,
      "total_bytes": 17179869184,
      "mounts": {
        "root": {
          "used_bytes": 6000000000,
          "total_bytes": 17179869184
        },
        "admin": {
          "used_bytes": 250000000,
          "total_bytes": 8589934592
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
| `agent_runtime.runtimes` | array |  | Status records for both supported runtimes. |
| `agent_runtime.runtimes[].type` | enum | `codex`, `claude_code` | Agent runtime type. |
| `agent_runtime.runtimes[].status` | enum | `deactivated`, `loading`, `awaiting_login`, `active`, `error` | Current agent runtime supervisor state. |
| `agent_runtime.runtimes[].active_task_ids` | string array |  | Currently running task ids for this runtime. |
| `network_controls.status` | enum | `active`, `error` | Derived network policy enforcement state. |
| `version.status` | enum | `ok`, `mismatch`, `error` | Version health for the running root volume and preserved admin state. |
| `version.runtime` | string or null |  | TrustyClaw version from `/opt/trustyclaw-host/VERSION`. |
| `version.state` | string or null |  | TrustyClaw preserved-state version from admin disk `version.json`. |
| `host_runtime.cpu.usage_percent` | number | 0-100 | Current host CPU usage percentage. |
| `host_runtime.memory.used_bytes` | integer |  | Current host memory used, in bytes. |
| `host_runtime.memory.total_bytes` | integer |  | Total host memory, in bytes. |
| `host_runtime.filesystem.used_bytes` | integer |  | Current root filesystem (`/`) used space, in bytes. Kept for compatibility; the admin UI focuses on the admin and agent data-volume mounts below. |
| `host_runtime.filesystem.total_bytes` | integer |  | Total root filesystem (`/`) capacity, in bytes. Kept for compatibility; the admin UI focuses on the admin and agent data-volume mounts below. |
| `host_runtime.filesystem.mounts.root.used_bytes` | integer |  | Current root filesystem used space, in bytes. |
| `host_runtime.filesystem.mounts.root.total_bytes` | integer |  | Total root filesystem capacity, in bytes. |
| `host_runtime.filesystem.mounts.admin.used_bytes` | integer | optional | Current admin data volume (`/mnt/trustyclaw-admin`) used space, in bytes. |
| `host_runtime.filesystem.mounts.admin.total_bytes` | integer | optional | Total admin data volume (`/mnt/trustyclaw-admin`) capacity, in bytes. |
| `host_runtime.filesystem.mounts.agent.used_bytes` | integer | optional | Current agent data volume (`/mnt/trustyclaw-agent`) used space, in bytes. |
| `host_runtime.filesystem.mounts.agent.total_bytes` | integer | optional | Total agent data volume (`/mnt/trustyclaw-agent`) capacity, in bytes. |
| `host_runtime.swap.allocated_bytes` | integer |  | Filesystem-backed RAM swap allocated to the host, in bytes. |
| `host_runtime.swap.used_bytes` | integer |  | Current filesystem-backed RAM swap used, in bytes. |

Runtime status is `deactivated` when that runtime's managed provider
integration is disabled, `loading` while the runtime is starting, `awaiting_login` while
the runtime needs operator login, `active` while it can accept work, and `error`
when the runtime supervisor cannot make it healthy.

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
| `POST` | `/v1/agent-runtime/reset-linked-account` | `{"agent_runtime": "..."}` | status response | Clears the linked (operator-approved) account anchor, local agent auth files, pending OAuth state, and the proxy account pin for that runtime. |

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
`{"agent_runtime": "codex"}` / `{"agent_runtime": "claude_code"}` to refresh one.
It runs the same provider status refresh used by the background status loop and
may invoke the provider CLI. It returns the same response shape as
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
| `runtimes[].type` | enum | `codex`, `claude_code` | Agent runtime type. |
| `runtimes[].status` | enum | `deactivated`, `loading`, `awaiting_login`, `active`, `error` | Current runtime state. |
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
        "weekly_used_percent": 0,
        "weekly_resets_at_text": "Jul 3, 3:59pm (UTC)",
        "last_checked_at": "2026-06-29T23:10:00Z"
      }
    }
  ]
}
```

Agent account response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `accounts[].agent_runtime` | enum | `codex`, `claude_code` | Agent runtime type. |
| `accounts[].provider` | enum | `openai`, `claude` | Managed AI provider for the runtime. |
| `accounts[].status` | enum | `deactivated`, `loading`, `awaiting_login`, `active`, `error` | Current runtime account status. |
| `accounts[].account_id` | string | optional | The linked provider account id (the operator-approved anchor). Present whenever an account is linked, including while the runtime is not active: the anchor outlives session expiry and deactivation until an operator reset clears it. |
| `accounts[].email` | string | optional | Present when available from the linked account metadata. |
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
| `accounts[].claude_usage` | object | optional | Claude Code usage metadata parsed from `claude -p "/usage" --output-format json`. |
| `accounts[].claude_usage.current_session_used_percent` | number | optional | Percent used for the current Claude Code session. |
| `accounts[].claude_usage.weekly_used_percent` | number | optional | Percent used for the current Claude Code weekly window across all models. |
| `accounts[].claude_usage.weekly_resets_at_text` | string | optional | Provider-rendered reset time text from the Claude Code `/usage` response. |
| `accounts[].claude_usage.last_checked_at` | string | optional | UTC timestamp when TrustyClaw last refreshed the cached Claude usage snapshot. Active runtimes are rechecked every 300 seconds. |

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
```

Every task belongs to a client-chosen thread (`thread_id`). Tasks on the same
thread share one runtime conversation and run one at a time in creation order;
tasks on different threads run in parallel, up to 6 total and up to 3 per
runtime. Codex keeps the app-server for a recently used thread warm, so a
follow-up task on the same thread skips the app-server start; Claude Code
resumes by the recorded session id. To start a fresh conversation with no prior
context, use a new `thread_id`. A `thread_id` belongs to the first runtime that
uses it; creating a task for the same `thread_id` with another runtime returns
`409`. `agent_runtime` chooses which runtime should execute the task. A queued task is claimed only when its chosen runtime is
`active`; tasks for a `deactivated`, `loading`, `awaiting_login`, or `error`
runtime remain queued. If a runtime leaves `active` while tasks are running
because its provider is disabled, its login expires, or its health check fails,
the host closes that runtime's live processes and marks those running tasks
`failed`.

Task endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `POST` | `/v1/tasks` | Create task request | Task response | Creates a task for the agent runtime. Returns `409` when 1,000 tasks are already queued. |
| `GET` | `/v1/tasks?last_seen_task_id=<task_id>` | `last_seen_task_id` query parameter is optional | Task list response | Lists up to 5 current and pending tasks with their status, in execution order. |
| `GET` | `/v1/tasks/{task_id}` | none | Task response | Returns one task. |
| `PUT` | `/v1/tasks/{task_id}` | Update task request | Task response | Updates one pending task. Only tasks with status `queued` can be updated. |
| `POST` | `/v1/tasks/{task_id}/steer` | Steer task request | Steer task response | Sends additional steering to one running task. Only tasks with status `running` can be steered. |
| `POST` | `/v1/tasks/{task_id}/cancel` | none | Task cancel response | Requests cancellation for one pending task. Only tasks with status `queued` can be cancelled. |
| `POST` | `/v1/tasks/{task_id}/kill` | none | Task kill response | Kills one running task: its runtime process is terminated and the task becomes `cancelled`. Only tasks with status `running` can be killed; returns `409` otherwise. The thread itself survives — a later task on the same `thread_id` resumes the conversation. |
| `GET` | `/v1/threads` | none | Thread list response | Lists recent runtime threads, including active queued/running work and retained runtime session mappings. |
| `GET` | `/v1/threads/{thread_id}/tasks` | none | Task list response | Lists retained tasks for one thread, newest first by `updated_at` with task id as a tiebreaker. |

Create task request:

```json
{
  "agent_runtime": "codex",
  "input_message": "Implement this change and report the result.",
  "thread_id": "feature-chat-1"
}
```

Create task request fields:

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `agent_runtime` | Yes | enum | Runtime to execute the task: `codex` or `claude_code`. |
| `input_message` | Yes | string | Task message for the agent runtime. Must be 1 to 50,000 characters. |
| `thread_id` | Yes | string | Client-generated conversation id this task belongs to. Must be 1 to 64 characters of `A-Z`, `a-z`, `0-9`, `-`, or `_`. The first task on a thread starts a new runtime conversation; later tasks on the same thread continue it. A thread id cannot be reused across runtimes. The host retains the 1,000 most recently used thread mappings; a task on an older thread starts a fresh conversation. |

Task response:

```json
{
  "task_id": "task_123",
  "status": "completed",
  "agent_runtime": "codex",
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
| `agent_runtime` | enum | `codex`, `claude_code` | Runtime assigned to this task. |
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
| `tasks[].queue_position` | integer | Queue position for this task. `0` marks every currently running task (up to 6 total and up to 3 per runtime run in parallel). Pending tasks use `1`, `2`, `3`, and so on in creation order. If no task is running, pending tasks still start at `1`. A pending task can run ahead of an earlier one when the earlier task waits on a busy thread or when an earlier task's runtime is already at its per-runtime cap. |

Thread list response:

```json
{
  "threads": [
    {
      "thread_id": "feature-chat-1",
      "agent_runtime": "codex",
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
| `threads[].agent_runtime` | enum | Runtime for this thread entry: `codex` or `claude_code`. |
| `threads[].last_used_at` | string | Latest retained task update or runtime session use timestamp known for this thread/runtime. |
| `threads[].active_tasks` | array | Queued or running retained tasks on this thread/runtime. Empty when no task is currently active. |
| `threads[].task_count` | integer | Number of retained task records for this thread/runtime. Older finished tasks can be pruned. |

Thread task list response fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `tasks` | Task response array | Up to 1,000 retained tasks for the selected thread, newest first by `updated_at` with task id as a tiebreaker. The host keeps active tasks and the 1,000 most recently updated finished tasks globally before pruning older task records. |

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
| `steer_message` | Yes | string | Additional steering message for the running task. Must be 1 to 50,000 characters. A task holds at most 20 undelivered steer messages at a time; delivered steers leave the queue (their content is preserved as `task.message` events). |

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
or when the task already holds 20 undelivered steer messages.

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
`{"agent_runtime": "codex"}` or `{"agent_runtime": "claude_code"}`.

`agent_runtime.login_completed` uses `task_id: null` and payload
`{"agent_runtime": "codex"}` or `{"agent_runtime": "claude_code"}`.

`agent_runtime.linked_account_reset` uses `task_id: null` and payload
`{"agent_runtime": "codex"}` or `{"agent_runtime": "claude_code"}` when an
operator reset cleared that runtime's linked account (the audit record of the
reset-linked-account endpoint).

`agent_runtime.deactivated` uses `task_id: null` and payload
`{"agent_runtime": "codex"}` or `{"agent_runtime": "claude_code"}` when a
runtime is disabled because its managed provider integration is disabled.

## Agent Files

```text
GET /v1/agent-files?path=<path>
GET /v1/agent-files/read?path=<path>
```

Agent file endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agent-files?path=<path>` | `path` query parameter is optional; default `/` | Agent file list response | Lists one directory under the agent home, including hidden entries. |
| `GET` | `/v1/agent-files/read?path=<path>` | `path` query parameter is optional; default `/` | Agent file read response | Reads one regular file under the agent home as a UTF-8 text preview. |

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

## Agent Processes

```text
GET /v1/agent-processes
```

Returns a read-only diagnostic snapshot of Codex, Claude Code, and processes
spawned by those runtimes. This is process state, not task state: app-server
processes may stay warm between tasks, and short-lived turn processes may exit
before the next snapshot. The response contains at most 1,000 processes; when
more matching processes exist, `truncated` is `true`.

Response:

```json
{
  "truncated": false,
  "processes": [
    {
      "pid": 1234,
      "ppid": 1,
      "user": "trustyclaw-agent",
      "state": "S",
      "name": "codex",
      "cmdline": "codex app-server --listen stdio://",
      "rss_bytes": 92274688,
      "elapsed_seconds": 184,
      "scope": "run-r123.scope"
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
| `POST` | `/v1/network-tools/github-pending-pushes/<id>/approve` | none | `{pending_push: {...}}` | Replays the held push to GitHub with the working token through the `approve-github-push` root helper and marks it approved. `404` if unknown, `409` if already resolved or the replay fails. Replay failures mark the row `failed` after one best-effort cleanup. |
| `POST` | `/v1/network-tools/github-pending-pushes/<id>/reject` | none | `{pending_push: {...}}` | Cleans up pending quarantine refs and marks the held push rejected. `404`/`409` as above; cleanup failures mark the row `failed`. |
| `GET` | `/v1/network/events?before=<seq>&decision=<allowed\|denied\|all>&limit=<n>` | query parameters optional | Network event response | Lists newest network decision events before an optional sequence cursor. |

The `github-credential` routes work whether or not
`managed_network_integrations.github` is enabled, so the credential can be
staged before the integration is turned on; the proxy-injected working token
is only ever published while GitHub is enabled.

Network policy request:

```json
{
  "managed_network_integrations": {
    "openai": {"enabled": true},
    "github": {
      "enabled": true,
      "write_repositories": [
        {"owner": "infiloop2", "repo": "trustyclaw"}
      ]
    }
  },
  "allowed_network_access": {
    "example.com": {
      "allow_http_methods": ["GET", "HEAD"],
      "path_guards": ["^/$", "^/docs(?:/.*)?$"]
    }
  }
}
```

The request body is the replacement runtime network controls object using the
schema from [`NetworkControls.md`](NetworkControls.md).

When `PUT /v1/network/policy` is accepted, the replacement policy has been
validated and atomically written. Concurrent replacements are serialized; a
request can return `409` if another replacement is already in progress.

Network policy response:

The API response uses the operator-facing network controls shape. Managed
integration domains are not listed in `allowed_network_access`; they are
expanded only in the internal enforcement policy, and credential secrets are
never included.

```json
{
  "network_controls": {
    "managed_network_integrations": {
      "openai": {"enabled": true},
      "github": {
        "enabled": true,
        "write_repositories": [
          {"owner": "infiloop2", "repo": "trustyclaw"}
        ]
      }
    },
    "allowed_network_access": {
      "example.com": {
        "allow_http_methods": ["GET", "HEAD"],
        "path_guards": ["^/$", "^/docs(?:/.*)?$"]
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

A denied event additionally carries a `reason` string explaining the denial
(e.g. `host is not in the allowed network policy`, `live web search is disabled
for this domain`); allowed events omit it.

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
| `reason` | string | optional | Present only on denied events: why the request was refused. |

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
