# Local Sockets

TrustyClaw uses Unix-domain sockets for local, in-host communication that must
not be exposed to the network or gated on the operator password. Each socket is
authenticated by **kernel peer credentials** (`SO_PEERCRED`): the server reads
the connecting process's uid from the kernel and accepts only specific uids, the
same OS-identity model as Postgres peer authentication. Peer credentials cannot
be spoofed by a process running as another uid, and Unix sockets are invisible to
the nftables loopback rules, so adding one does not widen the network surface.

The complete inventory keeps the local trust boundaries auditable in one
place.

## Inventory

| Socket | Server (uid) | Allowed client uids | Purpose |
| --- | --- | --- | --- |
| `/var/run/postgresql/.s.PGSQL.5432` | `postgres` | `trustyclaw-admin`, `trustyclaw-proxy`, `trustyclaw-tools`, `postgres`, and per-app uids, each mapped to its matching database role | Admin, network, tool, and per-app state. `pg_hba.conf` admits these named peer identities and then explicitly rejects everyone else; table/schema grants narrow each non-owner role. There is no TCP listener. |
| `/run/trustyclaw-tools/tools.sock` | `trustyclaw-tools` (tools service) | `trustyclaw-agent`, `trustyclaw-admin` (each path-scoped) | Agent-facing tools surface plus operator delegation, scoped strictly by path per peer. Only `trustyclaw-agent` reaches the MCP routes (`GET /tools`, `POST /call` — the MCP shim forwards `tools/list` and `tools/call`); no admin password, so the agent gets exactly the enabled tools plus the `list_bundled_tools` and `check_tool_approval` built-ins. Only `trustyclaw-admin` reaches the `/operator/...` routes that run the operations needing the tools service's egress (OAuth code exchange, token revoke, approved-action execution). Neither peer can call the other's routes. |
| `/run/trustyclaw-admin-api/app-backend.sock` | `trustyclaw-admin` (admin API) | per-app `trustyclaw-app-<app_id>` uids | App-backend → host admin API, server-to-server. The admin API checks the peer uid against the installed app's Linux user, then applies a narrow app-backend route allowlist (task/thread shapes only). Lets an app backend reach host resources without a second app secret. |

## Design notes

- **Directories are world-traversable, the sockets are peer-gated.** Bootstrap
  gives the admin-api unit `RuntimeDirectory=trustyclaw-admin-api` and the tools
  unit `RuntimeDirectory=trustyclaw-tools`, both at mode `0755`, so the agent/app
  uids can `connect(2)`; access control is the server's peer-uid check, not
  filesystem permissions.
- **Sockets are not TCP.** They carry no port, are unreachable over SSH
  forwarding or Cloudflare Access, and are not affected by the agent's nftables
  loopback drop rules. TCP loopback listeners (the admin API on `127.0.0.1:7443`,
  app backend ports) are separately firewalled by uid; see
  [`network-controls.md`](network-controls.md) and
  [`services-and-runtimes.md`](services-and-runtimes.md).
- **There is no per-app server socket.** The two app directions use different
  transports. Admin → app (serving an app UI request): each installed app
  backend listens on a host-assigned **loopback TCP port**, and the admin API
  reverse-proxies `/v1/apps/<app_id>/api/...` to `127.0.0.1:<app port>`
  (`host/runtime/app_api_proxy.py`); nftables restricts connecting to app
  backend ports to the admin API uid, so the port needs no shared secret. App →
  admin (an app backend calling host resources): all apps share the single
  `app-backend.sock` above, where the peer uid identifies which app is calling
  per connection. A per-app socket would add one file per app for the serving
  direction while the uid-firewalled port already gives the same guarantee.

## The tools service edge

The tools socket is served by the dedicated `trustyclaw-tools` service (see
[`tools/host-integration.md`](tools/host-integration.md)), so the agent connects
to a low-privilege tools-owned socket rather than an admin-owned one. Instead of
the tools service reaching back to admin over a fourth socket, it reads tool
state directly with a Postgres role scoped to the five tool tables plus
read-only access to the encryption key used for its encrypted config and
credentials. The **admin service** connects **into** the tools socket (peer uid `trustyclaw-admin`,
`/operator/...` routes) to delegate the operator operations that need the tools
service's egress. The data-out control is therefore split by design: the tools
service has internet egress but only tool state; the admin service has all other
state but no egress.
