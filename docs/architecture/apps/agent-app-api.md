# Agent App API

How an agent working an app-created task calls that app's backend directly —
many round trips per turn, synchronous validation errors, reads on demand —
without weakening the app platform's security boundary. This is the
high-bandwidth successor to parsing structured action blocks out of a task's
final `output_message`: the app still owns every write, but the agent can now
converse with the app backend *during* the turn instead of batching intents
into its final message.

The pieces:

- an opt-in **`agent.api` manifest field**
  (`"agent": {"instructions": "agent.md", "api": true}`) that marks an app's
  backend as serving agent-callable routes under `/agent/`,
- a stable **`app_api` MCP tool** served by the existing tools MCP shim,
  whose presence grants no authority,
- a dedicated **`trustyclaw-agent-app` service** that authenticates the
  caller, derives its app-scoped host thread, and reverse-proxies the
  call to the owning app backend's loopback port,
- **kernel-verified thread attribution**: every task turn runs in a systemd
  scope named after its host thread id, and the service reads that thread from
  the caller's cgroup, which no agent process can rewrite.

## The security boundary in one sentence

**The host proves the caller's app-prefixed thread from kernel state, resolves
the owning app from that reserved prefix and its installed manifest, and
forwards the request only to that app's `/agent/` routes; nothing the agent
claims is trusted, and no new reach is granted anywhere else.**

## Why not fenced action blocks, tokens, or claimed ids

The first app-agent protocol (structured action blocks parsed from
`output_message`) is secure but low-bandwidth: one batch of writes per turn,
no mid-turn reads without budgeted continuation turns, and validation errors
that surface a full turn later. Moving to a call surface raises one question:
when something connects and says "I belong to app A", how does the host know?

- **A claimed app or thread id proves nothing.** Every agent process runs as
  `trustyclaw-agent`, so socket peer credentials distinguish "an agent" but
  not which app owns the process. Any prompt-injected agent could name another
  app's task.
- **A secret token barely helps here.** Everything that can reach
  the socket (agent-uid processes) can also read another agent process's
  environment (`/proc/<pid>/environ` is same-uid readable), and argv is
  world-readable, so the set of possible thieves is exactly the set of
  callers the token was meant to distinguish.
- **The cgroup is kernel state.** The `run-claude-code` /
  `run-codex-app-server` root helpers already start every runtime in a
  transient systemd scope; naming that scope after the host thread id
  (`trustyclaw-agent-thread-<thread_id>.scope`) puts the thread identity
  somewhere only the kernel writes. A process cannot move itself into another
  cgroup (the cgroupfs is root-owned, delegation off), cannot mint a fake scope in
  `trustyclaw_agent.slice` (systemd requires privileges; user-manager scopes
  land under `user.slice` and are rejected by the parser), and cannot ptrace
  a non-child same-uid process into cooperating (Yama `ptrace_scope`).

So attribution is: `SO_PEERCRED` → peer pid → `/proc/<pid>/cgroup` →
`trustyclaw-agent-thread-<thread_id>.scope` under `trustyclaw_agent.slice`.
The pid is pinned with a pidfd before the /proc read and its liveness re-checked after
(polling the pidfd for exit readiness requires no cross-user signal
permission), so a pid that exited and was recycled mid-check fails closed
instead of inheriting the old process's scope.

App backends submit app-visible thread ids through their peer-authenticated
admin socket. The host prefixes them as `<app_id>__<thread_id>`; operator task
creation rejects that reserved namespace. The agent-app service splits this
kernel-attributed prefix, resolves the installed app, and requires its
manifest to set `agent.api` to true. Ordinary operator threads have no app
prefix, and non-opted-in apps fail closed even though the shim's stable tool
list includes `app_api`. No attribution database or task-lifecycle registry is
needed. A thread spans turns, which is intentional: app ownership is a
workspace property. Each runtime scope is collected when its turn process
exits, and a later turn on the same thread creates a fresh scope with the same
trusted name. Apps can additionally require an active run on the forwarded
thread, as Mission Pursuit does in its own database.

## The dedicated service

`trustyclaw-agent-app` follows the tools-service pattern: its own Linux user
and a world-connectable Unix socket
(`/run/trustyclaw-agent-app/agent-app.sock`) authenticated by peer
credentials, accepting only the `trustyclaw-agent` uid. It is deliberately
its own process rather than new admin-service surface: the admin plane gains
no agent-facing socket, and the tools service (the only uid with internet
egress) gains no app traffic. The service's entire network reach is the
per-app loopback port accepts nftables grants it — it is the one uid besides
`trustyclaw-admin` that may open new connections to app backend ports, and it
has no DNS, no HTTPS, no other loopback reach.

One route, JSON over the socket:

