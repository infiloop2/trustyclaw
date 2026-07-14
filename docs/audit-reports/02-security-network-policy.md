# Audit: Network Proxy Policy Enforcement

Finding ID prefix: `NET`. See [README.md](README.md) for the sweep process,
entry template, and severity scale.

## Audit question

Does the network proxy enforce the configured network policy without gaps or
loopholes? Is there any implementation bug through which traffic the active
policy does not allow — by domain, method, port, scheme, path guard, or
provider guard — can reach the internet, an internal address, or otherwise
exfiltrate data?

## Threat model

- **Adversary:** malicious agent code as `trustyclaw-agent` that can emit
  arbitrary bytes at the proxy port — malformed HTTP, protocol smuggling,
  odd encodings, WebSocket tricks — and can also drive well-formed traffic
  through the provider SDKs. Assume the agent knows the policy and the proxy
  implementation.
- **Assets:** the guarantee that every byte leaving the agent matches an
  operator-approved rule; the non-resolution of denied host names; the
  unreachability of loopback/link-local/private addresses; the OpenAI and
  Anthropic account pins.
- **Trust boundaries:** the proxy process (`trustyclaw-proxy`) between the
  agent and the internet; nftables as the independent backstop; the
  read-only database role the proxy uses for policy and pins.
- **In scope:** the whole request lifecycle in `host/runtime/network_proxy.py`
  and `host/runtime/network_policy.py` — CONNECT and TLS interposition,
  request parsing (chunked encoding, header folding, smuggling), domain and
  wildcard matching, method/port/scheme checks, `path_guards` regex
  semantics, body buffering limits, WebSocket handshake and tunneling,
  upstream address vetting (DNS rebinding, IPv6, redirects), domain spoofing
  (look-alike/homograph and confusable names, trailing dots, case tricks,
  mismatches between SNI, `Host`, CONNECT target, and the policy-matched
  domain), upstream TLS certificate and hostname verification and the trust
  store it uses, the provider guards and their fail-closed states, policy
  read/validation per request, and the nftables egress rules as the second
  layer.
- **Out of scope:** covert channels that carry no agent-chosen data to an
  agent-chosen endpoint (e.g. timing against an allowed host); cryptographic
  attacks on the TLS protocol itself — everything about *how the proxy
  verifies* the upstream (certificates, hostnames, trust anchors) stays in
  scope; Ubuntu/kernel bugs. Whether the *policy an operator wrote* is wise
  is axis 04's problem — here the policy as stored is the spec.

## Scope checklist

This checklist is not comprehensive: it names known-important areas, but the
audit question and threat model define the scope. Account for each item in
your coverage section, and report anything else within scope even if no item
below names it.

1. Parsing and canonicalization: can two components disagree about the host,
   port, method, or path of the same request (smuggling, absolute-form URIs,
   `Host` vs CONNECT target, percent-encoding, unicode)?
2. Matching semantics: wildcard precedence, empty `allow_http_methods`,
   case sensitivity, trailing dots, IP-literal hosts.
3. Every deny path actually closes the connection without forwarding, and
   denied names are never resolved.
4. Upstream connection: public-routability check ordering vs DNS resolution
   (rebinding), redirect handling, IPv6 and dual-stack answers, and TLS
   certificate/hostname verification against the policy-matched domain.
5. WebSockets: policy at handshake, and what the tunnel permits afterward.
6. Provider guards: OpenAI web-search denial and account pinning, Anthropic
   bearer-hash pinning, all fail-closed windows (missing pin, unreadable
   account file, database down).
7. Resource-exhaustion bypasses: does any overload path fail open?
8. nftables backstop: uid coverage, DNS blocking, the 7844 allowance.

## Key code and docs

- `docs/architecture/network-controls.md`, `docs/api/NetworkControls.md`
- `host/runtime/network_proxy.py`, `host/runtime/network_policy.py`,
  `host/runtime/proxy_state_client.py`
