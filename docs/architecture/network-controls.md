# Network Controls

Defense in depth, fail closed at each layer:

1. **nftables**: inbound is dropped except loopback, established traffic, and
   SSH port 22 when SSH operator access is configured. Outbound is dropped for
   everyone except root, `trustyclaw-proxy`, `trustyclaw-tools` (DNS and
   HTTPS only, for the bundled tool packages' third-party APIs — see
   [tools host integration](tools/host-integration.md); `trustyclaw-admin` has
   no egress at all), optional `cloudflared`, `systemd-resolved`, and
   `systemd-timesyncd`, with narrow
   loopback exceptions: the agent may reach only the proxy port, the admin API
   may reach app backend ports, and app service users may answer established
   admin-proxy connections. The agent has no direct network path at all.
   Non-root DNS is blocked even toward the local `systemd-resolved` stub (DNS
   lookups are an exfiltration channel); only `systemd-resolved`, the proxy,
   tools service, and optional `cloudflared` may query upstream DNS. If the
   proxy is down, the agent simply has no connectivity.
2. **Proxy environment**: agent processes run with `HTTP_PROXY`/`HTTPS_PROXY`/
   `ALL_PROXY` pointing at the local proxy and trust its CA via the system
   store and `NODE_EXTRA_CA_CERTS`. Tool-package traffic is separate: it runs
   as `trustyclaw-tools` and uses that service's direct DNS/HTTPS allowance,
   never the agent policy proxy.
3. **Policy proxy**: every request is checked against `network_controls` before
   any upstream DNS resolution or connection happens, so a denied host name is
   never even resolved (host names are otherwise an exfiltration channel).

Deployment config does not include runtime network controls. The active
policy lives in the `network_policy` database row; a missing row (fresh
deploy) is the fail-closed empty default, and a preserved database keeps its
policy across redeploys. Operators then enable managed network integrations or
website/domain rules through the admin UI/API. See
[`../api/NetworkControls.md`](../api/NetworkControls.md) for the runtime policy
schema, the managed integration model, and the GitHub repo-scope decision
tables.

The proxy enforces, per request:

- Domain match — exact rule wins over wildcards, longest wildcard wins; the rule
  must have a non-empty `allow_http_methods`.
- Method against `allow_http_methods`; HTTPS/WSS on port 443 only. Plain HTTP
  is not supported: every request gets a logged 403 before any body read, DNS
  resolution, or upstream connection.
- `path_guards` regexes against path plus query.
- OpenAI guards: `managed_network_integrations.openai` expands to the required OpenAI domains,
  denies requests that would make OpenAI reach an external URL with request
  data (any web-search tool other than `web_search` with
  `external_web_access: false`, Chat Completions search, standalone search
  requests that do not opt into cached retrieval, and remote MCP tools) while
  allowing cache-backed search, and requires
  data-plane traffic to match the account id inferred from Codex login status
  (failing closed while that id is unavailable). The agent's Codex runtime is
  also pinned to cached web search via a managed
  `/etc/codex/requirements.toml`, which also disables Codex app/plugin
  connector surfaces. The proxy remains a second enforcement layer.
- Anthropic guards: `managed_network_integrations.claude` expands to the
  Claude Code OAuth path on `platform.claude.com` and the Anthropic API domain.
  The API domain fails closed until Claude Code OAuth has produced a locally
  readable account file; after that, API requests must carry the exact bearer
  token whose SHA-256 hash was inferred from the agent user's Claude credentials.
  The proxy reads only that hash, never the bearer token itself.
