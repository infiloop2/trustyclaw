# Audit: Agent Isolation From Host and Operator Data

Finding ID prefix: `ISO`. See [README.md](README.md) for the sweep process,
entry template, and severity scale.

## Audit question

Can the agent (`trustyclaw-agent` processes, or code the agent writes and
runs) access any data belonging to other Unix users on the host — admin
state, proxy policy and CA material, operator credentials, Cloudflare tunnel
token — or escalate its privileges through any implementation bug, sudo
helper flaw, file permission mistake, or open socket?

## Threat model

- **Adversary:** the agent itself — fully malicious code running as
  `trustyclaw-agent`, in auto-approve mode, with arbitrary local compute and
  the ability to write and execute programs. Assume the AI model is
  adversarial or prompt-injected.
- **Assets:** admin database contents (tasks, policy, account metadata),
  proxy CA private key and TLS material, admin password hash, provider OAuth
  tokens beyond the agent's own, `/etc/trustyclaw/cloudflared.token`,
  operator SSH environment, and root itself.
- **Trust boundaries:** Unix user separation between `trustyclaw-agent`,
  `trustyclaw-admin`, `trustyclaw-proxy`, `cloudflared`, `postgres`, and
  root; the six root-owned sudo helpers in
  `/usr/local/lib/trustyclaw-host/`; Postgres peer authentication on the
  Unix socket; nftables uid-based rules blocking the agent from the admin
  API port and all direct egress.
- **In scope:** everything TrustyClaw configures or ships — bootstrap file
  modes and ownership (`host/bootstrap/`), sudoers entries, helper
  implementations (`host/bootstrap/helpers/`), `read-agent-file` path
  confinement, the admin API's handling of agent-produced content, sockets
  reachable from the agent uid, systemd unit sandboxing, environment leakage
  into agent processes.
- **Out of scope:** Ubuntu/kernel/setuid vulnerabilities in stock OS
  packages, EC2/hypervisor escape, and physical access. (A TrustyClaw choice
  that *widens* exposure to such a bug — e.g. leaving an unnecessary
  privileged socket reachable — is in scope.)

## Scope checklist

This checklist is not comprehensive: it names known-important areas, but the
audit question and threat model define the scope. Account for each item in
your coverage section, and report anything else within scope even if no item
below names it.

1. Every sudoers grant and every helper binary/script: argument handling,
   path confinement, symlink and TOCTOU races, environment sanitization.
2. File ownership and modes for secrets and state created by bootstrap and at
   runtime (CA key, tunnel token, database directory, config files).
3. Sockets and abstract endpoints reachable as `trustyclaw-agent`: Postgres
   socket peer auth, proxy port, admin API port block, systemd/dbus surfaces.
4. Data flows carrying agent-controlled content into privileged contexts:
   task output through the admin API and DB, file names/paths through
   `read-agent-file`, headers through the proxy.
5. Anything the agent's environment inherits from the launch helpers
   (variables, file descriptors, cwd).

## Key code and docs

- `docs/architecture/privilege-boundaries.md`, `docs/architecture/filesystem.md`
- `host/bootstrap/bootstrap.sh`, `host/bootstrap/helpers/`
- `host/runtime/admin_api.py` (agent file routes, process listing),
  `host/runtime/orchestrator.py`, `host/runtime/pgclient.py`
- systemd units and nftables rules as written by bootstrap

## Audit entries

## 2026-07-04 — Claude Opus 4.8 — `f28b50e`

Reviewer: Claude Opus 4.8 (claude-opus-4-8)
Commit: `f28b50e`
Methodology: static code reading of the bootstrap, the six sudo helpers, the
admin API's agent-facing routes, and the nftables/pg_hba configuration.
Reasoned about filesystem modes and socket reachability from the bootstrap
source; did not run a live host or write exploit code.

### What was reviewed

- `host/bootstrap/bootstrap.sh`: service-user creation (stable uids), durable
  volume ownership/mode fixups, the `/etc/sudoers.d/trustyclaw-host` grant,
  the nftables ruleset, Postgres `pg_hba.conf`/`postgresql.conf`, the proxy CA
  key/tunnel-token/agent-home modes, and the snapd masking.
