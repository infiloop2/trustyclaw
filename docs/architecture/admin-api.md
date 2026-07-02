# Admin API Architecture

The admin API is reachable only through SSH port forwarding (it binds loopback,
and nftables drops other inbound traffic). On loopback, nftables additionally
drops `trustyclaw-agent` traffic to the admin API port — the agent only needs the
proxy port, so it cannot reach the API at all. Every request needs the bearer admin
password, which is hashed and compared in constant time against the stored hash.
`GET /` serves the bundled single-page UI (no secrets in the page itself; the
browser keeps the password in a cookie and sends it as the bearer header).

Mutating requests are idempotent: the response for each `Idempotency-Key` is
kept in the service's memory for 24 hours (or until a service restart —
replay records are a retry convenience, not durable state), and a replay with
the same key, method, and path returns it without re-executing. The agent
event table and the proxy's network event log each keep the newest
1,000,000 entries.

`/v1/health` derives network status from policy validity and proxy liveness. If
the persisted policy cannot be parsed or the proxy process is not listening,
health reports `network_controls.status: error` so the operator notices and
repairs it. This failure is safe — nftables blocks the agent's direct egress
independently of the proxy, so a dead proxy leaves the agent with no network at
all, never with unfiltered access.

A background poller re-verifies an `active` agent login at most every five
minutes, so an expired login surfaces in health as `awaiting_login` without
waiting for a task to fail. Provider usage metadata is refreshed by the same
active-runtime check and is cached in account state with a `last_checked_at`
timestamp. Loading or awaiting-login runtimes are polled every five seconds.

Route handlers are thin: validate the documented protocol, read or update
admin state (the local Postgres database — see
[Admin state storage](admin-state-storage.md)), and delegate to the
orchestrator, the selected runtime client, or the fixed sudo helpers. Agent
file list/read routes cross into the private agent home through
`read-agent-file`, which demotes to `trustyclaw-agent`, confines paths to
`agent-home`, rejects symlinks, caps listing scan work and responses at 1,000
entries, opens files nonblocking, and caps reads at 1 MiB. The orchestrator
runs six worker threads, with a claim cap of three tasks per agent runtime:

- Every task names a client-chosen `thread_id`. The first task on a thread
  and an `agent_runtime` (`codex` or `claude_code`). The first task on a
  runtime/thread pair starts a runtime conversation; later tasks on that same
  runtime/thread pair resume the recorded Codex thread id or Claude session id
  (the maps live in the `thread_sessions` table, capped at the 100,000 most
  recently used per runtime). Tasks
  on one runtime/thread pair run serially in creation order; tasks on different
  pairs run in parallel, up to six total and up to three per runtime.
- Each running task gets its own runtime process, spawned through the sudo
  helper. For Codex, the app-server stays warm after the turn, bound to its
  thread, so a follow-up task on the same thread skips the app-server boot; the
  least recently used idle server is closed when its slot is needed for another
  thread. Claude Code turn processes exit after each turn, and later turns
  resume by session id.
- A task runs as one agent turn. Codex uses `thread/resume` + `turn/start` with
  `turn/steer`; Claude Code uses stream-json input and receives steering as
  additional user messages on the running CLI process. Each completed agent
  message becomes a `task.message` event, and the last one becomes the task's
  `output_message`.
- Tasks stay `queued` until their selected runtime is logged in (`active`).
- If a runtime becomes non-active after policy disable, login expiry, or a
  health-check error, the orchestrator fails that runtime's running tasks and
  closes all live runtime processes for it. Queued tasks remain queued until the
  runtime becomes `active` again.
- `POST /v1/tasks/{id}/kill` cancels a running task by terminating its
  runtime process — the one reliable abort for a stuck turn. The thread
  survives, so a later task on the same `thread_id` resumes the conversation;
  it stays unclaimable until the old process has fully shut down, so a new
  turn never races a dying one for the same runtime thread/session.
  A worker that was mid-turn cannot resurrect a cancelled task. After a host
  reboot, tasks left `running` are marked `failed`.
- Codex login uses its device-code flow. Claude Code login starts
  `claude auth login --claudeai`, returns the browser URL, and later writes the
  browser code back to the waiting CLI process. After login, the admin service
  infers provider account metadata from the agent user's auth files and stores
  only the account id / bearer hash needed by the proxy guards.

A scheduled maintenance pass (hourly, never on the request path) bounds state
growth with indexed deletes: it keeps every active task plus the 100,000 most
recent finished ones as history, caps the thread->session maps at 100,000 per
runtime, drops in-memory idempotency entries after 24 hours or beyond the
10,000 newest, and trims the agent event table to the newest 1,000,000.
The operator inputs maintenance cannot bound are capped at the API instead:
at most 1,000 queued tasks and 20 undelivered steers per task (both 409 past
the cap). A delivered steer is dropped from state once it is handed to the
turn — its content is already preserved as a `task.message` event.
