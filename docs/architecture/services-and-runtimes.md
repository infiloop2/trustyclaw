# Services and Runtimes

| systemd unit | User | Purpose |
| --- | --- | --- |
| `trustyclaw-network-proxy.service` | `trustyclaw-proxy` | Policy proxy on `127.0.0.1:7445`. |
| `trustyclaw-postgres.service` | `postgres` | Admin-state PostgreSQL, Unix socket only (no TCP listener). |
| `trustyclaw-admin-api.service` | `trustyclaw-admin` | Admin API on `127.0.0.1:7443`. Owns admin state; holds no internet egress. |
| `trustyclaw-tools.service` | `trustyclaw-tools` | Runs the bundled tool packages and owns the agent-facing tools socket `/run/trustyclaw-tools/tools.sock` (peer-credential authenticated). The only TrustyClaw application service besides the proxy with DNS+HTTPS egress; its Postgres role is scoped to the five tool tables plus read access to the encryption key needed for tool secrets. |
| `trustyclaw-agent-network.service` | `trustyclaw-agent-network` | Serves read-only network integration and denial introspection on `/run/trustyclaw-agent-network/agent-network.sock`. No egress; its Postgres role has SELECT-only policy and network-event grants. |
| `trustyclaw-agent-app.service` | `trustyclaw-agent-app` | Serves the agent-facing app API socket `/run/trustyclaw-agent-app/agent-app.sock` (peer-credential authenticated, agent uid only) and proxies thread-scope-attributed `app_api` calls to app backend ports. No database access or egress. See [agent-app-api.md](apps/agent-app-api.md). |
| `trustyclaw-app-<app_id>.service` | `trustyclaw-app-<app_id>` | Installed app backend on its host-assigned loopback app port, reachable only from the admin API and agent-app service uids. |
| `trustyclaw-cloudflared.service` | `cloudflared` | Optional Cloudflare Tunnel connector for Cloudflare Access operator endpoints. Installed only when `operator_connections` contains `cloudflare_access`. |
| `trustyclaw_agent.slice` | — | Top-level cgroup slice holding every agent runtime scope (underscore, not dash: dashes in slice names encode nesting, and the weight must compare against `system.slice` directly). `CPUWeight=50` guarantees the host services CPU time under contention while leaving idle cores to the agent; `MemoryHigh=70%`/`MemoryMax=80%`/`MemorySwapMax=5G` contain a runaway agent's RAM and swap to its own cgroup; `TasksMax=4096` stops a fork bomb from exhausting kernel PIDs. |
| `trustyclaw_app.slice` | — | Top-level cgroup slice holding installed app services. `CPUWeight=50` gives host services priority under contention while allowing apps to use idle CPU; the current app slice does not impose memory, swap, or task-count caps. |

## Process Inventory

| Process | User | Started By | Purpose |
| --- | --- | --- | --- |
| `systemd` | root | OS boot | Starts nftables, Postgres, proxy, tools, admin API, installed app, and optional Cloudflare Tunnel services. |
| `nftables` | kernel/root configured | bootstrap/systemd | Enforces inbound and per-user outbound network policy. |
| `trustyclaw-network-proxy.service` | `trustyclaw-proxy` | systemd | Handles all agent HTTP(S)/WS(S) egress and writes network events. |
| `trustyclaw-postgres.service` | `postgres` | systemd | Stores admin state; local Unix-socket connections only. |
| `trustyclaw-admin-api.service` | `trustyclaw-admin` | systemd | Serves localhost API/UI, owns task state, and supervises runtime work. |
| `trustyclaw-tools.service` | `trustyclaw-tools` | systemd | Executes bundled tool calls and operator-delegated OAuth/approval work; owns the peer-authenticated tools socket. |
| `trustyclaw-agent-network.service` | `trustyclaw-agent-network` | systemd | Serves the peer-authenticated network-introspection socket from SELECT-only policy and event state, without egress. |
| `trustyclaw-agent-app.service` | `trustyclaw-agent-app` | systemd | Attributes agent `app_api` calls to their app-prefixed thread by cgroup and proxies them to the owning app backend; owns the peer-authenticated agent-app socket. |
| `trustyclaw-app-<app_id>.service` | `trustyclaw-app-<app_id>` | systemd | Serves an installed app API on a loopback app port selected by the host. The admin API and agent-app service are the only uids allowed to open new TCP connections to that listener. |
| `trustyclaw-cloudflared.service` | `cloudflared` | systemd | Optional Cloudflare Tunnel connector. Reads `/etc/trustyclaw/cloudflared.token` and exposes the admin API through the configured Cloudflare Access hostname. |
| `run-codex-app-server` helper | starts as root, then `trustyclaw-agent` | admin API via sudo | Starts one Codex stdio app-server process. |
| `codex app-server` | `trustyclaw-agent` | launch helper | Executes one Codex turn, resuming its provider thread by id, then exits. |
| `run-claude-code` helper | starts as root, then `trustyclaw-agent` | admin API via sudo | Starts one Claude Code CLI process. |
| `claude` | `trustyclaw-agent` | launch helper | Executes one Claude Code turn, then exits. |
| `tools MCP shim` | `trustyclaw-agent` | Codex / Claude Code | Aggregates the tools, network-introspection, and app sockets into one MCP server; one per agent session that uses host tools. |
| `read-codex-account-id` / `read-claude-account` | starts as root, then `trustyclaw-agent` | admin API via sudo | Reads provider auth files narrowly and prints only account guard metadata. |
| `clear-agent-auth` | starts as root, then `trustyclaw-agent` | admin API via sudo | Removes local Codex/Claude auth files during linked-account reset. |
| `read-agent-file` helper | starts as root, then `trustyclaw-agent` | admin API via sudo | Lists agent-home directories, returns a bounded text preview, or streams one bounded regular file to the authenticated Files viewer without giving admin general agent-home access. |
| `mint-github-app-token` helper | root | admin API via sudo | Mints installation-wide GitHub App tokens through root egress because the admin service has none; the proxy repo guard is the per-repository boundary. |
| `audit-github-repo` helper | root | admin API via sudo | Reads GitHub repository/security facts with the working token and returns facts without storing secrets. |
| `approve-github-push` helper | root | admin API via sudo | Replays or cleans up a push held by the `.github` approval gate using the proxy-state quarantine mirror and a working GitHub token piped on stdin. |
| `reboot-host` helper | root | admin API via sudo | Requests a host reboot. |