- All six helpers in `host/bootstrap/helpers/`: `read-agent-file.sh`,
  `run-codex-app-server.sh`, `run-claude-code.sh`, `read-codex-account-id.sh`,
  `read-claude-account.sh`, `reboot-host.sh`.
- Agent-controlled content flowing into the admin service: the
  `read-agent-file` list/read routes and their argument handling
  (`host/runtime/admin_api.py` `_run_agent_file_helper`, `_agent_file_path`),
  and the `agent-processes` `/proc` reader.
- The environment the run-helpers hand to agent runtimes.

### Findings

No agent isolation or privilege-escalation findings at this commit.

Basis for the negative result:

- The agent (`trustyclaw-agent`) has no sudo entry; the sudoers grant is
  scoped to `trustyclaw-admin` and six fixed absolute helper paths with no
  wildcards. Every helper either demotes to `trustyclaw-agent` via `runuser`
  before doing work (so an admin-to-agent read/launch is a downward crossing
  that hands the agent nothing new) or is a fixed command (`systemctl
  reboot`). Executed directly by the agent the helpers fail, because
  `runuser`/`systemctl reboot` need privileges the agent lacks.
- `read-agent-file.sh` opens each path component with `O_NOFOLLOW`/
  `O_DIRECTORY` under a directory fd, rejects symlinks and `..`, opens files
  `O_NONBLOCK` and re-checks `S_ISREG`, and caps listing/scan/read work — and
  it runs as the agent, so even a confinement slip could only read what the
  agent already can.
- Secret files are unreadable by the agent: proxy CA key `600 trustyclaw-proxy`
  under `proxy-state` (`700`), tunnel token `640 root:cloudflared` under
  `/etc/trustyclaw` (`0750`), admin-home/admin-state/agent-home each `700`,
  pgdata `700 postgres` under a `711 root` parent.
- nftables drops the agent's traffic to every loopback port except the proxy
  port (so the admin API on `127.0.0.1:7443` is unreachable) and drops all
  non-root DNS including to the local stub; the agent has no direct egress.

The one cross-user endpoint the agent can still reach at the socket layer is
the Postgres Unix socket (nftables filters IP, not `AF_UNIX`). Admin-state
confidentiality/integrity holds regardless, because `pg_hba.conf` grants roles
only to `trustyclaw-admin`/`trustyclaw-proxy`/`postgres` and rejects everyone
else under peer auth, so the agent cannot read or write any table. Socket
*reachability* is not an isolation break, but it is a denial-of-service vector
— see `REL-1` in [05-reliability.md](05-reliability.md).

### Coverage and confidence

- Sudoers/helpers (checklist 1): all six helpers and the single sudoers line
  reviewed line by line; argument handling for `run-claude-code`'s `"$@"` is
  safe because the process runs as the agent regardless of arguments.
- File modes (checklist 2): traced every `chown`/`chmod`/`install` in
  bootstrap for the CA key, tunnel token, pgdata, and the three service homes;
  all deny `other`. Not independently verified against a running host's
  actual inode modes.
- Sockets (checklist 3): Postgres socket peer-auth reject confirmed in
  `pg_hba.conf`; agent→admin-API and agent→DNS drops confirmed in the nftables
  output chain; snapd masked. I did **not** enumerate every systemd/DBus
  endpoint the agent uid can reach beyond noting the container runtime is
  absent and snapd is masked — a dedicated review of the agent's reachable
  `/run` sockets would strengthen this.
- Data flows (checklist 4): agent file names/paths reach the admin service
  only through `read-agent-file` (path confined) and are rendered safely in
  the UI (see [03-security-admin-ui.md](03-security-admin-ui.md)).
- Environment (checklist 5): run-helpers set explicit `HOME`/proxy vars via
  `env`; sudo's `env_reset` plus `runuser` bound what the agent inherits, and
  no admin secret is passed as an argument or environment value to the agent
  runtimes.
