# Users and Privilege Boundaries

| User | Purpose | Privileges |
| --- | --- | --- |
| `trustyclaw-operator` | Human SSH login. | Full passwordless sudo, and therefore intentionally equivalent to root once logged in. |
| `trustyclaw-admin` | Runs the admin API; owns admin state (the `trustyclaw_admin` database role, full access). | sudo for exactly fourteen root helpers (below). No internet egress at all. |
| `trustyclaw-tools` | Runs the bundled tool packages in the dedicated tools service; owns the agent-facing tools socket. | No sudo. Postgres role scoped to the five tool tables plus read access to `secret_keys`. DNS and outbound HTTPS (443) for tool third-party APIs. |
| `trustyclaw-agent-network` | Runs the network-introspection service and owns its agent-facing socket. | No sudo, secrets, or egress. Postgres role has SELECT-only access to network policy and decision-log tables. |
| `trustyclaw-agent-app` | Runs the agent-app service; owns the agent-facing app API socket and proxies attributed `app_api` calls to app backends. | No sudo, database access, secrets, or egress. Its only network reach is opening loopback connections to installed app backend ports (the one uid besides `trustyclaw-admin` nftables allows there). |
| `trustyclaw-proxy` | Runs the policy proxy; owns proxy TLS and Git quarantine files. | No sudo. A narrow Postgres role reads enforcement inputs and the working token/key, inserts network and pending-push records, and prunes network events. Only nftables-approved DNS and TCP 80/443 egress. |
| `trustyclaw-agent` | Runs Codex, Claude Code, Pi, and Hermes runtime processes. | None. No sudo, no direct network, no database role. |
| `trustyclaw-app-<app_id>` | Runs one installed app backend and owns that app's derived Postgres schema, `app_<app_id>`. | No sudo. Its matching Postgres role is confined to the app schema and has no host-table grants. It may answer established admin reverse-proxy connections on its assigned loopback port and call allowlisted host routes over the peer-authenticated app socket, but cannot initiate arbitrary TCP loopback or external connections. |
| `cloudflared` | Runs the optional Cloudflare Tunnel connector. | No sudo, no database role. Only nftables-approved DNS, TCP 443, and TCP/UDP 7844 egress. |
| `postgres` | Runs the admin-state Postgres. | Database superuser over the local socket; no sudo, no network egress. |

The service accounts use fixed numeric IDs: `trustyclaw-admin` is
`47741`, `trustyclaw-proxy` is `47742`, `trustyclaw-agent` is `47743`,
`cloudflared` is `47744`, `postgres` is `47745` (created by bootstrap
before the PostgreSQL packages would assign a dynamic id),
`trustyclaw-tools` is `47746`, `trustyclaw-agent-app` is `47747`, and
`trustyclaw-agent-network` is `47748`.
Installed app service
users use the reserved UID/GID range `48000-48099`, matching the 100-app
cap. Each app package declares one stable `host_slot`; the host generates the
UID and GID as `48000 + host_slot` and creates
`trustyclaw-app-<app_id>` from the validated package list.
Stable IDs keep durable EBS ownership — including the preserved Postgres data
directory and app-owned schemas/files — valid when the root volume and
`/etc/passwd` are replaced.

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

`trustyclaw-admin`'s sudoers entry allows only fourteen fixed helpers in
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
- `run-pi` — starts the Pi RPC process as `trustyclaw-agent`, with fixed dummy
  AWS routing values and proxy-only network access, in the agent slice.
- `run-hermes` — starts one Hermes query as `trustyclaw-agent`, passes the
  prompt over stdin, and uses the same dummy AWS and agent-slice boundary.
- `read-aws-account` — receives the shared Bedrock key pair from the admin
  service through its environment and makes exactly one STS identity request.
  Root egress is required because the admin uid has none; the credential is
  never written to disk or exposed to the agent.
- `clear-agent-auth` — removes Codex/Claude local auth files as
  `trustyclaw-agent` during linked-account reset.