- nftables rules and proxy CA setup in `host/bootstrap/`
- `tests/` for existing proxy coverage (gaps in it are reportable as Info)

## Audit entries

## 2026-07-04 — Claude Opus 4.8 — `f28b50e`

Reviewer: Claude Opus 4.8 (claude-opus-4-8)
Commit: `f28b50e`
Methodology: static code reading of the full proxy request lifecycle and the
policy-matching/provider-guard code; one empirical check of Python's
`ipaddress.is_global` behavior for IPv4-mapped IPv6 on the target interpreter.
No live proxy run or PoC traffic.

### What was reviewed

- `host/runtime/network_proxy.py`: `do_CONNECT` + `_serve_tls_request` (TLS
  interposition), `_proxy_http` (plain HTTP/WS, removed since — the proxy is
  now HTTPS-only), `connect_public` (SSRF vet),
  `host_header_denial`/target-vs-Host consistency, `read_request_head`,
  `read_body`/`read_chunked_body` (smuggling, size caps), `send_http_request`
  (single-request pinning, header stripping), the WebSocket frame guard, and
  the `BoundedThreadingHTTPServer` connection cap.
- `host/runtime/network_policy.py`: `domain_matches`/`find_domain_rule`
  (wildcard precedence, apex exclusion), `decide_http_request` +
  `_normalized_path` (method/path-guard semantics), `openai_request_denied`,
  `anthropic_request_denied`, `_live_web_search_denial`, `_iter_tool_objects`,
  and the bounded gzip/zlib/zstd/brotli decoders.
- `host/config.py` `parse_network_controls`/`expand_network_controls`: method
  uppercasing, domain validation, managed-domain expansion.
- The nftables output chain as the independent backstop.

### Findings

| ID | Status | Severity | Location | Summary |
| --- | --- | --- | --- | --- |
| NET-1 | Fixed | High | `host/runtime/orchestrator.py:217`, `host/runtime/network_policy.py:140` | The managed-provider account pin is sourced from **agent-writable** state, so a malicious agent can redirect it to an attacker-controlled provider account and exfiltrate through the allowed provider domain. For OpenAI, `_seed_runtime_proxy_pin_for_status_check`/`account_status()` derive the account id by running `read-codex-account-id.sh` as `trustyclaw-agent` against `agent-home/.codex/auth.json` and write it into `proxy_provider_pins`; `openai_request_denied` then only checks the request's `ChatGPT-Account-Id` header against that DB pin (and never pins the OpenAI bearer token). There is no operator-controlled anchor for the expected account. A malicious agent overwrites its `auth.json` with valid credentials for an attacker-owned ChatGPT account B, waits for the background poller (`runtime_status_loop`, ≤300s) to re-derive the pin to B, then POSTs arbitrary data to the allowed `api.openai.com`/`chatgpt.com` with `ChatGPT-Account-Id: B` + B's token — the guard passes and the data lands in account B, retrievable by the attacker. The Anthropic guard shares the structure (`read-claude-account.sh` → `access_token_sha256` pin from agent-home credentials), so the same account-redirection exfil applies to Claude. Anchor the pin to operator-approved account state captured at login rather than re-deriving it from agent-home on every refresh, or treat provider domains as untrusted egress under the malicious-agent model. |

I did not find any other policy-bypass path: domain/method/port/scheme/path-guard
matching and the SSRF vet all hold, and no request reaches an upstream the active
policy does not name. The one gap was NET-1 — the provider *account* control,
whose enforcement source was agent-controlled; since fixed via operator-approved
account anchors, with Claude identity server-attested against the token itself.

Specific checks and why they held (with the NET-1 caveat on provider guards):

