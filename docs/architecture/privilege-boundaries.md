# Users and Privilege Boundaries

| User | Purpose | Privileges |
| --- | --- | --- |
| `trustyclaw-operator` | Human SSH login. | Full passwordless sudo. |
| `trustyclaw-admin` | Runs the admin API; owns admin state (the `trustyclaw_admin` database role). | sudo for exactly nine root helpers (below). |
| `trustyclaw-proxy` | Runs the policy proxy; owns proxy policy/log/CA files. | No sudo, no database role. Only nftables-approved DNS and TCP 80/443 egress. |
| `trustyclaw-agent` | Runs Codex and Claude Code runtime processes. | None. No sudo, no direct network, no database role. |
| `cloudflared` | Runs the optional Cloudflare Tunnel connector. | No sudo, no database role. Only nftables-approved DNS, TCP 443, and TCP/UDP 7844 egress. |
| `postgres` | Runs the admin-state Postgres. | Database superuser over the local socket; no sudo, no network egress. |

The five service accounts use fixed numeric IDs: `trustyclaw-admin` is
`47741`, `trustyclaw-proxy` is `47742`, `trustyclaw-agent` is `47743`,
`cloudflared` is `47744`, and `postgres` is `47745` (created by bootstrap
before the PostgreSQL packages would assign a dynamic id). Stable IDs keep
durable EBS ownership — including the preserved Postgres data directory —
valid when the root volume and `/etc/passwd` are replaced.

## Root-Owned Helper Pattern

Whenever one service user needs a narrow operation that crosses a Unix user
boundary, the host uses a root-owned helper instead of broad filesystem or sudo
access. The helper file is owned by root and not writable by service users;
`sudoers` grants `trustyclaw-admin` permission to execute exactly that helper
path. The helper then does one bounded action, usually by immediately demoting
with `runuser -u <target-user>`.

This is why the admin service can start agent runtimes and read provider account
pins without being able to generally read or write `agent-home`. The launch
helpers run the CLI processes as `trustyclaw-agent`; the account helpers read as
`trustyclaw-agent` and print only the account id or token hash needed by network
guards. If a helper or sudoers entry were writable by a service user, that user
could turn the sudo rule into arbitrary root execution, so these files stay on
the root volume as root-owned code.

`trustyclaw-admin`'s sudoers entry allows only nine fixed helpers in
`/usr/local/lib/trustyclaw-host/`:

- `reboot-host` — runs `systemctl reboot`.
- `run-codex-app-server` — starts a stdio Codex app-server demoted to
  `trustyclaw-agent`, with proxy environment variables and the proxy CA set,
  in a transient scope under the resource-limited `trustyclaw_agent.slice`.
- `read-codex-account-id` — reads the agent user's Codex auth files and prints
  only the inferred ChatGPT account id.
- `run-claude-code` — runs the Claude Code CLI demoted to `trustyclaw-agent`,
  with the same proxy and CA environment, in a transient scope under the same
  slice.
- `read-claude-account` — reads the agent user's Claude Code auth files and
  prints only account metadata plus a SHA-256 hash of the OAuth bearer token.
- `clear-agent-auth` — removes Codex/Claude local auth files as
  `trustyclaw-agent` during linked-account reset.
- `read-agent-file` — demotes to `trustyclaw-agent`, confines paths to
  `agent-home`, rejects symlinks, bounds directory scan work, and lists
  directories or returns bounded text previews.
- `mint-github-app-token` — mints a short-lived, installation-wide GitHub App
  token from an App id, installation id, and private key piped on stdin. It
  runs as root because only root (besides the proxy) has outbound network
  access; the key only ever moves through pipes (admin stdin in, openssl
  stdin on) and nothing is persisted.
- `audit-github-repo` — fetches the per-repository facts behind the operator
  warnings with a token piped on stdin, the same shape as the mint helper:
  root for the egress, facts on stdout, no state.
- `approve-github-push` — replays or cleans up a push held by the `.github`
  approval gate. The admin service passes the reviewed push id, ref updates,
  and working GitHub token on stdin; the helper uses root egress and the
  proxy-state quarantine mirror, then reports JSON success or failure.

Network policy, provider account pins, and network events need no helpers:
they are database tables the admin service (schema owner) writes after
validation and the proxy's narrow database role can only read (plus insert on
its own event table). The GitHub credential is also a database table — admin-owned
with no proxy grant; the admin service publishes only the short-lived working
token to the proxy-readable `proxy_github_token` row, and the proxy injects
it into policy-approved GitHub requests. The agent never holds the credential
— there is no agent-readable token file — so app-token minting, repository
audit, and approved push replay cross privilege boundaries through the helpers
above.

Admin state adds one more boundary with the same shape: the database accepts
Unix-socket connections only, authenticated by OS identity (`peer`), with roles
for exactly `trustyclaw-admin` and `postgres` and an explicit reject for
everyone else — so the agent and proxy users cannot read or write admin state
even though the socket path is technically reachable. Operators inspect it with
`sudo -u postgres psql trustyclaw_admin`.

`trustyclaw-agent` has no sudo, no login shell, and is in no privileged group,
and it cannot write any root-owned code, config, policy, or CA file. To shrink
the set of root daemons it could reach for privilege escalation, the host runs
no container runtime and masks the unused, world-accessible `snapd` socket.
Beyond that, escalation reduces to a generic OS bug (a setuid/kernel local
exploit) — outside this layer's control and bounded by the security group even
if it succeeds. The proxy parses agent traffic, so it has its own service uid
instead of root. It is still intentionally small, dependency-free Python and
shells out only with argument lists (no shell) to absolute paths.

When Cloudflare Access operator access is configured, `cloudflared` is a
separate unprivileged service user with no sudo and no access to admin, proxy,
or agent durable state. Its only TrustyClaw secret is the root-volume tunnel
token file `/etc/trustyclaw/cloudflared.token`, owned `root:cloudflared` and
mode `0640`; this lets the connector read the token without exposing it to the
admin API, proxy, agent, or SSH operator users.
