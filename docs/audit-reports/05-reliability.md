# Audit: Reliability, Resource Isolation, and Recovery

Finding ID prefix: `REL`. See [README.md](README.md) for the sweep process,
entry template, and severity scale.

## Audit question

Four named sub-questions. A sweep's coverage section must address each one
explicitly (or state that it was skipped):

- **REL-A — Service starvation.** Can the agent, by consuming CPU, memory,
  swap, PIDs, file descriptors, disk, or I/O bandwidth, starve the non-agent
  services (admin API, proxy, Postgres, cloudflared, sshd) to the point that
  the operator loses control?
- **REL-B — Admin API availability.** Can the admin API deadlock or get
  stuck so that operator requests hang — lock ordering, worker-pool
  exhaustion, slow sudo helpers or runtime calls holding state locks,
  unbounded waits on the database or on agent processes?
- **REL-C — Recovery by reboot.** Does a reboot reliably return the host to
  an operator-controllable state from any plausible bad state? Do all
  services come back in the right order with fail-closed network rules, and
  does the `reboot-host` path itself work when the box is degraded?
- **REL-D — Bounded database growth.** Is every growth path in the admin
  database and on-disk state bounded? Event/task history pruning, WAL and
  vacuum behavior, log files, proxy certificate cache, agent-home growth
  affecting other services' disk.

## Threat model

- **Adversary/hazard:** a runaway or malicious agent (fork bombs, memory
  hogs, disk fills inside its cgroup and home), plus ordinary bad luck —
  crashes mid-transaction, database outages, full disks, unclean shutdowns.
  Assume no root compromise (that is axis 01).
- **Assets:** the operator's ability to observe and control the host at all
  times, and the host's ability to run indefinitely without manual disk or
  database surgery.
- **In scope:** `trustyclaw_agent.slice` limits (`CPUWeight`, `MemoryHigh`/
  `MemoryMax`, `MemorySwapMax`, `TasksMax`) and what they do *not* cover
  (disk, file descriptors, I/O); volume layout separating agent storage from
  service storage; admin API threading (state lock discipline, six workers /
  three-per-runtime claim caps), Postgres via the
  in-repo `pgclient.py` (timeouts, reconnects, transaction hygiene); systemd
  unit ordering, restart policies, and watchdogs; the maintenance thread's
  pruning of the 1,000,000-row event caps; boot-time nftables ordering.
- **Out of scope:** AWS infrastructure failures; performance tuning where
  nothing is starved or stuck; provider API outages except where they wedge
  the admin API.

## Scope checklist

This checklist is not comprehensive: it names known-important areas, but the
audit question and threat model define the scope. Account for each item in
your coverage section, and report anything else within scope even if no item
below names it.

1. Enumerate agent-consumable resources one by one (CPU, memory, swap, PIDs,
   fds, disk space, inodes, I/O bandwidth, loopback connections to the proxy
   and admin ports) and identify the mechanism bounding each — or report the
   absence.
2. Map every lock in `admin_api.py`/`orchestrator.py`/`state.py` and every
   blocking call made while holding one; check worker-pool sizing against
   slow paths.
3. Trace reboot end to end: helper, systemd ordering, fail-closed nftables
   before proxy start, service restart policies, recovery with a full disk
   or corrupt policy row.
4. List every table and file that grows; verify each has a pruning or
   bounding mechanism that actually runs (and survives service restarts).
5. Check behavior at the bounds: what happens at `MemoryMax`, `TasksMax`,
   event-cap pruning, 128 MiB body limit — degraded-but-controllable, or
   wedged?

## Key code and docs

- `docs/architecture/services-and-runtimes.md`,
  `docs/architecture/admin-state-storage.md`,
  `docs/architecture/filesystem.md`, `docs/architecture/admin-api.md`
- `host/runtime/admin_api.py`, `host/runtime/orchestrator.py`,
  `host/runtime/state.py`, `host/runtime/db.py`, `host/runtime/pgclient.py`
- systemd units, slice definitions, and volume setup in `host/bootstrap/`

## Audit entries

## 2026-07-04 — Claude Opus 4.8 — `f28b50e`