- **SSRF / DNS rebinding / mapped-IPv6 (verified negative).** `connect_public`
  resolves once, requires *every* resolved address to be `is_global`, then
  connects to the vetted address rather than re-resolving. I specifically
  tested the IPv4-mapped-IPv6 bypass (a malicious `AAAA` of
  `::ffff:169.254.169.254` under a wildcard domain) on Python 3.10.12, the
  Ubuntu 22.04 interpreter the proxy runs under: `is_global` returns `False`
  for `::ffff:169.254.169.254`, `::ffff:127.0.0.1`, and `::ffff:10.0.0.1`, so
  the mapped-address SSRF does not apply here. (This would regress on some
  older 3.9.x/early-3.10 point releases, so it is worth re-checking if the base
  image's Python changes.)
- **Request smuggling / Host confusion.** CONNECT pins one `host` used for the
  policy check, the minted cert, `connect_public`, the upstream SNI, and the
  inner-request Host/target check; absolute-form and Host-header plain
  requests must agree via `host_header_denial`; `send_http_request` strips
  `Content-Length`/`Transfer-Encoding` and re-emits a single `Content-Length`
  with forced `Connection: close`, so no CL.TE/TE.CL desync reaches upstream.
- **Upstream TLS.** `ssl.create_default_context().wrap_socket(server_hostname=host)`
  verifies the upstream certificate chain and hostname against the CONNECT
  target, so a spoofed/look-alike upstream fails the handshake.
- **Path guards.** `_normalized_path` percent-decodes and `posixpath.normpath`
  s before `re.fullmatch`, closing `../` and `%2e%2e`/`%2f` traversal against
  a restrictive guard; the dangerous direction (guard allows but origin
  resolves elsewhere) did not materialize.
- **Provider guards.** The *matching logic* is sound — OpenAI account-id header
  required-and-matched; live web-search denied across gzip/zlib/zstd/brotli
  (bounded, fail-closed decode) and by byte-marker anti-evasion; Anthropic
  bearer-hash pin with a narrow pre-pin GET allowlist; all fail closed when the
  pin/account is unavailable. But the *pinned value itself* comes from
  agent-writable credential files, which is NET-1: the guard confines the header
  to whatever account the agent is logged into, and the agent controls that, so
  it does not confine traffic to an operator-approved account.
- **Fail-closed states.** Missing policy row, unparseable policy, and database
  outage all deny; a decision that cannot be logged fails that request.

### Coverage and confidence

- Checklist 1–3 (parsing/matching/deny paths): covered by reading; the
  matching precedence (exact > longest wildcard) and empty-`allow_http_methods`
  handling were traced against `host_allowed`/`decide_http_request`.
- Checklist 4 (upstream): SSRF vet reproduced for the mapped-IPv6 case only;
  IPv6/dual-stack ordering and redirect handling reviewed by reading (the
  proxy does not follow redirects itself — it forwards the upstream response
  bytes, and each new agent request is policy-checked).
- Checklist 5–6 (WebSockets, provider guards): the client-frame guard (masking
  required, RSV/extension denied, fragmentation, per-message size cap, opaque
  tunnel only when inspection is not required) and both provider guards read in
  full. Tracing the *provenance* of the account pin (not just its matching
  logic) through `orchestrator.refresh_runtime_status` →
  `read-codex-account-id.sh`/`read-claude-account.sh` → `proxy_provider_pins`
  is what surfaced NET-1; an earlier draft of this report wrongly concluded the
  provider guards hold, because I checked the comparison but not the source of
  the pinned value.
- Checklist 7 (overload): no policy path fails *open* under load; the closest
  reliability concern is unbounded proxy memory (64 handlers × 128 MiB buffered
  bodies, proxy not in a memory-limited cgroup) and unbounded per-host cert
  minting under wildcards — both reported as `REL-5` and `REL-2` in
  [05-reliability.md](05-reliability.md), not as policy bypasses.
- Checklist 8 (nftables backstop): output-chain uid rules and non-root DNS drop
  confirmed in bootstrap.
- Low-confidence / not done: I did not drive live traffic or fuzz the header/
  chunk parser, and I did not exhaustively test exotic percent-encoding vs a
  real origin server's path resolution. A running-proxy fuzz of
  `read_request_head`/`read_chunked_body` and the path-guard normalizer would
  raise confidence most.
## 2026-07-04 — GPT-5.5 — `f28b50e87b61`

Reviewer: GPT-5.5 (gpt-5.5)
Commit: `f28b50e87b61507db372d288d971487f55cb2121`
Methodology: static code reading and grep sweeps. I traced HTTP, CONNECT/TLS,
WebSocket, provider-guard, DNS/connect, policy-load, and nftables paths against
the scope checklist. I did not run live proxy fuzzing or packet-level PoCs.

### What was reviewed

- `host/runtime/network_proxy.py`: request parsing, CONNECT prechecks, TLS
  interposition, Host/target validation, body buffering, WebSocket frame
  inspection, upstream DNS/connect, certificate generation, connection caps,
  and event logging.
- `host/runtime/network_policy.py`: domain matching, path normalization,
  HTTP method checks, OpenAI account/web-search guard, Anthropic bearer-hash
  guard, body decoding, and managed-provider expansion call sites.
- `host/config.py`: domain/method/path guard validation and managed provider
  rule expansion.
- `host/runtime/proxy_state_client.py`, `host/runtime/state.py`, and
  `host/migrations/0001_admin_state_schema.sql`: policy storage, proxy grants,
  provider pins, and network event logging.
- `host/bootstrap/bootstrap.sh`: nftables uid rules, proxy service identity,
  proxy CA setup, DNS allowance, and fail-closed defaults.
- Proxy-related tests under `tests/`, especially request parsing, policy,
  provider guard, and smoke/stage coverage.

### Findings

No findings.

### Coverage and confidence

Parsing and canonicalization: I checked plain HTTP absolute-form and
origin-form paths, CONNECT targets, TLS inner request targets, duplicate/missing
Host headers, malformed ports, method normalization, path percent-decoding and
`posixpath.normpath`, chunked dechunking, and stripping of
`Content-Length`/`Transfer-Encoding` before forwarding. I did not fuzz raw byte
streams beyond static parser inspection.

Matching semantics: I checked exact-domain precedence over wildcard matches,
longest wildcard selection, lower-casing, empty method denial through
`host_allowed`, IP literals via policy/domain validation and public-IP checks,
and trailing-dot behavior. A host must match the stored policy shape before DNS
resolution or upstream connect.

Deny paths: I checked that denied CONNECT targets are rejected before
certificate generation and DNS, and that plain HTTP/TLS requests record a deny
event and return 403 without calling `connect_public`. Plain HTTP currently
reads a bounded body before policy denial; that is a reliability finding in
axis 05, not a policy-bypass finding, because the request is still not
forwarded.

Upstream connection: I checked `connect_public` resolves only after policy
allowance, rejects every non-global resolved address before connecting, connects
to the vetted address, and uses default TLS verification with `server_hostname`
for HTTPS/WSS upstreams. Redirect following is absent in the proxy itself.

WebSockets: I checked handshake policy enforcement, removal of extension
offers, client-frame mask and RSV validation, message-size bounds, fragmented
message assembly, OpenAI live-web-search inspection for guarded domains, and
opaque tunneling for domains with no message-dependent guard.

Provider guards: I checked OpenAI `ChatGPT-Account-Id` pinning, missing/mismatch
denials, live `web_search_preview` and `web_search` body denial semantics,
compressed body decoding fail-closed behavior, Claude bearer hash matching,
pre-pin bootstrap read allowances, and database/pin-missing fail-closed states.

nftables: I checked the backstop allows proxy DNS/80/443 egress, blocks
non-root DNS otherwise, lets the agent reach only the loopback proxy port, and
drops other agent loopback/direct egress. Confidence is high for code-level
policy enforcement; lower for kernel/nftables behavior because this sweep did
not run a live host.
