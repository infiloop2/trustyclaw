# Filesystem Layout

The host uses three EBS volumes: the EC2 root volume plus two durable data
volumes. Runtime code uses these mounted paths directly. The durable data
volume mount roots are root-owned and mode 711, so service users can traverse
to their own private subtrees without being able to list or rewrite the mount
root.

## Root volume

The root volume is replaceable on redeploy. It holds the OS, installed tools,
root-owned runtime code, service definitions, firewall config, logs, and swap.
Root owns every executable or configuration file that defines the trust
boundary. Service users are not granted write access to any of these root-volume
paths. Most are intentionally readable or executable by service users because
the services must import Python code, run CLI binaries, execute fixed helpers
through sudo, or trust the public proxy CA. The security boundary is that
service users cannot write, replace, or rename these trusted files. The main
exception to broad readability is sudoers config, which is root-readable only.
This is not a claim that service users cannot write anywhere under `/`: normal
Unix-writable temporary locations such as `/tmp`, `/var/tmp`, and `/dev/shm`
may still be writable, and each service has its own mounted state/home
directory below. Those writable locations are not trusted code or policy inputs.

| Path | Access | Contents |
| --- | --- | --- |
| `/opt/trustyclaw-host` | root-owned, `a+rX`, not service-writable | Host runtime Python package imported by the services. |
| `/usr/local/bin`, `/usr/local/lib/node_modules` | root-owned, readable/executable, not service-writable | Node.js, Codex CLI, and Claude Code CLI. |
| `/usr/local/lib/trustyclaw-host/*` | root-owned, `755`, not service-writable | Fixed sudo helpers for runtime launch, account reads, auth clearing, agent-home file reads, reboot, GitHub App token minting/audits, and `.github` push approval. |
| `/etc/sudoers.d/trustyclaw-host` | root-owned, `440`, not service-writable | Exact helper allowlist for `trustyclaw-admin`. |
| `/etc/systemd/system/trustyclaw*` | root-owned system config, not service-writable | Postgres, admin API, network proxy, tools, installed app, optional Cloudflare Tunnel service units, and the agent/app slice definitions. |
| `/etc/trustyclaw/cloudflared.token` | root-owned, `0640`, group `cloudflared` | Cloudflare Tunnel token for the optional `cloudflared` service. Directly readable only by root and `cloudflared`; the SSH operator can deliberately cross that boundary with unrestricted sudo. |
| `/etc/trustyclaw/cloudflare_hostname` | root-owned, `644` | Configured Cloudflare Access hostname used for bootstrap verification and operator diagnostics. |
| `/etc/nftables.conf` | root-owned system config, not service-writable | Host firewall rules. |
| `/etc/codex/requirements.toml`, `/etc/codex/managed_config.toml` | root-owned, `644`, not service-writable | Managed Codex policy restricting web search and connector surfaces, plus the bundled-tools MCP server definition. |
| `/usr/local/share/ca-certificates/trustyclaw-network-proxy.crt` | root-owned, `644`, public certificate | Public proxy CA certificate installed in the system trust store for agent runtimes. |
| `/swapfile` | root only | 6 GiB swapfile. |

Mutable admin state, proxy state, and agent home data do not live in these
root-owned code paths. They live on the admin and agent data volumes below, with
separate Unix owners.

## Admin volume

The admin volume is mounted at `/mnt/trustyclaw-admin`, owned by `root`, and
mode 711 so service users can only traverse it. Mutable database-backed host,
network, app, and tool state lives in the local Postgres database whose data
directory sits on this volume under `postgres/`, owned by the `postgres` user (see
[Admin state storage](admin-state-storage.md)). The network policy, provider
account pins, and network events are database tables too (the proxy holds a
narrow database role over its enforcement inputs and outputs). The proxy-owned
`proxy-state/` directory, mode 700, keeps the state whose consumers require
files: the CA keypair, minted leaf certificates, and Git quarantine mirrors.
The `ssl`, OpenSSL, and Git interfaces consume those paths, and the CA private
key stays out of the admin-owned schema. Bootstrap sanitizes managed paths
before writing them on redeploy:
every directory it creates and every file slot it writes as root (including
the Postgres data-directory path components and the managed
`postgresql.conf`/`pg_hba.conf` inside it) is checked with `lstat` and any
planted symlink removed, so a compromised previous service process cannot
redirect a future root bootstrap's writes. The volume is preserved across
redeploys.

| Path | Access | Contents |
| --- | --- | --- |
| `/mnt/trustyclaw-admin/postgres/<major>/main/` | postgres user only | Postgres data directory: host config and operator endpoints, task/session state, app and tool state, audit logs, network policy/pins/events, and encrypted GitHub, Cloudflare, and tool credentials. |
| `/mnt/trustyclaw-admin/admin-state/version.json` | admin only | Authoritative admin disk version, read by bootstrap before the database is up to enforce the deploy/upgrade/recover policy. |
| `/mnt/trustyclaw-admin/proxy-state/network_proxy_ca.key` | proxy only | Proxy CA private key. |
| `/mnt/trustyclaw-admin/proxy-state/network_proxy_ca.crt` | mode 644, but behind proxy-state traversal controls | Proxy CA certificate copied into the system trust store for agent/runtime use. |
| `/mnt/trustyclaw-admin/proxy-state/generated-certs/` | proxy only | Per-host leaf certificates minted by the proxy. |
| `/mnt/trustyclaw-admin/proxy-state/github-quarantine/` | proxy only | Bare per-repository Git mirrors and `refs/pending/...` objects for `.github` pushes held for operator approval. |
| `/mnt/trustyclaw-admin/tools-state/assets/` | tools only | Bounded temporary image/video copies for tool calls. Cleared on tools-service start; expired files are swept hourly. |
| `/mnt/trustyclaw-admin/admin-home/` | admin only | Admin service home directory. |

## Agent volume

The agent volume is mounted at `/mnt/trustyclaw-agent`, with the agent home at
`/mnt/trustyclaw-agent/agent-home`. It is preserved across redeploys. This is
the agent user's only durable writable storage: runtime auth, session files,
workspace state, and user-installed non-root tools should land here because the
runtime helpers set `HOME` and the working directory to this path. Bootstrap
also maintains a few root-owned, readable, immutable runtime files here:
`AGENTS.md`, `CLAUDE.md`, `.codex/config.toml`, and `.claude/settings.json`.

That does not mean the agent has literally no other writable path. As a normal
unprivileged Linux user it can still write to standard ephemeral locations such
as `/tmp`, `/var/tmp`, and `/dev/shm` if the base OS exposes them with the usual
world-writable sticky permissions. Those paths live on the replaceable root
volume, are not treated as TrustyClaw state, are not preserved as part of the
agent volume contract, and should not contain secrets or important workspace
data. The agent cannot write root-owned code, service files, admin state, proxy
state, global CLI installs, or the proxy CA/private-key material without a local
privilege escalation.

| Path | Access | Contents |
| --- | --- | --- |
| `/mnt/trustyclaw-agent/agent-home/` | agent writable, with selected immutable root-owned files | Agent runtime auth, provider session files, CLI caches, workspace data, and the small bootstrap-managed instruction/config files installed after symlink slots are sanitized. |
| `/mnt/trustyclaw-agent/agent-home/user-files/` | agent writable | Durable operator uploads. Stored names begin with a UTC upload timestamp; uploads publish atomically and are not pruned automatically. |
