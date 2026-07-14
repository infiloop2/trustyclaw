# Admin API Architecture

The admin API binds `127.0.0.1:7443` and is reached through an SSH port forward,
the optional Cloudflare Tunnel, or both. nftables drops other inbound traffic
and drops `trustyclaw-agent` loopback traffic to the admin port; the agent can
reach only the proxy port. Every API request needs the bearer admin password,
which is hashed and compared in constant time against the stored hash. Static
admin/app UI assets and the side-effect-free OAuth callback shell are the only
unauthenticated HTTP routes. The browser keeps the password in a cookie and
adds it as the bearer header for API calls.

App backends are reached only through the admin API reverse proxy. Each app
service binds a host-assigned `127.0.0.1` port, and nftables accepts new
connections to that port only from the `trustyclaw-admin` uid before dropping
the same port for every other local uid. The app receives a host proxy marker,
not the operator's raw admin bearer. Agent runtimes, app service users, and
ordinary local users cannot call app backend TCP listeners directly.

The agent, network, and tool event logs each keep the newest 1,000,000 entries.

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
timestamp. Claude's check performs a live authenticated usage probe for the
pinned token, or provider profile attestation for a new or rotated token,
before it publishes `active`. This lets Claude Code refresh its token and
prevents a cached-but-rejected credential from staying connected. Codex's
usage read is also live; if it fails for a pinned account, TrustyClaw asks
Codex to force one credential refresh before deciding whether the account is
still active. A locally cached provider account is therefore insufficient for
either runtime.

Each live-validation verdict is remembered in process memory, so validation
generates provider traffic at most once per scheduled recheck even though
loading or awaiting-login runtimes are polled every five seconds and Claude
tasks re-check runtime status at claim (the full lifecycle is in
[Agent provider lifecycle](agent-provider-lifecycle.md)). An authentication failure is final
for that credential: the runtime stays `awaiting_login` with no further
provider traffic until an operator login or account reset replaces the
credential. Any other validation failure surfaces as `error` and is retried
on the next scheduled recheck. When a new or rotated Claude token is
validated by attestation, the same refresh reads usage once right after it
publishes the token's proxy pin, so usage appears immediately after login.

Route handlers are thin: validate the documented protocol, read or update
admin state (the local Postgres database — see
[Admin state storage](admin-state-storage.md)), and delegate to the
orchestrator, the selected runtime client, or the fixed sudo helpers. Agent
file list/read routes cross into the private agent home through
`read-agent-file`, which demotes to `trustyclaw-agent`, confines paths to
`agent-home`, rejects symlinks, caps listing scan work and responses at 1,000
entries, opens files nonblocking, and caps reads at 1 MiB. The orchestrator
runs six worker threads, with a claim cap of three tasks per agent runtime:

`GET /v1/agent-processes` is a read-only diagnostic endpoint. It walks
descendant `cgroup.procs` files under `trustyclaw_agent.slice`, then reads
basic process metadata from `/proc/<pid>` without sudo or shelling out to `ps`.
The result is intentionally not task state: turn processes exit shortly after
their task finishes, and child processes normally inherit the runtime cgroup
and show up in the same agent slice.

- Every task names a client-chosen `thread_id`. The first task also names an
  `agent_runtime` (`codex` or `claude_code`) and one allowed model/effort pair,
  which binds all four values and starts a runtime conversation. Later tasks
  may omit the runtime, model, and effort; the host loads the thread's fixed
  configuration and resumes the recorded Codex thread id or Claude session id
  (the maps live in the `thread_sessions` table, capped at the 100,000 most
  recently used per runtime). Tasks on one runtime/thread pair run serially in
  creation order; tasks on different
  pairs run in parallel, up to six total and up to three per runtime.
- Each running task gets its own runtime process, spawned through the sudo
  helper and closed when the turn ends. Codex turns resume their provider
  thread by id on a fresh app-server; Claude Code turns resume by session id.
- A task runs as one agent turn. Codex receives the selected model on thread
  start/resume and the selected model and effort on every turn; it uses
  `turn/steer` for steering. Claude Code receives `--model` and `--effort` on
  every new or resumed CLI process and receives steering as additional
  stream-json user messages. Each completed agent message becomes a
  `task.message` event, and the last one becomes the task's `output_message`.
- A task runs only while its selected runtime is `active`. A worker that
  claims a task for a non-active runtime fails it immediately with that
  status as the error message; queued work never parks behind a missing
  login.
- If a runtime becomes non-active after policy disable, login expiry, or a
  health-check error, the orchestrator fails that runtime's running tasks and
  closes all live runtime processes for it.
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
runtime, and leaves the audit logs to their own amortized on-append pruning.
The operator inputs maintenance cannot bound are capped at the API instead:
at most 1,000 queued tasks and 20 undelivered steers per task (both 409 past
the cap). A delivered steer is dropped from state once it is handed to the
turn — its content is already preserved as a `task.message` event.

A separate process-local poller checks for a newer public TrustyClaw version
when the admin service starts and every four hours afterward. Because the admin
user has no egress, it invokes the fixed `check-for-upgrade` root helper, which
can only read `infiloop2/trustyclaw`'s main-branch `VERSION` file over HTTPS.
The admin service validates and compares the result, then exposes it through
`/v1/health`. A failed check preserves the last successful advisory result,
does not degrade host health, and is retried from scratch on the next poll.
