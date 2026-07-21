# TrustyClaw Host Integration

How this host implements the tool contract in
[`tool-contract.md`](tool-contract.md): which user and service run tool code and
how they reach the internet, how the agent calls tools, how the operator installs
and configures them, and how approvals resolve. The framework and bundled
packages themselves are host-neutral; everything here is TrustyClaw-specific and
lives in `host/`.

Each TrustyClaw host is for one operator. Tool credentials, config, and approvals
are partitioned by `tool_id`.

## Where tool code runs, and its internet access

Tool packages make outbound HTTPS calls to third parties (Google, Brave, X,
LinkedIn, Serper, Meta/Instagram, ScrapeCreators, Polymarket, Interactive
Brokers, and Runway) and
parse their responses, so unlike other host code they need direct egress and are
the host code most exposed to attacker-influenced data. They run in a **dedicated
`trustyclaw-tools` service** — its own Linux user, running
`host.runtime.tools.service` — kept out of the admin service. nftables grants the
`trustyclaw-tools` uid DNS and outbound HTTPS (port 443) and **nothing to the
`trustyclaw-admin` uid**, so the admin service holds no internet egress at all: a
compromised tool package cannot exfiltrate admin state or reach an arbitrary host.
The agent never holds tool secrets and never talks to tool third parties; its own
path through the policy proxy is unchanged, and tool traffic never rides the agent
proxy (which would have opened those domains to the agent as an exfiltration path).

The tool tables (`enabled_tools`, `tool_config`, `tool_credentials`,
`tool_approvals`, `tool_events`) are **owned by `trustyclaw-admin`**: the database
is created owned by that role and the migrations run as it, so — as the table
owner — the admin service has full read/write on them implicitly, no `GRANT`
needed. The **`trustyclaw-tools` role** the tools service connects as is layered
on top with an *additional, scoped* grant: read-only on `enabled_tools`/
`tool_config`, read/write on `tool_credentials`/`tool_approvals`/`tool_events`, and
nothing else in the admin database. Those grants live in the schema migration
(`host/migrations/0007_tool_state.sql`), the same pattern as the proxy role's
grants; bootstrap provisions only the role, its `pg_hba` line, and database
CONNECT before migrations run. That grant does not remove the owner's access;
it confines the *tools* role. So the confinement is asymmetric by design: the admin
service reaches all state (including the tool tables it owns) but has no internet
egress, while the tools service has egress but can touch only the tool tables — a
compromised tool package therefore cannot exfiltrate admin state, and admin, which
owns everything, has no way out to the internet. The admin service holds no tool
code path that parses third-party data; it authenticates the operator and
**forwards** the whole OAuth connect flow (start, complete, disconnect) plus
approved-action execution — everything that runs tool code or needs egress — to
the tools service over the tools socket (peer-gated to the admin uid). The
operator operations that touch only stored state (listing tools, enable/disable,
config, listing/reading approvals) run in the admin service against the tool
tables it owns, and need no egress. See [`../local-sockets.md`](../local-sockets.md) for
the socket inventory and [`../privilege-boundaries.md`](../privilege-boundaries.md)
for the service/user map.

## The agent-facing surface

Agents speak MCP, so the host bridges MCP to the tool runtime with a shim:

- Both harnesses spawn `python3 -m host.runtime.agent_shim.mcp_shim` as
  `trustyclaw-agent` — Claude Code through `--mcp-config` (with
  `--strict-mcp-config` making it the only server), Codex through `mcp_servers`
  in the root-owned managed config `/etc/codex/managed_config.toml`.
- The shim is a dumb stdio-to-socket pipe: `tools/list` and `tools/call` forward
  to the tools socket `/run/trustyclaw-tools/tools.sock`. It holds no state and
  no secrets. A **`tools/call`** failure — including the tools service being
  unavailable — is forwarded to the agent as a normal MCP result with
  `isError: true` and a sanitized message, so the agent sees the error and can
  react. Only **`tools/list`** falls back to the stable `app_api` declaration
  when the tools socket is unavailable, rather than erroring: an error at list
  time can make a harness disable the MCP server for the whole session, so the
  fallback keeps the session healthy and a later list picks the bundled tools
  back up once the service is up.