## Thread Inventory

| Thread Group | Process | Purpose |
| --- | --- | --- |
| HTTP handler threads | admin API | One per concurrent API request. Mutations use state transactions and run slow helper calls outside the state lock. |
| Tools socket handler threads | tools service | One per agent tool call (and per delegated operator operation), bounded by a concurrency cap; tool packages run their third-party requests on these threads. |
| Network-introspection socket handler threads | agent-network service | One per local request, bounded by a concurrency cap; calls perform read-only policy or denial queries. |
| Maintenance thread | admin API | Periodically prunes bounded state and event history. |
| Runtime status poller | admin API/orchestrator | Rechecks provider health; the one Bedrock result is projected into both Pi and Hermes runtime rows. |
| Task worker threads | admin API/orchestrator | Twelve total workers claim queued tasks; at most three tasks run per runtime. Each turn spawns and closes its own runtime process. |
| Proxy handler threads | network proxy | One per proxied connection, capped so buffered request bodies cannot exhaust memory. |
| Proxy certificate lock users | network proxy | Serialize per-host certificate generation so concurrent TLS CONNECTs do not race on cert files. |

Agent runtimes are spawned through fixed sudo helpers that demote them to
`trustyclaw-agent`, each inside a transient systemd scope under
`trustyclaw_agent.slice`. Without the scope they would inherit the admin API's
service cgroup and compete with the host services for resources. The slice's
`CPUWeight=50` versus `system.slice`'s default 100 keeps the admin API, proxy,
and Postgres responsive while an agent build or test run saturates the cores,
and costs the agent nothing when the host services are idle (weights, unlike
quotas, are work-conserving). `MemoryHigh=70%` reclaims agent pages to swap
under pressure and `MemoryMax=80%` OOM-kills inside the agent cgroup, so a
runaway agent process dies instead of triggering a host-wide OOM kill; the
admin API records the failed task and the host stays up. `MemorySwapMax=5G`
keeps 1G of the 6G swapfile available to host services (systemd 249 offers no
percentage form for swap, and bootstrap owns the swapfile size). `TasksMax=4096`
bounds agent threads and processes so a fork bomb cannot exhaust kernel PIDs,
which would otherwise block the admin API from spawning helpers at all. Each
scope is `BindsTo=trustyclaw-admin-api.service`: leaving the admin API's
cgroup must not decouple lifecycles, so when the admin service stops,
restarts, or crashes, systemd stops the scopes too and no orphaned runtime
keeps running after its task was recovered as failed.
Codex runs as stdio app-server child processes; status
checks and login flows use short-lived servers, and each task turn runs on a
fresh app-server that resumes its provider thread by id. The host supplies the
session's selected model on Codex thread start/resume and its model and effort
on every turn. Claude Code does not expose the same app-server protocol, so the
host runs one CLI process per turn
with the selected `--model` and `--effort`, then resumes the Claude session id
recorded for the user thread. Both runtimes persist login/session state under
`agent-home`, so restarted admin services can
re-derive active status from the agent user's home directory.

## Reboot and restart

The admin API, proxy, tools, network-introspection, Postgres, app backends,
nftables, and optional Cloudflare Tunnel service are `systemctl enable`d, so
they resume on every boot. Postgres starts before the proxy, tools, and
network-introspection services; those services start before the admin API; app
backends and `cloudflared` start after the admin API when installed. nftables
reloads `/etc/nftables.conf`.
Because admin state and agent home data live on the two data EBS volumes, a
reboot, including via `POST /v1/host-runtime/reboot`, preserves them: the proxy comes
back immediately enforcing the last active policy (no fail-open window),
Cloudflare Tunnel reconnects when configured, agent login and queued tasks
survive, and the swapfile is re-enabled from `/etc/fstab`. Redeploys can
replace the root volume and runtime code while reattaching the tagged admin
and agent volumes for the same `agent_name`.

On start the admin API runs a recovery pass: a task that was `running` when the
host went down is marked `failed` (an in-flight agent turn cannot survive a
reboot). `queued` tasks survive the reboot, but a task claimed before its
chosen runtime's first status poll publishes `active` fails with that
non-active status: tasks run only against an active runtime, never park
behind one.

The tools service independently marks an approval caught in `approved`
execution as `failed` with an unknown outcome; it never repeats the
third-party side effect after a restart.