- GitHub guards: `managed_network_integrations.github` expands to the GitHub
  domain set with an all-reads, scoped-writes guard. When enabled, every read
  is allowed (the agent may read any repository the injected token reaches);
  the guard only gates writes, which must target a repository in
  `write_repositories`. Writes that reach past repository content — repository
  administration, forks/generate/transfer, publishing, running code outside the
  proxy — are denied even for a write repository, under one reason
  (`github_repo_admin_write_denied`). GraphQL is denied entirely until a real
  GraphQL parser lands, because a `POST /graphql` can mutate and repository
  references in request bodies cannot be verified with path rules. Denials
  carry write-scope reasons in network events. The decision tables live in
  [`../api/NetworkControls.md`](../api/NetworkControls.md#access-enforcement).
- Package guards: `python_packages` and `npm_packages` expand to fixed
  read-only domain and path rules for PyPI, pythonhosted, the npm registry,
  and Node.js downloads.

The proxy refuses to connect upstream to any address that is not publicly routable
(loopback, link-local, or private ranges), so an allowed domain pointed at an
internal address — by misconfiguration or DNS rebinding — cannot reach host-local
or VPC-internal services through the proxy.

HTTPS and WSS are inspected by terminating client TLS with a per-host certificate
signed by the local proxy CA, then opening a separate verified TLS connection
upstream. Each upstream connection carries exactly one policy-checked request
(`Connection: close`), except WebSockets, which tunnel frames after their
handshake passes policy. Request bodies are buffered for inspection — chunked
bodies are decoded and re-sent with an explicit `Content-Length` — and bodies over
128 MiB are rejected so the policy always sees the complete body.

The host firewall accepts outbound traffic from root, the dedicated
`trustyclaw-proxy` and `trustyclaw-tools` uids, and the optional `cloudflared`
uid. Root egress covers bootstrap/package installation, security updates, and
ordinary root-owned system traffic. The host does not install or configure the
AWS SSM agent, and
snapd is explicitly stopped and masked during bootstrap. Proxy egress is limited
to DNS and TCP 80/443, and only after a request has passed policy. Tools-service
egress is limited to DNS and TCP 443 for the bundled packages' third-party
calls. Cloudflare Tunnel egress is limited to DNS, TCP 443, and TCP/UDP 7844,
and the EC2 security group keeps TCP/UDP 7844 open only when a
`cloudflare_access` operator endpoint
is configured. That 7844 allowance is outbound-only and paired with nftables uid
checks: it is usable by the `cloudflared` connector, not by the agent, admin
API, or proxy users. It does not expose an inbound EC2 port.

Loopback is also uid-scoped. The agent can open new loopback TCP connections
only to the network proxy port. App backend ports are opened only to the
`trustyclaw-admin` uid, and a port-specific drop blocks all other local users
before the general loopback accept. App service users may send established
loopback responses for admin-proxied requests, but may not initiate loopback
connections to the proxy, the browser-facing admin API, other app backends, or
other local listeners. The agent — a non-root user with no sudo — only inherits
root's blanket path by first escalating to root.

Decisions are logged to the `network_events` database table, which the proxy
writes under its own narrow database role. A denied `CONNECT` (no inner
request was readable) is logged with method `CONNECT`.

Policy replacement (`PUT /v1/network/policy`) validates the body and replaces
the normalized policy tables in one transaction; the proxy role can only read
those enforcement inputs. The
proxy reads and validates the policy per request, with deliberately no
fallback cache: a database outage denies every request until the database
returns, and an invalid stored policy equally denies all requests. Fail
closed in every state.

## Internal Guard Fields

Managed integrations are stored as operator intent only. Before enforcement the
proxy expands them, in memory, into domain rules carrying internal guard fields
that are never exposed through the admin API, never accepted in
`allowed_network_access`, and never persisted. This section is the canonical
reference for those fields.

| Internal field | Set by | Implementation |
| --- | --- | --- |
| `allow_http_methods` (generated) | every integration | Same mechanics as manual rules; the integration registry fixes the method list per generated domain (for example `POST` only on `api.openai.com`, `GET`/`HEAD` only on `raw.githubusercontent.com`). |
| `path_guards` (generated) | `claude`, `python_packages`, `npm_packages` | Same regex mechanics as manual rules, with registry-fixed patterns (for example `^/v1/oauth(?:/.*)?$` on `platform.claude.com`, `^/packages(?:/.*)?$` on `files.pythonhosted.org`). Request paths are percent-decoded and dot-segment-normalized before matching so `..` segments cannot escape a guard. |
| `openai_account_guard` | `openai` | Requires a `chatgpt-account-id` header on data-plane requests that is present and equal to the account id inferred from Codex login status (stored as the openai row in `proxy_provider_pins`). Missing id or missing header fails closed. |
| `openai_external_url_request_guard` | `openai` | Denies requests that would make OpenAI reach an external URL with request data. Buffers and decodes the request body (gzip/deflate in-process, decoded output capped at 128 MiB; any other encoding is denied outright), then enforces the rule structurally on the parsed JSON: a web-search-family tool object must be exactly `web_search` with `external_web_access: false` (preview and dated variants always browse live and are denied), Chat Completions search (`web_search_options`, `*-search*` models) is denied outright, remote MCP tools (`type: mcp` by `server_url` or hosted `connector_id`, or a `server_url` key anywhere) are denied outright, and the Codex standalone search endpoints (`/backend-api/codex/alpha/search`, `/v1/alpha/search`) must set `settings.external_web_access: false` because the server default there is live. Prompt text mentioning a tool name carries no capability and never matches; JSON-looking bodies that fail to parse are denied. Also applied to each client-to-upstream WebSocket message on guarded domains. |
| `anthropic_account_guard` | `claude` | Denies `api.anthropic.com` requests unless the presented bearer token's SHA-256 hash equals the hash stored in the claude `proxy_provider_pins` row after Claude OAuth. Allows the unauthenticated `GET /api/hello` readiness probe and a fixed set of bearer-authenticated pre-pin bootstrap `GET` paths. The proxy stores and compares only the hash. |
| `github_repo_guard` | `github` | Carries the normalized `write_repositories` list and `require_dot_github_approval` toggle. Applies the access decision tables — every read allowed on `github.com` web/smart-HTTP, `api.github.com` REST, and `codeload`/`raw`; writes gated to the write list, with repository administration denied outright, GraphQL denied outright, and REST content-write bypasses denied when `.github` approval is required. The signed-URL domains carry no guard: they expand to plain `GET`/`HEAD` rules (presigned S3 paths have no owner/repo; the signed URL is the access control). Runs after the generic domain/method/path checks, before upstream connection, with no DNS dependency. |

### Path canonicalization

Every guarded path — `path_guards` regexes and the GitHub repository match
alike — is canonicalized before matching (`_normalized_path`): one
percent-decode pass, then dot-segment collapse (`posixpath.normpath`), with
the leading slash restored and a trailing slash preserved. The principle is
that the guard must evaluate the path in the form the upstream server will
resolve, because any difference between what the guard matches and what the
server serves is a bypass:

- **Percent-decoding** defeats encoding differentials. GitHub decodes
  `%XX` escapes before routing, so `/repos/infiloop2/%74rustyclaw` reaches
  the same resource as `/repos/infiloop2/trustyclaw`; a raw-string
  comparison would let an encoded spelling dodge (or dress up) the repo
  match.
- **Dot-segment collapse** defeats traversal. A naive prefix check on
  `/repos/listed/repo/../../other/secret/contents` sees a listed repo, while
  the server resolves the `..` segments and serves `other/secret`.
  Decoding runs first, so `%2e%2e` becomes `..` and is then collapsed —
  the two steps compose against encoded traversal.
- The GitHub guard additionally strips one optional **`.git` suffix** from
  the repository path segment. This is compatibility, not security: git's
  smart-HTTP endpoints use `owner/repo.git/...` while the web UI and REST
  API use `owner/repo`, and the policy stores repositories normalized
  without the suffix, so both spellings must land on the same policy row.


Guard inputs that are secrets or account pins live where the proxy can read
no more than it needs: the OpenAI/Claude pins are the two comparison values in
the `proxy_provider_pins` table (SELECT-only for the proxy role), and the
GitHub credential — a pasted PAT or a GitHub App key with its minted
installation tokens — lives in the admin-owned `github_credential` table with
no proxy grant at all, because the network guard only decides repository
reachability and never needs the secret; its secret columns are additionally
encrypted at rest (key in the `secret_keys` table, so a stray read of the
credential table alone reveals nothing). The admin API exposes at most
credential metadata (`configured`, mode, app/installation ids, expiry,
validation status), never token or key material.

The active working token lives in the proxy-readable `proxy_github_token`
row (the `proxy_provider_pins` pattern): the admin service publishes it on
mint/set/replace and clears it on disable or delete, while the credential row
itself — App PEM key, PAT storage — keeps no proxy grant. The row holds
`secretbox` ciphertext like every other stored secret, and the proxy also
holds SELECT on `secret_keys` to decrypt it; grants are per-table, so key
plus row decrypt exactly the proxy's working set and nothing else — in app
mode a short-lived installation token, refreshed hourly; in pat mode the PAT
itself, one reason to prefer app mode. Two narrow root helpers
carry the egress the admin service does not have: `mint-github-app-token`
(short-lived, installation-wide App tokens) and `audit-github-repo` (the
repository facts behind the operator warnings). Minted tokens are
deliberately not scoped to the policy's repository list — the
`github_repo_guard` above is the per-repository boundary on every request,
and the App installation bounds what the token could reach if the proxy were
bypassed.

The proxy injects the credential per request: on the GitHub auth domains
(`github.com`, `api.github.com`, `uploads.github.com`,
`codeload.github.com`, `raw.githubusercontent.com`) it strips whatever
`Authorization` the agent sent and adds the working token after the repo
guard has passed — as `Bearer` on the REST hosts and as the Basic password
(username `x-access-token`, GitHub's convention for tokens over git smart
HTTP) on the git/web hosts, and only ever inside TLS (the plain-HTTP proxy
path strips agent auth but never injects); on the signed-URL domains it only strips (an Authorization
header breaks a presigned download, and the signed URL is the access control
there). The proxy already terminates TLS on every GitHub request and could
read a bearer token in transit, so injection gives a compromised proxy
nothing it could not already see — what it removes is the **agent's** copy,
and with it the exfiltration channel, the smuggled-credential identity swap,
and the whole agent-file lifecycle (install/remove, staging windows,
credential helpers). git and gh both come from the Ubuntu archive at
bootstrap; git simply sends unauthenticated requests that arrive at GitHub
authenticated, and `/usr/local/bin/gh` is a shim that supplies a fixed
placeholder `GH_TOKEN` (gh refuses to run authenticated calls without one)
that never reaches GitHub. Because the token is applied in transit, a
rotation or a revocation reaches every process — however warm — on its very
next request, and disabling the integration is one row delete with nothing
to uninstall on the agent side.

Each configured repository is additionally audited through
`audit-github-repo` using the working token — visibility, the token's own
effective permissions, default-branch protection, workflows and their
triggers — into the admin-owned `github_repo_audit` table. Facts live in the
database, judgments in code (`github_repo_audit._warnings`), so message
changes never need a re-fetch. Refreshes are forced after credential and
repository-list changes and by the UI re-check action, and TTL-gated (daily)
from the poller; audits warn, never gate.

The admin service converges the working-token row with one
`reconcile()` path — after every credential or policy change and from the
orchestrator poller each cycle. Enablement and credential health are separate
concerns: the policy decides reachability, the credential row decides the
token, and they meet only in reconcile's convergence rule — the working
token is published exactly while GitHub is enabled with a credential stored,
absent otherwise. A credential change or any publish that changes the GitHub
integration, in either direction, mints a fresh App token — installation
tokens carry the repositories *and App permissions* granted at mint time, so
the token must postdate whatever was just granted; only the poller and
unrelated policy edits reuse the cached token until it nears its one-hour
expiry. Failure handling is one rule, fail closed: any failure
records itself in the credential's validation status, withdraws the working
token and the cached mint — a token that may not match the stored credential
or the published repository list must never stay injectable — and is retried
on the next poller cycle. Until it converges, git and gh simply run
unauthenticated — fail closed; the poller's fixed cadence (well inside the
App token's refresh margin) is the one retry path.
