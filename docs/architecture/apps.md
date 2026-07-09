# Apps

Apps are TrustyClaw-managed product surfaces that run next to the host admin
plane. An app provides code for a narrow workflow: a backend API service,
database migrations, and UI assets. The host decides how that code is mounted,
which local user runs it, which database namespace it receives, and where its UI
appears in the admin shell.

Apps are not raw chat threads and they are not arbitrary agent plugins. Agents
work inside a durable host and apps provide a richer UX for humans to interact
with that work. Host-owned resources such as agent tasks, runtime credentials,
network policy, process control, files, and logs stay behind the host admin API.

The important security property is that the host can load app code without
letting that code quietly bypass the policies that make TrustyClaw fail closed.
App code may orchestrate a workflow, but host boundaries remain host-enforced.

## Product Thesis

Most AI products are either chat windows or one-off tool calls. Apps make the
agent useful in a different way: the agent works inside durable workflows that
humans can inspect, correct, and operate over time.

An app owns a domain such as agent chat, GitHub review, recruiting, finance
operations, support triage, CRM, billing, or personal operations. The app stores
domain state, renders it in a purpose-built UI, and uses the host admin API for
agent work and host resources.

This creates compounding value:

- The agent is not stateless. It works over accumulated app memory.
- The UI is not just a transcript. It is the operating surface for the workflow.
- Agent actions are not arbitrary shell activity with arbitrary network access.
  They go through host-owned admin APIs and host network controls.

The long-term product is an operating system for AI-run work: users install or
create apps, give agents scoped authority inside them, and supervise results
through durable state instead of reading every token of every conversation.

## App Package

An app package is a directory with a manifest and app-owned files. The manifest
is the exact contract the app provides to the host.

```json
{
  "id": "<app_id>",
  "title": "<App Title>",
  "backend": {
    "entrypoint": "backend.py"
  },
  "database": {
    "migrations": "migrations"
  },
  "ui": {
    "path": "ui"
  }
}
```

`id` is the stable app identity. The host requires it to be unique across
installed apps and to match the snake_case pattern
`^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$`. The host uses this id to derive
collision-proof names for the Linux user, database schema, database role,
service unit, route prefix, and UI mount point.

`title` is the human-readable name shown in app listings and diagnostics.

`backend.entrypoint` is the app backend server code, relative to the app
package. The app provides code only; the host chooses the port, local bind
address, environment, service unit name, working directory, and run user. Port
assignments are derived from stable app ids, not manifest scan order.

`database.migrations` is a directory of app migration SQL files, relative to the
app package. The app does not choose its schema name or role name. The host
derives them from `id`, creates them, and runs the migration files under the app
database role.

`ui.path` is a directory of app UI assets, relative to the app package. The app
does not choose its final URL. The host mounts the UI under a host-derived path
such as `/v1/apps/<app_id>/ui/`, and uses the app title as the admin tab
label.

## Host Integration

The host integrates an app by reading the manifest, validating every referenced
path, and deriving all host-owned names from the app id. Duplicate ids,
invalid ids, path traversal, and generated-name collisions are bootstrap
errors. The first implementation caps the installed app registry at 100 apps so
port assignment, provisioning, and admin UI mounting stay inside an intentionally
bounded surface. Static UID/GID and port allocations live in the root-owned
`host/apps/registry.json` file so bootstrap provisioning and `/v1/apps`
metadata are generated from the same app list.

The host derives names from the app id:

| Host object | Derived value |
| --- | --- |
| Linux user | `trustyclaw-app-<app_id>` |
| Postgres schema | `app_<app_id>` |
| Postgres role | `trustyclaw-app-<app_id>` |
| systemd unit | `trustyclaw-app-<app_id>.service` |
| app API route | `/v1/apps/<app_id>/api/` |
| app UI route | `/v1/apps/<app_id>/ui/` |

The app backend runs as the app Linux user. That user owns no host resources and
does not get direct access to agent home directories, proxy state, root helpers,
runtime auth files, or host-owned database tables. It may connect to Postgres as
the matching app role.

App UI does not call host admin APIs directly and never receives the operator's
admin credential. The isolated frame posts app-backend requests to the parent
admin shell. The parent adds the operator's existing admin auth only for that
app's reverse-proxy route, `/v1/apps/<app_id>/api/...`. The admin API verifies
the normal admin auth, verifies the route is an installed app backend route, then
proxies to the app service over host loopback with a host proxy marker. The
operator's raw admin credential is stripped before the request reaches the app
backend.