- Out of scope, untested: kernel/setuid local-privilege-escalation and EC2
  metadata credential theft (the latter depends on IMDS configuration set at
  instance launch, outside this layer).
## 2026-07-04 — GPT-5.5 — `f28b50e87b61`

Reviewer: GPT-5.5 (gpt-5.5)
Commit: `f28b50e87b61507db372d288d971487f55cb2121`
Methodology: static code reading and grep sweeps. I reviewed bootstrap-created
users, file modes, sudoers grants, helper scripts, admin routes that cross into
agent-owned data, Postgres peer-auth configuration, nftables loopback rules, and
runtime launch environments. I did not run a live host or exploit PoCs.

### What was reviewed

- `host/bootstrap/bootstrap.sh`: stable service users, managed-path symlink
  cleanup, Postgres `pg_hba.conf`, cloudflared token ownership, proxy CA
  ownership, sudoers, nftables, systemd units, and agent slice setup.
- `host/bootstrap/helpers/*.sh`: runtime launch helpers, account readers,
  `read-agent-file`, and `reboot-host`.
- `host/runtime/admin_api.py`: `/v1/agent-files*`, `/v1/agent-processes`,
  reboot helper invocation, auth, static serving, and route dispatch.
- `host/runtime/state.py`, `host/runtime/db.py`, `host/runtime/pgclient.py`,
  and `host/migrations/0001_admin_state_schema.sql`: database roles, peer auth,
  proxy grants, event writes, provider pins, and certificate file paths.
- `host/runtime/orchestrator.py`, `host/runtime/codex_app_server.py`, and
  `host/runtime/claude_code.py`: launch environment, task process ownership,
  runtime status/account metadata reads, and process shutdown paths.
- `docs/architecture/privilege-boundaries.md`,
  `docs/architecture/filesystem.md`, and
  `docs/architecture/admin-state-storage.md` for doc/code drift.

### Findings

| ID | Status | Severity | Location | Summary |
| --- | --- | --- | --- | --- |
| ISO-001 | Open | Info | `docs/architecture/filesystem.md:26` | The filesystem table says `/usr/local/lib/trustyclaw-host/*` includes policy-update, proxy-state-read, and provider-pin-sync helpers, but the actual sudoers allowlist installs only `reboot-host`, runtime launch helpers, account readers, and `read-agent-file` (`host/bootstrap/bootstrap.sh:673`). This does not weaken isolation; it overstates privileged helper surface and can send future reviewers toward non-existent code. Update the docs to match the six installed helpers. |

No isolation-breaking defects were found in this sweep.

### Coverage and confidence

For sudoers and helpers, I checked the single sudoers entry, every helper
script, argument handling, demotion with `runuser`, root-only ownership, and
the `read-agent-file` path algorithm. `read-agent-file` rejects NULs and `..`,
opens each parent directory by file descriptor with `O_NOFOLLOW`, rejects
symlinks, skips disappeared directory entries, lists at most 1000 entries, and
reads at most 1 MiB as `trustyclaw-agent`.

For file ownership and modes, I checked bootstrap's managed-path `lstat`
cleanup, root-owned runtime code, admin/proxy/agent volume roots, Postgres data
directory ownership, proxy CA key mode, Cloudflare token mode/group, sudoers
mode, and system trust-store CA copy. The only writable durable agent path is
the agent home; standard world-writable temporary directories remain ordinary
OS scratch space and are not trusted inputs.

For sockets and endpoints, I checked Postgres Unix-socket-only configuration,
peer `pg_hba.conf` rules, absence of an agent database role, the proxy's narrow
database grants, nftables rules that let `trustyclaw-agent` reach only the
loopback proxy port and drop other loopback/direct egress, and the admin API's
loopback bind.

For agent-controlled data flows, I checked task output/event rendering paths
only insofar as they enter privileged host code, proxy event writes, process
listing under the agent cgroup, and file names/paths/content returned by the
file helper. I did not perform a live permission probe as `trustyclaw-agent`, so
confidence is highest for shipped bootstrap/runtime code and lower for
distribution-specific defaults outside the scripts.
