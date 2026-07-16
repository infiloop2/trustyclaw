# Agent Chat

Agent Chat is an installed app (see [Apps](apps.md) for the platform
contract) that gives the operator threaded conversations with the agent. It is
the plainest possible app: each thread is a sequence of host tasks on one
agent runtime, and the app's job is organizing those threads, not changing how
tasks run.

## What The App Owns

The app owns presentation and thread bookkeeping in its `app_agent_chat`
schema:

- `threads`: the thread index: app thread id and archive state. Session
  configuration (agent runtime, model, effort) is host-owned and no longer
  stored here.
- `thread_tasks`: which host tasks belong to which thread.

Task contents and execution stay host-owned. The app backend reads them
through the allowlisted app-backend socket routes and never copies transcripts
into its own schema, so the host remains the single source of truth for what
the agent actually did.

## How It Works

The UI lists unarchived threads newest-first with their tasks and statuses.
Sending a message either creates a new thread (picking the runtime with the
first message) or appends a task to an existing one:

- `POST /tasks` creates a host task via the app-backend socket
  (`POST /v1/tasks`) with the thread's runtime and thread id, and records the
  returned task id in `thread_tasks`.
- `GET /tasks/<task_id>` proxies the host task read so the UI can poll status
  and output. `POST /tasks/<task_id>/cancel|kill|steer` proxy the matching
  host controls; every task id is first checked against `thread_tasks`, so the
  app only ever touches its own tasks.
- `POST /threads/<thread_id>/archive` hides a thread from the index without
  touching host state.

Thread ids the app sends over the socket are app-scoped by the host
(`agent_chat__<thread_id>` internally), so Agent Chat cannot reach threads
created by another app or by the core admin surface.

## Security Posture

Agent Chat introduces no agent-controlled write channel into the app: the
agent's output is displayed, never parsed for instructions. The UI
HTML-escapes all task output before rendering. Everything else is the standard
app-platform boundary: sandboxed opaque-origin UI frame, bridge-only backend
access, peer-authenticated socket with the narrow task/thread allowlist, and
an app role limited to the `app_agent_chat` schema.