Reviewer: Claude Opus 4.8 (claude-opus-4-8)
Commit: `f28b50e`
Methodology: static reading of the cgroup/slice unit, the admin-API threading
and lock discipline, the DB connection layer and wire client, the orchestrator
worker pool, and the state pruning/caps. The four findings below are reasoned
from the code and standard PostgreSQL/systemd defaults; none were reproduced on
a live host.

### What was reviewed

- `host/bootstrap/bootstrap.sh`: `trustyclaw_agent.slice`
  (`CPUWeight`/`MemoryHigh`/`MemoryMax`/`MemorySwapMax`/`TasksMax`), the
  network-proxy/admin-api/postgres units and restart policies, the volume
  layout, `postgresql.conf` (`max_connections=50`), and `pg_hba.conf`.
- `host/runtime/admin_api.py` locks (mutation lock usage, `NETWORK_POLICY_LOCK`,
  `OAUTH_LOGIN_LOCK`), helper timeouts, the maintenance loop, and the
  queue/steer caps.
- `host/runtime/state.py` (`mutation()` RLock, event caps + amortized prune,
  task/thread-session pruning), `host/runtime/db.py` (pool, `MAX_ACTIVE_CONNECTIONS`,
  checkout timeout), `host/runtime/pgclient.py` (socket handling),
  `host/runtime/orchestrator.py` (worker pool, claim caps, lock ordering).
- `host/runtime/network_proxy.py` for proxy-side resource bounds.

### Findings

| ID | Status | Severity | Location | Summary |
| --- | --- | --- | --- | --- |
| REL-1 | Open | High | `host/bootstrap/bootstrap.sh:396,443` (postgres unit/conf) | The Postgres Unix socket in `/var/run/postgresql` is reachable by the agent uid (nftables filters IP, not `AF_UNIX`; default `unix_socket_permissions=0777`, `RuntimeDirectory` mode `0755`), and `pg_hba` only *rejects* the agent after a backend is forked. With `max_connections=50` and the default `authentication_timeout=60s`, a malicious agent that opens ~50 socket connections and stalls before the startup packet holds every backend slot for 60s at a time, so the admin service and proxy get `53300` "too many connections". The proxy fails closed (safe), but the admin API's operator requests error out for as long as the agent sustains the flood — the operator loses control with no special policy needed. Restrict the socket dir/group (e.g. `unix_socket_group` + `0770` to a group excluding the agent) or lower `authentication_timeout`. |
| REL-2 | Open | High | `host/runtime/network_proxy.py:362`, `host/runtime/state.py:112` | Under any wildcard allow rule (a first-class, documented feature — the manual-domain field literally suggests `*.example.com`), `ensure_host_cert` mints and stores a per-SNI-host keypair (`.crt`+`.key`+`.csr`+`.ext`, two `openssl` runs) with no eviction or cap, in `proxy-state/generated-certs` on the **admin** volume that also holds Postgres. The agent, by connecting to unbounded distinct sub-domains matching the wildcard, grows this directory without limit until the admin volume fills; Postgres writes then stall (see REL-3), wedging the admin API. The cache is only cleared on redeploy, not reboot. Cap/evict the cert cache, or move it off the admin volume. |
| REL-3 | Open | Medium | `host/runtime/pgclient.py:139`, `host/runtime/db.py:164` | The wire client opens the `AF_UNIX` socket with no `settimeout()` and issues blocking `recv`s, and no `statement_timeout` is set on connections. Connection *checkout* is bounded (10s semaphore), but once a transaction is in flight a stalled Postgres (from REL-1, a full admin volume per REL-2, or otherwise) blocks the thread indefinitely. Because `state.mutation()` holds a process-wide RLock across its transaction, one stuck write freezes *all* admin-state mutations — policy replace, task cancel/kill — so the operator cannot regain control until Postgres recovers. Set a socket timeout and a `statement_timeout` so a wedged query fails instead of holding the mutation lock. |
| REL-4 | Open | Medium | `host/runtime/orchestrator.py:390`, `host/runtime/state.py:606` | Agent-streamed output is stored via `record_agent_event("task.message", …, {"message": message})` with no per-row size cap, unlike `network_events` whose fields are explicitly truncated (`host[:512]`, `path[:2048]`, …). The 1,000,000-row retention bounds the event *count* but not per-row size, so an agent emitting very large messages can grow `agent_events` on the admin volume far beyond the intuitive bound (feeding the same volume-fill path as REL-2). Cap the stored message length. |
| REL-5 | Open | Medium | `host/runtime/network_proxy.py:55,56` | `MAX_CONNECTIONS=64` handlers each buffer up to `MAX_BODY_BYTES=128 MiB` for inspection (and each web-search-inspected request may additionally hold a 128 MiB decoded body), i.e. up to ~8 GiB resident, yet the proxy service has no `MemoryMax` (only the *agent* slice is memory-limited). On a small instance the agent can OOM the proxy — its own sole egress path — by driving concurrent large POSTs. Put the proxy in a memory-limited cgroup or lower the body/connection product. |

