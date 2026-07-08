# Audit: Admin UI Browser Surface

Finding ID prefix: `UI`. See [README.md](README.md) for the sweep process,
entry template, and severity scale.

## Audit question

Is the admin UI safe to run in the operator's browser? It must make no
network call to anything except the admin API that served it — no CDNs,
fonts, analytics, or other third-party fetches. There must be no cookie
vulnerabilities, no cross-site request forgery, and no way for content
rendered in the UI (task output, file previews, network event logs) to run
code or act with the operator's credentials.

## Threat model

- **Adversaries:** (a) the agent, whose output the UI renders — task
  transcripts, file contents and names, proxied host names and paths in
  network events are all attacker-controlled strings; (b) any third-party
  website open in the same browser, which can issue cross-origin requests
  toward the tunneled/forwarded admin endpoint; (c) a network observer,
  where relevant to cookie transport.
- **Assets:** the admin password held by the browser (cookie, sent as bearer
  header), the operator's authenticated session, and the integrity of what
  the operator sees before approving settings.
- **Trust boundaries:** the browser origin of the admin UI; the cookie/bearer
  scheme; the loopback/SSH-forward or Cloudflare Access transport in front
  of `127.0.0.1:7443`.
- **In scope:** `host/runtime/admin_ui.html`, `admin_ui.js`, `admin_ui.css`
  and how `admin_api.py` serves them — every sink where dynamic data enters
  the DOM (`innerHTML` vs text nodes), external references of any kind
  (scripts, styles, fonts, images, prefetch, `fetch` targets), cookie
  attributes and lifetime, CSRF exposure of state-changing endpoints given
  the bearer-header design, response headers (CSP, `X-Content-Type-Options`,
  frame ancestors), MIME handling of agent file previews, and anything
  cacheable that contains secrets.
- **Out of scope:** browser zero-days; compromise of the operator's machine;
  Cloudflare Access itself (its configuration hand-off is in scope for
  axis 04 if it can mislead).

## Scope checklist

This checklist is not comprehensive: it names known-important areas, but the
audit question and threat model define the scope. Account for each item in
your coverage section, and report anything else within scope even if no item
below names it.

1. Grep-level sweep: every `innerHTML`/`insertAdjacentHTML`/template
   construction in `admin_ui.js`, and every URL the page can request.
2. XSS via each agent-controlled string: task output, thread names, file
   names and contents, network event fields, provider metadata JSON.
3. Cookie: flags (`Secure`, `HttpOnly` feasibility, `SameSite`), scope, what
   happens on the plain-HTTP loopback transport.
4. CSRF: confirm no state-changing endpoint accepts cookie-only
   authentication; preflight behavior; any CORS headers emitted.
5. Clickjacking/framing and drag-drop of the UI.
6. The `GET /` page and static assets: verify byte-level absence of external
   origins, not just intent.

## Key code and docs

- `docs/architecture/admin-api.md`
- `host/runtime/admin_ui.html`, `host/runtime/admin_ui.js`,
  `host/runtime/admin_ui.css`
- `host/runtime/admin_api.py` (static serving, auth, headers)

## Audit entries

## 2026-07-04 — Claude Opus 4.8 — `f28b50e`

Reviewer: Claude Opus 4.8 (claude-opus-4-8)
Commit: `f28b50e`
Methodology: static reading of the served HTML/JS and the API's static-asset
and auth handling; enumerated every dynamic DOM sink and every URL the page
can request. No browser-driven test.

### What was reviewed

- `host/runtime/admin_ui.html` (every external reference, inline script/style,
  favicon), `host/runtime/admin_ui.js` (every `innerHTML`/`setHtml` sink, the
  `esc()`/`badge()` helpers, cookie handling, the `api()` fetch wrapper), and
  `host/runtime/admin_ui.css` by reference.
- `host/runtime/admin_api.py`: `_send_ui_asset`, `_authenticate`,
  `SECURITY_HEADERS`, `_send_security_headers`, `_send_json`, cache headers.

### Findings

| ID | Status | Severity | Location | Summary |
| --- | --- | --- | --- | --- |
| UI-1 | Open | Low | `host/runtime/admin_ui.js:150,151` | `badge()` interpolates its argument into markup with no escaping and `esc()` does not escape `"`/`'`; both are currently fed only server-controlled enums / used only in text context, and the page's CSP (`script-src 'self'`, no `unsafe-inline`) blocks injected inline script, so this is a latent footgun rather than a live XSS — but a future caller placing agent-controlled data in an attribute or through `badge()` would reintroduce injection. Escape quotes in `esc()` and route `badge()` through it. |
| UI-2 | Fixed | Low | `host/runtime/admin_ui.js:120` | The admin-password cookie is set without `Secure`, so over the Cloudflare Access (HTTPS) path it can be sent on a downgraded/plain-HTTP request to the same host. It is intentionally omitted so the same cookie works over the plain-HTTP SSH-tunnel transport; consider setting `Secure` when the request arrived over HTTPS. |

Core requirements are met: the UI makes no external network call, has no CSRF
exposure, and has no agent-reachable XSS.

- **No external calls.** The HTML references only same-origin `/admin_ui.css`
  and `/admin_ui.js`, an inline `data:` favicon, and inline SVGs; there are no
  external scripts, styles, fonts, images, or prefetch. Every runtime request
  goes through `api()` to a relative `/v1/...` path. OAuth login URLs are
  rendered as operator-clicked `<a target="_blank">`, not auto-fetched. This
  is enforced in depth by the response CSP `default-src 'self'; connect-src
  'self'; img-src 'self' data:; script-src 'self'; style-src 'self'` plus
  `base-uri 'none'`/`object-src 'none'`.