- `POST /call` — `{"method", "path", "body"?}`. The service validates the
  method (`GET/POST/PUT/PATCH/DELETE`), requires the path to sit inside the
  app's **`/agent/` namespace** (operator/UI routes are unreachable through
  this proxy by construction), caps the request at 256 KB and the response at
  1 MB, bounds the call at 30 s and 8 concurrent slots across the host, then
  proxies to the owning app's port with the trusted markers
  `X-TrustyClaw-Agent-App-Proxy: <app_id>`,
  `X-TrustyClaw-Agent-Thread: <app-visible thread id>`. Those headers arrive
  only over the app port, which nftables restricts to the two proxy uids, so
  the app backend can trust them the same way it trusts the admin bridge's
  `X-TrustyClaw-App-Proxy` marker — and can tell the two callers apart.

The app's HTTP status and JSON body are returned to the agent verbatim as
`{"status": ..., "body": ...}`: an app validation error is information the
agent uses to retry within the same turn, not a proxy failure. App-specific
activity history belongs to the app's own schema and product surface; the host
does not store or expose a second ledger of agent-to-app calls.

## The agent-facing tool

The tools MCP shim (`host.runtime.agent_shim.mcp_shim`) always serves one extra
tool, `app_api`. Its stable presence grants no authority and avoids changing
the MCP tool list between ordinary and app-created tasks. There is nothing to
configure and no secret to deliver: the shim is a child of the runtime CLI,
so it sits inside the thread's scope and the service attributes *each call* the
same way it attributes every caller. Ordinary operator threads, status
probes, login sessions, and tasks for apps without `agent.api` receive 404 if
they call it. The current app instructions name the app and define whether
and how to use the tool.

Guidance is split by ownership. The immutable host `AGENTS.md` and `CLAUDE.md`
explain that listing `app_api` grants no access and it is usable only through
routes documented by the current app instructions. The MCP tool description
defines the mechanical method/path/body and status/body contract. The app's
manifest-referenced `agent.md` owns every app-specific route, request shape,
workflow rule, and interaction style. The shim does not enumerate or discover
routes, and the agent is told not to probe for them.

What each app exposes under `/agent/` is the app's own design: Mission Pursuit
(the first consumer, see [mission-pursuit.md](mission-pursuit.md))
serves its action protocol at `POST /agent/actions` plus full artifact and workspace-state
reads, and layers its own authorization on the trusted thread marker: only the
current session's thread while it has an active run is served. The host deliberately does not schema-validate agent routes — the app
backend owns semantic validation exactly as it owns it for its operator UI —
but the transport (namespace, caps, and attribution) is host-enforced.

## What a compromised piece can and cannot do

- **A prompt-injected agent** in app A's task can call exactly app A's
  `/agent/` routes for its own thread, at most 8 calls at a time. It cannot
  reach app B (its cgroup names app A's host-prefixed thread), cannot reach the app's operator routes,
  cannot reach the admin API, and gains no new network reach (the app ports
  still drop the agent uid).
- **A compromised agent-app service** can call installed apps' `/agent/`
  routes with forged markers — the reason it is its own uid with nothing else:
  it holds no secrets, has no database access, and has no egress. It
  cannot touch admin state, tool credentials, or the agent.
- **A malicious app backend** gains nothing new: it still cannot connect to
  the agent-app socket (peer-gated to the agent uid), other apps' ports, or
  the admin API beyond its existing allowlist.

Two residual notes, stated honestly. First, same-uid agent processes share a
writable home, so one could in principle plant files another CLI later
executes and thereby run *inside* the victim's scope; that is a pre-existing
property of the shared agent user, unchanged by this feature, and the
eventual fix (per-app agent users) slots underneath this design without
changing the protocol — attribution simply gains a uid check. Second, the
pidfd liveness probe closes the pid-recycle race, but attribution quality is
bounded by the kernel facts it reads; anything that could legitimately start
a process inside a thread's scope shares that thread's app authority by design.

## How a turn changes for an app

Nothing about task dispatch changes: the app backend still composes
`input_message`, creates the task over its admin socket, and reads
`output_message` when the task completes. What changes is what the agent can
do mid-turn: read app state when it needs it (no continuation-turn budgets,
no digest squeezed into the task input limit), write incrementally, and see
each write's validation verdict immediately. An app migrating from fenced
action blocks can serve the same actions as `/agent/` routes and keep its
feed journaling server-side; the output-message parser then becomes dead code
to remove rather than a boundary to maintain.

## Testing

`tests/test_agent_app_api.py` covers the scope parser (including forged
user-manager scopes and the pidfd liveness path), prefix and manifest
resolution (fail-closed for operator threads and non-opted-in apps), the socket
service end-to-end against a stub app backend (trusted markers, namespace
enforcement, status passthrough, 502s, peer gating, and the concurrency cap),
and the MCP shim as a real subprocess (stable listing, call forwarding, and
rejection surfacing). `tests/test_orchestrator.py` verifies every provider
turn receives its host thread id, and `tests/test_deploy.py` pins the service
unit, uid, nftables reachability, absence of database access, and launch
helpers' `--thread-scope` handling.