When an app backend needs host resources such as tasks or threads, it calls the
host admin API server-to-server over a local Unix-domain socket. The admin API
authenticates that socket by checking the peer process uid against the installed
app's Linux user. This avoids storing a second app secret while keeping the
browser-facing TCP admin API protected by the operator password. Server-to-server
calls are then checked against an app-backend route allowlist. The first
allowlist is intentionally narrow: it includes only task creation and task/thread
lookup or control route shapes needed by the first app workflow. It does not
allow broad host routes such as network policy, files, process inventory,
runtime auth, app registry, or generic task/thread listing.

The host chooses the backend port and passes it through environment. App code is
expected to bind the provided loopback address and port, but the host does not
rely on app self-discipline for route security: the admin API proxies only to
the host-derived app port, and nftables makes that assigned listener
admin-proxy-only. New TCP connections to the assigned app port are accepted only
from the `trustyclaw-admin` uid and are dropped for every other local uid before
the broad loopback allow rule. If app code binds a different port, the host will
not route app UI traffic to it, it is not exposed through SSH forwarding,
Cloudflare Access, or any non-loopback interface, and the app service uid still
cannot initiate arbitrary loopback connections. Stronger kernel-level prevention
of arbitrary binds would require socket activation or per-service socket-bind
filtering on hosts that support it; the current security boundary is the
admin-proxy route, firewall reachability, and app-backend allowlist.
The app service uid may still send established loopback responses for
admin-proxied requests, but it cannot initiate arbitrary TCP loopback
connections.
External network access is still controlled by the host network controls.
Operator access endpoints such as SSH forwarding and Cloudflare Access expose
only the host admin API; app backend ports are not separately exposed.
App service users also have loopback restrictions: they may answer established
host reverse-proxy connections, but they may not open arbitrary TCP loopback
connections to the unauthenticated network proxy, the browser-facing admin API,
other app backends, or other local listeners. Server-to-server host API access
uses the local Unix socket described above instead of TCP loopback; that is how
an app backend gets task/thread data from the host admin API.

The host mounts app UI into the authenticated admin shell as an isolated frame.
App UI does not run as same-origin JavaScript inside the host admin page. The
host renders app UI in a sandboxed iframe without `allow-same-origin`, and app
UI asset responses carry a CSP `sandbox` directive so direct/top-level app UI
loads also receive an opaque origin. A third-party or compromised app that
shared the admin browser principal could read JS-accessible admin credentials
and call host routes as the operator, so the only browser bridge exposed to app
UI is a reverse-proxy helper for that app's backend route. The bridge is not a
generic host admin API bridge.

App UI asset CSP is intentionally narrow. App frames may load scripts, styles,
images, and fonts only from the host-derived app UI route/origin that served the
asset, with `data:` allowed for images and fonts. `connect-src` stays `none`, so
browser network calls cannot bypass the parent bridge, and wildcard image/style
sources are not allowed. The explicit app asset origin in CSP exists so the same
policy works when a test or deployment serves the admin API on an ephemeral host
or port; it is not permission to beacon to arbitrary origins.

## Storage And Migrations

App state lives in Postgres under an app-owned schema. The host guarantees name
collision avoidance by deriving storage names from the validated app id and
rejecting any duplicate generated names.

Apps store the workflow state they own instead of using host admin lists as
their source of truth. For a chat-like workflow, that means app-owned thread
records, related host task ids, and archive state. The app does not duplicate
task transcripts or runtime execution data because those remain available
through allowlisted host admin API routes. This matters once multiple apps use
the same host admin task API: each app can show the threads it started and hide
or archive them according to its own product rules, without exposing unrelated
threads created by another app or host surface.

Each app gets:

- A dedicated Linux service user.
- A dedicated Postgres role with the same name.
- A dedicated Postgres schema such as `app_<app_id>`.
- Migration records in a host-owned migration table.

Bootstrap applies app migrations after core admin-state migrations. The host
creates the app role and schema first, then runs each app migration SQL file
through an app-role database connection with only the app role's schema-limited
privileges. The host, not the app role, records which app migration versions
have been applied.

This split matters: app migrations can create and change app-owned tables, but
they do not run with host database privileges that could read, modify, or grant
access to host-owned tables.

Because the app-role SQL commit and host-owned version record are separate
database writes, app migrations are replay-safe. If bootstrap is interrupted
after app SQL commits but before the host records the version, the next bootstrap
reruns the same app migration and then records it without manual database
repair.