- **No CSRF.** `_authenticate` accepts only the `Authorization: Bearer`
  header (constant-time SHA-256 compare) and never consults the cookie
  server-side, so a cross-site page cannot authenticate (it cannot set the
  header, and the `SameSite=strict` cookie is not an accepted credential
  anyway). `frame-ancestors 'none'` + `X-Frame-Options: DENY` block
  clickjacking.
- **No agent-reachable XSS.** The genuinely attacker-controlled strings —
  agent file names/paths, file contents, task output, process command lines,
  and proxied hosts/paths in the network log — are rendered via `textContent`/
  `dataset` (the file list) or `esc()` in text (`<pre>`/`<td>`) contexts, none
  in an attribute position. `esc()` neutralizes `<`/`>`/`&` there.

### Coverage and confidence

- Checklist 1 (sink sweep): every `setHtml`/`innerHTML` template in the JS was
  enumerated; the only unescaped or quote-unsafe helpers are `badge()` and
  `esc()` (UI-1), and I traced their callers to confirm none currently pass
  agent-controlled data into an attribute.
- Checklist 2 (per-string XSS): file names/contents, thread/task ids, network
  event fields, process cmdlines, and provider metadata each traced to a safe
  sink.
- Checklist 3–4 (cookie/CSRF): flags reviewed (UI-2 on `Secure`); CSRF ruled
  out structurally by header-only auth.
- Checklist 5 (framing): `frame-ancestors`/`X-Frame-Options` present.
- Checklist 6 (byte-level external-origin check): confirmed by reading the
  served HTML and the static-asset handler; assets are read from disk and
  served with fixed content types and `nosniff`. Not verified against a live
  rendered response or a browser CSP report.
- Not done: no live browser test, no automated CSP evaluator run. Given the
  CSP strength and header-only auth, residual risk is low.
## 2026-07-04 — GPT-5.5 — `f28b50e87b61`

Reviewer: GPT-5.5 (gpt-5.5)
Commit: `f28b50e87b61507db372d288d971487f55cb2121`
Methodology: static code reading and grep sweeps. I enumerated DOM sinks,
external references, fetch targets, cookie handling, auth headers, CSP/security
headers, and agent-controlled data renderers. I did not run browser automation
or a live XSS/CSRF PoC.

### What was reviewed

- `host/runtime/admin_ui.html`: static markup, script/style/icon references,
  login form, tabs, agent task composer, file explorer, policy builder, and
  audit-log tables.
- `host/runtime/admin_ui.js`: every `innerHTML` assignment and template
  renderer, `fetch` wrapper, cookie read/write, OAuth link rendering, agent
  output rendering, file explorer rendering, network event rendering, provider
  metadata rendering, and click delegation.
- `host/runtime/admin_api.py`: UI asset serving, JSON responses, auth, request
  body limits, CSP, referrer policy, frame denial, MIME sniffing header, and
  lack of CORS headers.
- `tests/smoke-ui/admin_ui_smoke.py` and `tests/smoke-ui/run_admin_ui_mock.py`
  for existing UI smoke coverage of malicious-looking strings and layout.

### Findings

| ID | Status | Severity | Location | Summary |
| --- | --- | --- | --- | --- |
| UI-001 | Fixed | Medium | `host/runtime/admin_ui.js:120` | The admin password is stored in a month-long JavaScript-readable cookie with `SameSite=Strict`, but the cookie is not marked `Secure` when the UI is served over a Cloudflare Access HTTPS hostname. If the operator later visits `http://<configured-hostname>` or a downgrade/misconfiguration exposes plain HTTP, the browser can send the admin password cookie over cleartext before any redirect. Keep JavaScript readability if the bearer-header design requires it, but append `; secure` when `location.protocol === "https:"` and clear the cookie with matching attributes. |
| UI-002 | Open | Low | `host/runtime/admin_ui.js:501` | OAuth login links open external provider pages with `target="_blank"` but no explicit `rel="noopener noreferrer"`. Modern browsers generally imply `noopener`, but older/embedded browsers can leave `window.opener` available; a compromised provider login page could navigate the admin tab to a phishing page. Add `rel="noopener noreferrer"` to both OAuth links. |

### Coverage and confidence

Grep-level DOM sweep: I checked every `innerHTML` and dynamic template call.
Dynamic agent-controlled strings pass through `esc()` before insertion, while
the file explorer uses `textContent`/DOM nodes for file names and file content.
Task input/output/error text, task events, network events, provider metadata,
process command lines, thread ids, and file paths were all inspected for either
escaping or text-node insertion. I did not find a direct XSS sink.

External references: the page loads `/admin_ui.css`, `/admin_ui.js`, and a
data-URL favicon; `fetch()` calls use same-origin relative admin API paths.
The intentional external navigations are OAuth links rendered after API calls.
CSP is `default-src 'self'` with `connect-src 'self'`, no third-party scripts,
no external fonts, no analytics, `object-src 'none'`, and
`frame-ancestors 'none'`.

Cookie/CSRF: API requests use an `Authorization: Bearer <password>` header set
by same-origin JavaScript, so cross-site forms/images cannot authenticate and
non-simple cross-origin XHR would need a preflight that the server does not
enable with CORS headers. The cookie is intentionally not `HttpOnly` because
the JS reads it to build that header; UI-001 covers the missing HTTPS `Secure`
attribute.

Static/API headers: UI and JSON responses include CSP, `Referrer-Policy:
no-referrer`, `X-Content-Type-Options: nosniff`, and `X-Frame-Options: DENY`.
I did not verify byte-level browser requests with Playwright or packet capture,
so confidence is high for source-level surfaces and lower for browser-specific
behavior.
