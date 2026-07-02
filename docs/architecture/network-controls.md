# Network Controls

Defense in depth, fail closed at each layer:

1. **nftables**: inbound is dropped except loopback, established traffic, and
   SSH port 22 when SSH operator access is configured. Outbound is dropped for
   everyone except root, `trustyclaw-proxy`, optional `cloudflared`,
   `systemd-resolved`, and `systemd-timesyncd`; the agent and admin users have
   no direct network path at all. Non-root DNS is blocked even toward the local
   `systemd-resolved` stub (DNS lookups are an exfiltration channel); only
   `systemd-resolved`, the proxy, and optional `cloudflared` may query upstream
   DNS. If the proxy is down, the agent simply has no connectivity.
2. **Proxy environment**: agent processes run with `HTTP_PROXY`/`HTTPS_PROXY`/
   `ALL_PROXY` pointing at the local proxy and trust its CA via the system
   store and `NODE_EXTRA_CA_CERTS`.
3. **Policy proxy**: every request is checked against `network_controls` before
   any upstream DNS resolution or connection happens, so a denied host name is
   never even resolved (host names are otherwise an exfiltration channel).

Deployment config does not include runtime network controls. The active
policy lives in the `network_policy` database row; a missing row (fresh
deploy) is the fail-closed empty default, and a preserved database keeps its
policy across redeploys. Operators then enable managed AI providers or
website/domain rules through the admin UI/API. See
[`../api/NetworkControls.md`](../api/NetworkControls.md) for the runtime policy
schema.

The proxy enforces, per request:

- Domain match — exact rule wins over wildcards, longest wildcard wins; the rule
  must have a non-empty `allow_http_methods`.
- Method against `allow_http_methods`; plain HTTP only on port 80, HTTPS/WSS
  only on port 443.
- `path_guards` regexes against path plus query.
- OpenAI guards: `managed_ai_provider_network_access.openai` expands to the required OpenAI domains,
  denies live web search on the API/data-plane domains (the
  `web_search_preview` tool, or `web_search` without
  `external_web_access: false`) while allowing the cached tool, and requires
  data-plane traffic to match the account id inferred from Codex login status
  (failing closed while that id is unavailable). The agent's Codex runtime is
  also pinned to cached web search via a managed
  `/etc/codex/requirements.toml`, so this guard is a second layer.
- Anthropic guards: `managed_ai_provider_network_access.claude` expands to the
  Claude Code OAuth path on `platform.claude.com` and the Anthropic API domain.
  The API domain fails closed until Claude Code OAuth has produced a locally
  readable account file; after that, API requests must carry the exact bearer
  token whose SHA-256 hash was inferred from the agent user's Claude credentials.
  The proxy reads only that hash, never the bearer token itself.

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

The host firewall accepts outbound traffic from root, from the dedicated
`trustyclaw-proxy` uid, and from the optional `cloudflared` uid. Root egress
covers bootstrap/package installation, security updates, and ordinary root-owned
system traffic. The host does not install or configure the AWS SSM agent, and
snapd is explicitly stopped and masked during bootstrap. Proxy egress is limited
to DNS and TCP 80/443, and only after a request has passed policy. Cloudflare
Tunnel egress is limited to DNS, TCP 443, and TCP/UDP 7844, and the EC2 security
group keeps TCP/UDP 7844 open only when a `cloudflare_access` operator endpoint
is configured. That 7844 allowance is outbound-only and paired with nftables uid
checks: it is usable by the `cloudflared` connector, not by the agent, admin
API, or proxy users. It does not expose an inbound EC2 port. The agent — a
non-root user with no sudo — only inherits root's blanket path by first
escalating to root.

Decisions are logged to the `network_events` database table, which the proxy
writes under its own narrow database role. A denied `CONNECT` (no inner
request was readable) is logged with method `CONNECT`.

Policy replacement (`PUT /v1/network/policy`) validates the body and replaces
the `network_policy` database row, which the proxy role can only read. The
proxy reads and validates the policy per request, with deliberately no
fallback cache: a database outage denies every request until the database
returns, and an invalid stored policy equally denies all requests. Fail
closed in every state.
