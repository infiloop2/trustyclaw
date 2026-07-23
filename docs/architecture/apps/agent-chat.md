# Agent Chat

Agent Chat is an installed app (see [Apps](apps.md) for the platform
contract) that gives the operator threaded conversations with the agent. It is
the plainest possible app: each thread is a sequence of host tasks on one
agent runtime, and the app's job is organizing those threads, not changing how
tasks run.

The admin shell hardwires Agent Chat as the host's main interface: the home
tab opens with a "Begin chat" navigator and the app sits directly below Home
in the navigation. Its manifest still declares the required
`release_stage: "stable"`; the hero placement is the shell's one special case.

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
The index is one bulk host call: `GET /v1/threads` over the app-backend
socket returns summaries (runtime, model, effort, last-used time, task count,
active task ids) for exactly this app's threads, which the backend joins
against its own `archived` flags. It costs one round trip regardless of thread
count, and the app stores no task data of its own. A thread the app recorded
but whose task creation failed (a lost generated-name reservation) has no host
summary and stays invisible.

Sending a message either creates a new thread (picking the runtime with the
first message) or appends a task to an existing one:

- The composer accepts up to ten optional file attachments. Each file has its
  own immediate Remove control. The app asks the
  host-owned parent bridge to open the native file picker and retain the
  selection in browser memory. When the operator sends the message, the app
  asks the parent to upload each selection sequentially through
  `POST /v1/agent-files/upload`, then creates the task after every upload
  succeeds and appends one
  `[User-uploaded file: user-files/<timestamp>_<name>]` line per file to
  `input_message`.
  The host's immutable agent instructions define that reference and the
  `user-files/` directory for every runtime. Clearing or abandoning an
  unsubmitted attachment creates no workspace file. Files over 25 MiB remain
  visible with a per-item error and cannot be sent. If an upload fails, files
  already uploaded keep their returned paths and a later Send retries only
  unfinished files. If task creation fails after every upload, pressing Send
  again reuses every returned path. Durable files remain available without
  cleanup or reconciliation. There is no separate aggregate-byte limit; the
  ten-file and 25 MiB-per-file bounds limit one message to 250 MiB.
- `POST /tasks` creates a host task via the app-backend socket
  (`POST /v1/tasks`) with the thread's runtime and thread id, and records the
  returned task id in `thread_tasks`. A request without `thread_id` starts a
  new thread: the backend generates the next successive name (`thread-1`,
  `thread-2`, ...), counting over every thread it has ever recorded, archived
  included, so a generated id never revives an archived thread. The name is
  reserved by inserting its `threads` row before the host call; the primary
  key makes concurrent generators take distinct names. A reservation whose
  host call fails stays as an empty thread: the index hides threads without
  tasks and the generator counts it, so its number is skipped rather than
  reused. The operator never types a thread id.
- `GET /tasks/<task_id>` proxies the host task read so the UI can poll status
  and output. `POST /tasks/<task_id>/cancel|kill|steer` proxy the matching
  host controls; every task id is first checked against `thread_tasks`, so the
  app only ever touches its own tasks. Hermes has no mid-turn steering input,
  so its running tasks omit the Steer control; the host also rejects a direct
  Hermes steer request. The composer queues later input as a new task on the
  same thread, which runs after the current task finishes.
- `GET /threads/<thread_id>/events?since=<seq>` proxies the host thread event
  stream so the UI shows every message of every task, not just the prompt and
  final answer: interim agent progress and mid-task operator steering both
  render inline, for running and finished tasks alike. The UI accumulates the
  stream forward-paged by `seq`, so a poll fetches only new events.
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