- The same shim always serves the **`app_api`** tool,
  forwarded to the separate agent-app socket
  (`/run/trustyclaw-agent-app/agent-app.sock`) rather than the tools socket.
  Listing it grants no authority. On every call the service attributes the
  session to a running app task with an agent API; there is nothing to
  configure and no secret involved because attribution comes from the
  caller's cgroup. See
  [`../apps/agent-app-api.md`](../apps/agent-app-api.md).
- The socket service authenticates by kernel peer credentials (`SO_PEERCRED`),
  the same OS-identity pattern as Postgres peer auth, and scopes each peer
  strictly by path: only the `trustyclaw-agent` uid reaches the MCP routes
  (`GET /tools`, `POST /call`, `POST /assets/video`, `POST /assets/image`) and only the
  `trustyclaw-admin` uid reaches the
  `/operator/...` delegation routes — neither can call the other's routes. No
  admin password is involved, so the agent gains exactly this tool surface and
  nothing else. Unix sockets are
  invisible to the nftables loopback rules, so the agent's drop rules are
  untouched. See [`../local-sockets.md`](../local-sockets.md) for the full local-socket inventory.

The listed actions are the enabled tools' manifest actions, named
`<tool_id>_<action>` (e.g. `gmail_search_messages`), plus host actions:

- **`list_bundled_tools`** returns the full bundled catalog — `tool_id`, display
  name, description, connection type, `enabled`, and action ids — from manifests
  plus the enablement set only (no credentials, no third-party calls). It lets
  the agent distinguish *bundled but not enabled* (ask the operator to enable it
  in the Tools tab) from *not bundled at all* (no host integration exists; the
  agent tells the operator the tool is not implemented and to file a feature
  request), instead of inferring from an empty list.