## Safety Model

The host owns the security boundary even though it loads app code. The boundary
is designed around bad or compromised app assets:

- Malicious UI assets can try to steal operator credentials, impersonate
  operator clicks, call host APIs outside the app's workflow, phish the operator,
  or exfiltrate data shown in the admin shell. They run in an isolated frame
  with an opaque origin, without direct access to the host admin
  JavaScript context, cookies, local storage, or raw admin credential. The
  parent bridge only attaches admin auth for that app's backend proxy route; it
  does not expose direct task, file, runtime, network, process, or other host
  admin API calls to app UI.
- Malicious backend API code can try to read runtime auth files, agent home
  directories, proxy state, host config, logs, root helpers, other apps'
  listeners, or unauthenticated local services. It can also try to start network
  side effects directly. The host runs it as a dedicated app Linux user with no
  ownership of host resources, an app role limited to the app schema, and no
  direct reachability to host-owned listeners. The app TCP listener binds only
  loopback and nftables accepts new connections to that app port only from the
  `trustyclaw-admin` uid; agent runtimes, app users, and ordinary local users
  cannot call it directly even if they spoof the host proxy header.
  App users do not get arbitrary outbound TCP loopback access. They can answer
  established admin-proxy TCP connections to their assigned app port, and they
  use peer-authenticated Unix sockets for host admin API and Postgres access.
  External network behavior is mediated by host integrations and network
  controls. App backend calls to the host admin API are restricted to an
  app-backend route allowlist. The first allowlist covers only the task/thread
  route shapes needed by the first app workflow; app-object ownership,
  app-specific credentials, broader route/capability scopes, and rate limits are
  future extensions.
  App service units do not yet have app-specific cgroup resource bounds such as
  an app slice, CPU quota or weight, memory and swap caps, task-count caps, or
  bounded restart bursts. A malicious app backend can therefore still try to
  starve host services with CPU loops, fork bombs, or memory growth. Adding
  host-owned app unit resource limits is future hardening before third-party app
  backends are treated as strongly contained against resource exhaustion.
- Malicious database tables or migration SQL can try to read or modify
  host-owned tables, grant itself privileges, collide with another app's schema,
  poison migration records, or create non-replay-safe state that bricks future
  bootstrap. App SQL runs as the app database role with schema-limited
  privileges, while the host owns migration records, validates derived names,
  rejects collisions, and requires replay-safe app migrations.

Those threat cases drive the concrete controls:

- App ids are validated and all host object names are derived by the host.
- App services run as separate Linux users.
- App backend TCP listeners bind only loopback, are not exposed by operator
  access endpoints, and are reachable only from the admin API service uid.
- App database objects are namespaced and owned by app roles.
- App migration SQL runs under the app role, while migration records stay
  host-owned.
- App UI is isolated from the host admin origin and receives only explicit
  bridge access to its own backend proxy route.
- App backend code reaches agent tasks, runtime credentials, files, processes,
  and network controls only through host admin APIs over the local app-backend
  socket.
- External side effects require host integrations and host network enforcement.

## Future App Credential Scoping

The current app-backend credential is the app service user's Unix peer identity
on the local admin API socket. It authenticates which installed app process made
the request and then applies a route-shape allowlist. It does not yet scope host
objects by app ownership. A backend for one installed app can therefore call the
currently allowlisted task/thread routes for task ids or thread ids that were
created by another app or by the core admin UI if it knows those ids. This is an
accepted limitation of the first implementation. The same limitation applies to
caller-provided host thread ids: an app can ask the host task API to use a
thread id that already exists outside that app, so future hardening needs
app-scoped thread identity or explicit grants before host sessions are reused.

The hardening direction is an app-scoped host capability model:

- Host-issued app backend credentials or peer-authenticated app identities map
  to explicit capabilities by app id.
- Task, thread, file, and other host objects gain app ownership or capability
  grants at creation time.
- Host thread/session identifiers created through app backends are app-scoped,
  or the host rejects reuse of ungranted existing thread/session ids.
- Host admin API dispatch checks both route capability and object ownership
  before returning or mutating data.
- App backend permissions include rate limits and audit labels per app.
- The UI bridge continues to forward only app-backend proxy calls; direct host
  admin API calls remain outside the app UI surface.

The host can make apps easy to install and run while staying the authority for
execution, credentials, storage namespaces, network policy, and UI isolation.