- `read-agent-file` — demotes to `trustyclaw-agent`, confines paths to
  `agent-home`, rejects symlinks, bounds directory scan work, and lists
  directories, returns bounded text previews, or streams one bounded regular
  file to the authenticated Files viewer.
- `check-for-upgrade` — fetches only the public
  `infiloop2/trustyclaw` main-branch `VERSION` file over HTTPS, with strict
  connection, transfer-time, and response-size limits. It accepts no input.
- `mint-github-app-token` — mints a short-lived, installation-wide GitHub App
  token from an App id, installation id, and private key piped on stdin. It
  runs as root because the admin service has no outbound network access; the
  key only ever moves through pipes (admin stdin in, openssl
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
validation. The proxy's narrow database role reads enforcement inputs,
inserts and prunes its own events, and inserts held-push records. The GitHub
credential is also an admin-owned database table
with no proxy grant; the admin service publishes only the short-lived working
token to the proxy-readable `proxy_github_token` row, and the proxy injects
it into policy-approved GitHub requests. The agent never holds the credential
— there is no agent-readable token file — so app-token minting, repository
audit, and approved push replay cross privilege boundaries through the helpers
above.

The tools socket (`/run/trustyclaw-tools/tools.sock`) is the deliberate crossing
from the agent to the tools service: the harnesses spawn the MCP shim as
`trustyclaw-agent`, and the `trustyclaw-tools`-owned socket service accepts only
the `trustyclaw-agent` and `trustyclaw-admin` uids by kernel peer credentials
(`SO_PEERCRED`). The agent gets exactly the enabled tools' actions; the admin
service uses the same socket (admin uid, `/operator/...` routes) to delegate the
operator operations that need the tools service's egress. Tool secrets and
approval decisions stay in Postgres, reachable by the scoped `trustyclaw-tools`
role and the admin role but not the agent, and approval-gated actions still
require the operator's decision in the admin UI.

For local-video handoff, the MCP shim opens a regular file under the agent uid
and streams only its bytes and bounded metadata through the same socket. The
tools service never receives or opens the agent pathname. Its private runtime
spool is mode 0700 with mode-0600 assets; packages see only tool-scoped ids and
already-open streams. Instagram bytes remain local until approval.

The agent-app socket (`/run/trustyclaw-agent-app/agent-app.sock`) is the second
deliberate agent-side crossing, from the agent to the `trustyclaw-agent-app`
service: peer credentials admit only the agent uid, the caller's host thread is
read from its cgroup (turns run in systemd scopes named by thread id, which is
kernel state the agent cannot rewrite), and the app prefix selects the owning
app's `/agent/` routes. The service holds no secrets and no egress; see
[`agent-app-api.md`](apps/agent-app-api.md).

The network-introspection socket
(`/run/trustyclaw-agent-network/agent-network.sock`) is a separate read-only
crossing from the agent to `trustyclaw-agent-network`. Peer credentials admit
only the agent uid. The service has no egress and can SELECT only policy and
network-event tables, so the egress-capable tools service receives no network
policy database access.

Admin state adds one more boundary with the same shape: the database accepts
Unix-socket connections only, authenticated by OS identity (`peer`), with a role
for `trustyclaw-admin` (full admin state), narrowly scoped roles for
`trustyclaw-proxy` (enforcement inputs, working events/pushes, and its token),
`trustyclaw-tools` (the five tool tables plus the shared encryption key),
`trustyclaw-agent-network` (SELECT-only policy and network-event state),
per-app roles confined to their schemas, `postgres` for operators, and an
explicit reject for everyone else, so
the agent user cannot read or write admin state, and a compromised tools, proxy,
or app service reaches only its granted tables, even though the socket
path is technically reachable. Operators inspect it with
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
mode `0640`; this lets the connector read the token without exposing it
directly to the admin API, proxy, or agent. The SSH operator can read it only
by deliberately using its unrestricted sudo authority.