- **`list_network_integrations`** and **`recent_network_denials`** are the
  agent's read-only view of the host's network controls. A separate non-egress
  `trustyclaw-agent-network` service serves them from
  `/run/trustyclaw-agent-network/agent-network.sock`; the MCP shim aggregates
  that listing with the tools socket. The first describes each integration and
  its typed policy options, and the second returns the newest denied requests
  from the proxy's decision log with each denial's code and guidance. They
  exist because agent HTTP clients usually swallow the proxy's 403 body. The
  network service reads only policy and event tables under SELECT grants; the
  egress-capable tools role cannot read those tables. See
  [network controls](../network-controls.md#denial-reasons-and-agent-introspection).
- **`check_tool_approval`** is how the host meets its contract obligation to let
  the agent query an approval's status and terminal result: an approval-gated
  call returns a single token-bearing `approval_id` (`approval_<number>.<token>`),
  and `check_tool_approval` verifies that token before it returns the summary or
  the terminal execution result — so another agent process cannot enumerate old
  approvals by guessing sequential ids. Direct-action results are returned inline
  on the call.
- **`stage_video` and `stage_image`** are public MCP actions because that process
  has the agent's filesystem access. They open a regular, non-symlink MP4/MOV
  or JPEG/PNG/WebP as the agent, bound it to 512 bytes–200 MB, and stream raw
  bytes through `POST /assets/video` or `POST /assets/image`. The tools service
  returns a random, tool-scoped asset id. The agent passes that id directly to
  the consuming Runway or Instagram action and does not persist it as app state.
  The shim stores no copy. It streams the opened descriptor over
  `/run/trustyclaw-tools/tools.sock`; the socket's kernel peer credentials
  authenticate the `trustyclaw-agent` UID, then the separate
  `trustyclaw-tools` process writes the bytes as its own UID under
  `/mnt/trustyclaw-admin/tools-state/assets`.
  The tools service separately reads and writes its scoped Postgres tool
  tables, whose database files live on the persistent admin volume at
  `/mnt/trustyclaw-admin`; staged media bytes never enter those tables. The
  service code itself is installed on the instance root volume.
  The shim opens the file with `O_NOFOLLOW`, then checks the opened descriptor
  with `fstat`; this rejects a symlink at the final path component plus
  directories, devices, sockets, and FIFOs. Streaming continues from that
  descriptor, so replacing the pathname after open cannot switch the source.
  Parent directories follow normal OS path resolution and cannot grant access
  beyond the `trustyclaw-agent` user's permissions. The tools service never
  uses the supplied filename as a path: it writes the bytes to a random,
  exclusive-create, mode-0600 file in its mode-0700 asset directory.
  The tools service accepts only the authenticated agent peer, receives
  filename/type/length but no pathname, and stores a mode-0600 private copy in
  its mode-0700 asset directory. The returned random id is scoped to either
  Runway or Instagram. Runway deletes its input copy after Runway accepts the
  generation or editing task. Instagram deletes its copy after publishing.
  Every remaining id expires after about
  26 hours. The next staging/access check
  removes it immediately after expiry, and an hourly service sweep removes it
  even when no later call touches the store. A tools-service restart deletes
  every remaining file and forgets every id. No asset metadata enters
  Postgres, and startup does not reconcile pending approvals against lost
  assets. An approval created near expiry can outlive its asset too. Approving
  either one later fails and the upload must be retried. At most 20 assets and
  1 GB total are staged across both media types. This private spool preserves
  the Instagram approval boundary: Meta receives no video bytes until approval.

  Binary action output uses a generic exclusive result. A package returns either
  `ActionExecuted` JSON or one `StreamingAsset`, never both. The tools service
  enters the stream, validates its basename-only filename, media type, and exact
  length up to 200 MB, and relays it directly over the existing agent Unix-socket
  response. Provider output does not land in the private staged-input spool.

  The MCP shim recognizes the binary response and always converts it into one
  mode-0600 agent file under `/tool_assets`. It validates the same bounded
  metadata, streams into an exclusive temporary file, verifies the declared
  length, fsyncs it, and atomically exposes a random host-generated filename.
  Failure removes the partial file. The final MCP result is JSON containing only
  `path`, `media_type`, and `size_bytes`; the agent never chooses a destination
  path or sees a transport id.

  Runway is the first producer. `runway_save_video {task_id}` re-reads the task
  from Runway, accepts only its authoritative successful HTTPS output, and
  returns that response as a `StreamingAsset`. The egress-capable tools process
  cannot write agent files, while the filesystem-capable shim has no Runway
  credential or external network access. To publish the returned workspace file
  later, the agent explicitly stages it for Instagram and passes the resulting
  `video_asset_id` into `instagram_post_reel`; the Instagram package and shim
  interface stay unchanged.

  One process lock owns every index and quota transition. An upload reserves
  its declared count and bytes under that lock, then streams outside it so a
  slow agent cannot block reads or cleanup; the reservation is not readable
  until its hash and file are complete. Cleanup and deletion remove the index
  entry under the same lock. A reader either opens the file first and keeps
  using that Unix file descriptor after an unlink, or loses the race and fails
  closed before sending bytes to a provider.

  Staging is a separate public action because bundled tool packages execute as
  `trustyclaw-tools`, without access to the agent filesystem. Giving package
  code a local path would either be unusable or give internet-facing tool code
  read access to agent files. It would also force large media through JSON tool
  arguments and the audit log. The explicit two-call flow keeps package schemas
  unchanged: stage the file, then pass the short-lived id to the media action.
  One shared host implementation enforces size, count, lifetime, destination,
  and cleanup limits.

**Concurrency cap.** Each agent tool call blocks one handler thread on a
third-party request, so the agent's in-flight tool calls are capped at
`MAX_CONCURRENT_CALLS = 8` (`host/runtime/tools/api.py`). The cap is global across
**all** of the agent's tool calls, not per `tool_id`. Agent calls beyond the cap are rejected immediately
with HTTP 429 — before the request body is read — rather than queueing. The
operator delegation routes are not subject to this cap, so a busy agent can never
block the operator from deciding approvals or disconnecting a tool.

## Input validation

The host schema-validates action input against the manifest `input_schema`
before invoking the package, and validates a direct action's result against its
`output_schema`. Per the contract, the tool still owns semantic validation and
returns a specific `ActionFailed`; the manifest `input_schema` is always required
regardless, because the agent needs it to call the action over MCP. (The host
validation is defense-in-depth over the small JSON Schema subset manifests use;
it is not a substitute for the tool's own checks.)

Registration rejects any declared keyword or shape outside exactly that
enforced subset, including empty input schemas, untyped nested values, invalid
array bounds, and `required` names without declared properties. The unit suite
discovers and registers every bundled package, so an invalid manifest fails the
pull request check before it can reach a host.

## Host API implementation

`host.runtime.tools.tools_host` implements the contract against admin state:

- **Credentials** — the `tool_credentials` table, one row per tool holding that
  tool's `StoredCredential` in its contract fields: the non-secret connected-
  account columns (`account_id`, `account_label`, `account_scopes`), the
  `secret` column (the provider token JSON, serialized and stored as secretbox
  ciphertext, encrypted at rest like every other secret column), and the
  non-secret `metadata` bookkeeping.
- **Config** — the `tool_config` table, keyed by `(tool_id, key)`: config is
  scoped per tool, so Gmail and Calendar each hold their own
  `GOOGLE_OAUTH_CLIENT_ID` even though the key name repeats. Values are secretbox
  ciphertext and never leave the host; the API/UI report only whether a key is
  set.
- **Approvals** — the `tool_approvals` table. The host assigns `approval_<number>`
  ids; every status change is an atomic conditional update from the expected
  prior status, so an approval is single-use by construction. Exact host policy:
  new pending approvals are capped at `PENDING_APPROVAL_LIMIT = 1000` (backpressure
  once reached), pending approvals expire after
  `APPROVAL_PENDING_TTL_SECONDS = 24h` (swept by the admin API's hourly
  maintenance pass), decided records are kept as bounded history pruned to
  `APPROVAL_HISTORY_LIMIT = 10,000`, and an approval caught mid-execution by a
  service restart is marked failed at startup (an unknown outcome spends it).
- **Assets.** Ephemeral files under
  `/mnt/trustyclaw-admin/tools-state/assets`, indexed only in the tools service
  process and exposed to packages as tool-scoped metadata/open-stream/delete
  operations. They never enter Postgres. The service clears the directory on
  every start and sweeps expired files hourly; it does not rewrite approval
  state to reconcile files lost across a restart.
- **Audit** — every tool call, approval decision, connect/disconnect, enable/
  disable, and config change is recorded in the `tool_events` table
  (`tool_id`, `action_id`, `outcome`, `detail`, and the exact bounded action
  `arguments` for calls and approval decisions),
  the tool-side peer of the agent and network event logs. `GET /v1/tools/events`
  pages summaries newest-first with the same `before`/`limit` cursor model;
  `GET /v1/tools/events/<seq>` loads one event's arguments only when the
  operator expands it. Config values and OAuth callback parameters never enter
  the argument field.

## Operator flow

Tools live on the admin UI's **Internet Access and Tools** tab, in their own
section beneath the network controls and formatted the same way: one card per
tool, matching the managed-integration rows. Each card carries enable/disable, an
info popover with its summary and protections, write-only config inputs with set
indicators, the OAuth connect/disconnect buttons, and, when expanded, the tool
approvals table. The separate **Integration Guides** tab renders the manifest's
ordered setup steps (with this host's callback URI and the tool's config keys
shown inside the step that needs them), exact action list with approval
controls, local audited screenshots, and the data summary — what leaves the
host and where it can go, what the third party can do with it, and how long it
retains it, with authoritative policy links. Backed by the
`/v1/tools` API (see
[`../../api/AdminAPI.md`](../../api/AdminAPI.md)):

1. **Configure** the deployment values a tool declares (per tool; all secret,
   write-only) at `PUT /v1/tools/<tool_id>/config`.
2. **Enable** the tool for agent calls. Enablement is not gated on config: a tool
   can be enabled with partial or no config (the config status stays visible per
   key in the tool listing), and an action that needs a key that is not set fails
   when the tool reads it, with the operator-actionable message "Tool config
   `<KEY>` is not set. The operator must set it in the admin UI's Tools tab.";
   while actions that do not need it still work.
3. **Connect** (OAuth tools): the tool builds the provider authorization URL, the
   browser returns to `/oauth/callback` on the admin origin, and the UI completes
   the exchange. The operator registers that callback URL once with the provider.
   Tokens live in the tool credential store; disconnect revokes and deletes them
   (also via the tools service). The authorization model for the callback and the
   exchange is described in [OAuth callback and token exchange](#oauth-callback-and-token-exchange)
   below.
4. **Decide approvals**: pending approval-gated actions appear with the tool's
   redacted summary and the exact recorded payload. Approving runs
   `execute_approved` immediately and reports the outcome; denial is terminal.

## OAuth callback and token exchange

OAuth tool connect flows split into two requests with deliberately different
authorization, because a provider redirect and an API call have different
trust properties.

**`GET /oauth/callback` is not admin-authenticated, by design.** After the
operator authorizes at the provider, the provider redirects the operator's
*browser* to `/oauth/callback?code=...&state=...` on the admin origin. A browser
redirect cannot carry the admin bearer token, so this path is served like every
other admin-UI asset — it returns the same static SPA shell (`admin_ui.html`)
that `/` returns, with no secrets in the response and no side effect. It does
not perform the token exchange; it only lets the already-loaded SPA read the
`code` and `state` query parameters. Nothing security-sensitive happens here.
The provider's own servers never call this URL — only the operator's browser
does — so the callback never needs to be reachable from the public internet;
it only needs to be reachable by the operator's browser.

**`POST /v1/tools/<tool_id>/oauth_connect/complete` is the only path that
performs the exchange, and it is fully authenticated.** The SPA reads the
returned `code`/`state` and calls this API with the operator's admin bearer
token, exactly like every other `/v1/...` request. The handler additionally
re-verifies the `state` the tool minted at connect start: an HMAC keyed on the
deployment's OAuth client secret over `{tool_id, nonce, issued_at}`, checked for
a matching `tool_id` and a 15-minute expiry. So a forged, replayed, or
cross-tool callback cannot complete a connection even from an authenticated
session, and an unauthenticated caller cannot complete one at all. The admin
service holds no egress: it forwards the exchange to the tools service, which
calls the provider's token endpoint over its own egress, so the flow is
identical whether the operator reached the UI over SSH-forwarded loopback
(`http://localhost:7443/oauth/callback`, which providers accept without HTTPS)
or over a Cloudflare Access hostname.

**No API path is served without the admin password.** The unauthenticated GETs
are the static UI assets — the SPA shell (served at both `/` and
`/oauth/callback`), `admin_ui.css`, the `admin_ui/*.js` modules, and the
favicons — plus each installed app's own static UI assets under
`/v1/apps/<app_id>/ui/...`, which are served before authentication for the
sandboxed iframe. Every other `/v1/...` route, including `oauth_connect/complete`
and every `/v1/apps/<app_id>/api/...` call, passes through admin authentication
before it runs. The unauthenticated set carries no
secrets and performs no state change.

**Abuse and DDoS.** The origin does not rate-limit its own HTTP; it is never
anonymously reachable, so a volumetric flood has no unauthenticated path to a
meaningful endpoint. In the two supported access modes the request is gated
before it reaches the origin: SSH-forwarded loopback requires a host account,
and Cloudflare Access requires passing the Access identity policy (and gets
Cloudflare's edge DDoS mitigation for free). The one unauthenticated origin
path, the callback GET, serves a cached static file with no database, crypto, or
egress work, so it is cheap to absorb; the expensive path (`complete`) is behind
admin auth. Adding app-level rate limiting at the origin would be redundant
given it is never openly exposed, so we rely on the access boundary instead.

**Under Cloudflare Access.** The redirect URI registered with the provider is
the Cloudflare Access hostname. The operator has already passed Access to load
the admin UI, so their browser holds the `CF_Authorization` cookie for that
hostname; when the provider redirects the browser back to
`https://<hostname>/oauth/callback`, the browser sends that cookie automatically
and Access lets the request through without re-prompting (the OAuth round trip
takes seconds, well inside the Access session lifetime). The subsequent
`complete` POST carries both the Access cookie and the origin's admin bearer, so
under Access the exchange sits behind two independent identity checks. Because
the provider only redirects the browser and never calls the origin itself,
Access gating inbound requests never breaks the flow.

## Testing

Unit tests cover the framework and packages against a fake host API
(`tests/test_tools.py`), the host runtime and approval lifecycle against Postgres
(`tests/test_tools_host.py`), and the socket service plus the real shim
subprocess (`tests/test_tools_api.py`); the UI flows run in the browser smoke
(`tests/smoke-ui/`). The live AWS smoke exercises the credential-free paths on a
real host: manifest listing, enabling with unset config, the shim as the agent
user, peer-credential rejection, and a Brave call with an invalid key proving the
tools-service egress path (and that the admin uid has none). Live checks that need real credentials run in the stage test
(`tests/stage/stage_aws.py`), gated on the stage Brave key secret and on the
operator having connected the Google tools once in the stage admin UI.