### Coverage and confidence

- **REL-A (starvation):** CPU/memory/PIDs are bounded for agent runtimes by
  `trustyclaw_agent.slice` (`CPUWeight=50`, `MemoryHigh/Max`, `MemorySwapMax`,
  `TasksMax=4096`) and agent-home is on a separate volume, so those are sound.
  The gaps are the resources the slice does not cover and the services that
  are *not* in it: the Postgres connection budget (REL-1), the admin-volume
  disk shared with Postgres (REL-2/REL-4), and the un-capped proxy service
  memory (REL-5).
- **REL-B (admin API stuck):** lock discipline is otherwise good — mutation
  lock, `NETWORK_POLICY_LOCK`/`OAUTH_LOGIN_LOCK` (both 5s-timeout → 409), and
  the orchestrator's documented mutation→`_POOL_LOCK` ordering are acyclic, and
  slow work (runtime spawn/turn/close, helper subprocesses) runs outside all
  locks. The single systemic weakness is the missing DB timeout (REL-3), which
  turns any Postgres stall into an indefinite mutation-lock hold. Helper
  subprocess calls are timeout-guarded (10s).
- **REL-C (reboot recovery):** reviewed by reading only. `initialize_state`
  fails orphaned running tasks on restart; every service is `Restart=always`
  with `StartLimitIntervalSec=0`; nftables is enabled so egress stays
  fail-closed before the proxy comes up; connections are ping-verified on
  checkout so a Postgres restart costs a reconnect. I did **not** test a real
  reboot, a reboot with a full admin volume, or the `reboot-host` path under a
  degraded box — worth a live drill.
- **REL-D (bounded growth):** every admin-state table has a cap with an
  amortized range-delete prune (agent_events/network_events 1M rows,
  finished tasks 100k, thread sessions 100k/runtime, idempotency 10k). Growth
  is bounded in *row count*; the exceptions are per-row size (REL-4) and the
  on-disk cert cache (REL-2), which are the real disk risks. I did not compute
  the worst-case total DB size against the admin volume's provisioned size —
  the caps permit tens of GB, so volume sizing deserves its own check.
- Not done: no live load test, fork-bomb/OOM drill, socket-exhaustion
  reproduction, or reboot drill. The findings are reasoned from the code and
  documented PostgreSQL/systemd defaults; REL-1/REL-2 in particular should be
  confirmed on a running host.
## 2026-07-04 — GPT-5.5 — `f28b50e87b61`

Reviewer: GPT-5.5 (gpt-5.5)
Commit: `f28b50e87b61507db372d288d971487f55cb2121`
Methodology: static code reading, grep sweeps, and resource-bound analysis. I
traced cgroup limits, proxy buffering, admin API locks, worker-pool claiming,
database connection pooling, reboot recovery, Postgres storage, event pruning,
and filesystem growth paths. I did not run stress tests or a live host.

### What was reviewed

- `host/bootstrap/bootstrap.sh`: agent slice limits, swapfile, service units,
  restart policies, nftables ordering, Postgres setup, volume ownership, proxy
  CA/cert directories, sudoers, and reboot helper.
- `host/runtime/network_proxy.py` and `host/runtime/network_policy.py`: body
  buffering, connection caps, WebSocket inspection, decompression limits,
  generated certificate cache, DNS/connect behavior, and fail-closed policy
  loads.
