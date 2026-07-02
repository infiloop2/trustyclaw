# Services and Runtimes

| systemd unit | User | Purpose |
| --- | --- | --- |
| `trustyclaw-network-proxy.service` | `trustyclaw-proxy` | Policy proxy on `127.0.0.1:7445`. |
| `trustyclaw-postgres.service` | `postgres` | Admin-state PostgreSQL, Unix socket only (no TCP listener). |
| `trustyclaw-admin-api.service` | `trustyclaw-admin` | Admin API on `127.0.0.1:7443`. |
| `trustyclaw-cloudflared.service` | `cloudflared` | Optional Cloudflare Tunnel connector for Cloudflare Access operator endpoints. Installed only when `operator_connections` contains `cloudflare_access`. |
| `trustyclaw_agent.slice` | — | Top-level cgroup slice holding every agent runtime scope (underscore, not dash: dashes in slice names encode nesting, and the weight must compare against `system.slice` directly). `CPUWeight=50` guarantees the host services CPU time under contention while leaving idle cores to the agent; `MemoryHigh=70%`/`MemoryMax=80%`/`MemorySwapMax=5G` contain a runaway agent's RAM and swap to its own cgroup; `TasksMax=4096` stops a fork bomb from exhausting kernel PIDs. |

## Process Inventory

| Process | User | Started By | Purpose |
| --- | --- | --- | --- |
| `systemd` | root | OS boot | Starts nftables, Postgres, admin API, proxy, and optional Cloudflare Tunnel services. |
| `nftables` | kernel/root configured | bootstrap/systemd | Enforces inbound and per-user outbound network policy. |
| `trustyclaw-network-proxy.service` | `trustyclaw-proxy` | systemd | Handles all agent HTTP(S)/WS(S) egress and writes network events. |
| `trustyclaw-postgres.service` | `postgres` | systemd | Stores admin state; local Unix-socket connections only. |
| `trustyclaw-admin-api.service` | `trustyclaw-admin` | systemd | Serves localhost API/UI, owns task state, and supervises runtime work. |
| `trustyclaw-cloudflared.service` | `cloudflared` | systemd | Optional Cloudflare Tunnel connector. Reads `/etc/trustyclaw/cloudflared.token` and exposes the admin API through the configured Cloudflare Access hostname. |
| `run-codex-app-server` helper | starts as root, then `trustyclaw-agent` | admin API via sudo | Starts one Codex stdio app-server process. |
| `codex app-server` | `trustyclaw-agent` | launch helper | Executes Codex turns for one active/warm thread. |
| `run-claude-code` helper | starts as root, then `trustyclaw-agent` | admin API via sudo | Starts one Claude Code CLI process. |
| `claude` | `trustyclaw-agent` | launch helper | Executes one Claude Code turn, then exits. |
| `read-codex-account-id` / `read-claude-account` | starts as root, then `trustyclaw-agent` | admin API via sudo | Reads provider auth files narrowly and prints only account guard metadata. |
| `reboot-host` helper | root | admin API via sudo | Requests a host reboot. |

## Thread Inventory

| Thread Group | Process | Purpose |
| --- | --- | --- |
| HTTP handler threads | admin API | One per concurrent API request. Mutations use state transactions and run slow helper calls outside the state lock. |
| Maintenance thread | admin API | Periodically prunes bounded state and event history. |
| Runtime status poller | admin API/orchestrator | Rechecks provider login state and updates each runtime status. |
| Task worker threads | admin API/orchestrator | Six total workers claim queued tasks; at most three tasks run per runtime. |
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
checks and login flows use short-lived servers, while task turns run on
per-thread servers that are kept warm between tasks (at most one per worker
slot) so chat-style follow-ups skip the app-server boot. Claude Code does not
expose the same app-server protocol, so the host runs one CLI process per turn
and resumes the Claude session id recorded for the user thread. Both runtimes
persist login/session state under `agent-home`, so restarted admin services can
re-derive active status from the agent user's home directory.

## Reboot and restart

The admin API, proxy, Postgres, nftables, and optional Cloudflare Tunnel
service are `systemctl enable`d, so they resume on every boot. The proxy and
Postgres start before the admin API, `cloudflared` starts after the admin API
when configured, and nftables reloads `/etc/nftables.conf`. Because admin
state and agent home data live on the two data EBS volumes, a reboot —
including via `POST /v1/host-runtime/reboot` — preserves them: the proxy comes
back immediately enforcing the last active policy (no fail-open window),
Cloudflare Tunnel reconnects when configured, agent login and queued tasks
survive, and the swapfile is re-enabled from `/etc/fstab`. Redeploys can
replace the root volume and runtime code while reattaching the tagged admin
and agent volumes for the same `agent_name`.

On start the admin API runs a recovery pass: a task that was `running` when the
host went down is marked `failed` (an in-flight agent turn cannot survive a
reboot), while `queued` tasks stay queued and the worker resumes them once the
task's chosen runtime is `active`.