- `host/runtime/admin_api.py`: idempotency caps, queued/steer caps,
  maintenance loop, helper subprocess timeouts, reboot endpoint, process/file
  listing bounds, health, and startup recovery.
- `host/runtime/orchestrator.py`: worker counts, per-runtime caps, pool lock
  discipline, task claiming, kill/deactivate behavior, runtime status polling,
  and warm-server lifecycle.
- `host/runtime/state.py`, `host/runtime/db.py`, `host/runtime/pgclient.py`,
  and `host/migrations/0001_admin_state_schema.sql`: table growth paths,
  pruning, transaction lock discipline, connection budgets, proxy grants, and
  Postgres schema/indexes.
- `docs/architecture/services-and-runtimes.md`,
  `docs/architecture/admin-state-storage.md`,
  `docs/architecture/filesystem.md`, and `docs/architecture/admin-api.md`.

### Findings

| ID | Status | Severity | Location | Summary |
| --- | --- | --- | --- | --- |
| REL-001 | Open | High | `host/runtime/network_proxy.py:55` | The proxy can buffer up to 128 MiB per handler and allows 64 concurrent handlers, so a malicious agent can force roughly 8 GiB of request-body buffers in `trustyclaw-proxy`, which is outside `trustyclaw_agent.slice`. Plain HTTP reads the body before policy validation (`host/runtime/network_proxy.py:230`), so even the fail-closed empty policy does not prevent this memory pressure. On the documented small host, this can OOM or swap-storm non-agent services and break operator control. Move cheap host/method/policy denial before body reads where possible, add an aggregate body-memory semaphore well below host reserve, and/or put the proxy service itself under a memory limit that preserves admin/Postgres/sshd. |
| REL-002 | Open | Medium | `host/runtime/network_proxy.py:362` | Per-host MITM certificates are generated into the durable admin volume and are never pruned or capped. With a wildcard rule such as `*.example.com`, the agent can CONNECT to unbounded unique subdomains; each accepted host writes `.key`, `.csr`, `.ext`, and `.crt` files under `proxy-state/generated-certs` (`host/runtime/state.py:112`). Over time this can fill the admin volume that also holds Postgres state. Add an LRU/count/size cap, prune on startup/maintenance, or generate short-lived certs outside durable admin storage. |

### Coverage and confidence

REL-A, service starvation: I enumerated CPU, memory, swap, PIDs, file
descriptors, disk, inodes, I/O bandwidth, and loopback connections. CPU,
memory, swap, and PIDs for runtime processes are bounded by
`trustyclaw_agent.slice`; agent loopback access is limited by nftables to the
proxy port. REL-001 covers proxy memory that the agent can consume outside the
agent cgroup. REL-002 covers admin-volume disk/inode growth through proxy certs.
I did not find explicit I/O bandwidth or fd cgroup controls; no concrete
operator-control failure was proven beyond the proxy memory and cert-cache
paths.

REL-B, admin API availability: I mapped `state.mutation()` and `_POOL_LOCK`
ordering, idempotency lock use, network-policy and OAuth locks with timeouts,
database checkout semaphores/timeouts, helper subprocess timeouts, and worker
claim/release paths. Slow runtime spawns/turns, server closes, helper calls,
and provider status checks are generally outside the state mutation lock.
Worker counts are six total and three per runtime; queued tasks and steer
messages are capped. I did not find a deadlock path in the reviewed code.

REL-C, recovery by reboot: I traced the reboot helper, systemd unit enablement,
service ordering, restart policies, nftables reload, Postgres start, admin API
startup, and `initialize_state()`. Running tasks are failed on startup, queued
tasks remain queued, and runtime statuses are re-derived. Reboot was not tested
on a live instance, so confidence is source-level.

REL-D, bounded database growth: Tasks are capped for queued inputs and pruned
for finished history; steers are capped per task; thread session maps are
pruned; idempotency entries are in-memory with count/time caps; agent and
network events have 1,000,000-row caps and field-size caps for network event
rows. Postgres default WAL/checkpoint/autovacuum behavior was not live-verified.
Durable file growth outside Postgres was checked for proxy CA/cert files,
agent-home, root logs, and swap. REL-002 covers the unbounded generated cert
cache; agent-home growth is isolated to the separate agent volume.
