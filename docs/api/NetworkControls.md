# Network Controls

Network controls are runtime policy managed through the admin UI/API, not
deployment config. By default, all network access for the agent is closed. The
host opens network access incrementally through `PUT /v1/network/policy`.

Network controls govern the agent and host service users, not root-owned host
bootstrap and maintenance work. At the host firewall, root (uid 0) has outbound
access for package installation, security updates, and ordinary root-owned
system traffic. The dedicated `trustyclaw-proxy` uid has outbound access only so
it can make policy-approved upstream connections on behalf of the agent. The
separate `trustyclaw-tools` uid has DNS and HTTPS access for bundled tool
packages; tool calls follow each action's data policy and approval contract,
not the agent's domain policy. When
Cloudflare Access operator access is configured, the dedicated `cloudflared` uid
has outbound access only for DNS, TCP `443`, and TCP/UDP `7844`. The host does
not install or configure the AWS SSM agent, and snapd is stopped and masked
during bootstrap. Root, proxy, tools, and optional `cloudflared` egress are still
bounded by the EC2 security group, which keeps TCP/UDP `7844` open only when a
`cloudflare_access` operator endpoint is configured. That `7844` rule is
outbound-only and nftables allows it only for the `cloudflared` uid, not for the
agent, admin API, or proxy users; it does not expose an inbound EC2 port. The
agent runs as a non-root user with no sudo, so it reaches root's broader path
only if it first escalates to root.

Policy is one object: `network_integrations`, keyed by integration id. The
managed provider integrations own exact provider rules and guards; the
`custom` integration holds operator-defined domains for destinations no
provider integration owns. Every reachable host belongs to exactly one
integration.

```json
{
  "network_integrations": {
    "openai": {"enabled": true},
    "claude": {"enabled": true},
    "github": {
      "enabled": true,
      "write_repositories": [
        {"owner": "infiloop2", "repo": "trustyclaw"},
        {"owner": "infiloop2", "repo": "infibot"}
      ]
    },
    "python_packages": {"enabled": true},
    "npm_packages": {"enabled": true},
    "custom": {
      "domains": {
        "api.example.com": {"allow_http_methods": ["GET"], "path_guards": ["^/v1(?:/.*)?$"]}
      }
    }
  }
}
```

| Field | Required | Type | Behavior |
| --- | --- | --- | --- |
| `network_integrations` | No | object | Integration configs keyed by integration id. Known ids are `openai`, `claude`, `github`, `python_packages`, `npm_packages`, and `custom`. The five provider integrations take an `enabled` boolean; `custom` has no `enabled` field and is enabled exactly while its `domains` map is non-empty (passing `enabled` to `custom` is rejected). A missing key or `enabled: false` disables a provider; a disabled integration carries no other state, and serialization omits it entirely. |
| `network_integrations.custom.domains` | No | object | Map of operator-defined domain rules. Keys are exact domains or wildcard suffix domains. Wildcards must start with `*.`, such as `*.example.com`; `*` matches any non-empty hostname prefix ending at that dot. Embedded globs and regex keys are not supported. Domains owned by a provider integration are always rejected here. The custom integration is enabled exactly while this map is non-empty. |

Integration entries are stored exactly as configured. The host parses each
entry directly into that integration's typed config. The proxy selects one
integration for a host and asks its guard to decide the request; it does not
generate a parallel domain-rule or guard-field representation.

## Reserved Managed Domains

Every domain owned by a provider integration is reserved: it is rejected in
`network_integrations.custom.domains` whether or not the integration is
enabled, so a custom rule can never be broader than the integration's guard. The reserved suffixes are
`openai.com`, `chatgpt.com`, `anthropic.com`, `claude.ai`, `claude.com`,
`github.com`, `githubusercontent.com`, `pypi.org`, `pythonhosted.org`,
`npmjs.org`, and `nodejs.org`, including all their subdomains. Manual rules
also cannot set provider-specific guard configuration.

## OpenAI Integration

When `network_integrations.openai.enabled` is `true`, Codex tasks can
run after Codex OAuth login. The OpenAI integration directly enforces:

```json
{
  "api.openai.com": {
    "allow_http_methods": ["POST"]
  },
  "auth.openai.com": {
    "allow_http_methods": ["GET", "POST"]
  },
  "chatgpt.com": {
    "allow_http_methods": ["GET", "POST"]
  }
}
```

The OpenAI external URL request guard (cache-only web search, no remote MCP)
and account guard are always applied to the
managed API/data-plane domains. The host infers the OpenAI account id from
Codex login status instead of accepting it in config. OpenAI data-plane
requests are denied until that inferred account id is available;
`auth.openai.com` stays available for login. Disabling the integration
deactivates the Codex runtime, clears the account pin, closes live runtime
processes, and fails running Codex tasks.

## Claude Integration

When `network_integrations.claude.enabled` is `true`, Claude Code tasks
can run after Claude OAuth login. The Claude integration directly enforces:

```json
{
  "api.anthropic.com": {
    "allow_http_methods": ["GET", "POST"]
  },
  "platform.claude.com": {
    "allow_http_methods": ["GET", "POST"],
    "path_guards": ["^/v1/oauth(?:/.*)?$"]
  }
}
```

The managed bundle opens only `platform.claude.com` OAuth paths plus
`api.anthropic.com`; `claude.ai` stays closed unless a future Claude Code
version proves, by a live denial, that it requires it. The host verifies `claude auth status`, infers
the Claude account metadata from the agent user's Claude config, stores only
account metadata plus a SHA-256 hash of the OAuth access token, and denies
`api.anthropic.com` data-plane requests until the presented bearer token
matches that stored hash. The unauthenticated `/api/hello` readiness probe
remains available for Claude Code startup.

## GitHub Integration

```json
{
  "enabled": true,
  "write_repositories": [
    {"owner": "infiloop2", "repo": "trustyclaw"},
    {"owner": "infiloop2", "repo": "infibot"}
  ]
}
```

| Field | Required | Type | Behavior |
| --- | --- | --- | --- |
| `enabled` | Yes | boolean | Enables the GitHub integration. When enabled, the agent may **read** any repository the injected token reaches (public repositories always; private ones as far as the token allows). When `false`, nothing else may be configured: a non-empty `write_repositories` is rejected, and disabling through the UI removes the configured list (the stored credential is kept). |
| `write_repositories` | No | array | Repositories the agent may also **write** to — git push and mutating REST calls. Repository administration stays denied even for these (see below). Reads are universal and need no listing. Owner/repo are normalized to lowercase; duplicates are rejected. This is also the list the repository audit inspects. Enabling with an empty list gives a read-only agent. |
| `write_repositories[].owner` | Yes | string | GitHub owner (user or organization) identifier. |
| `write_repositories[].repo` | Yes | string | GitHub repository identifier. |
| `require_dot_github_approval` | No | boolean | Default `false`. When `true`, a git push that changes any `.github/` path is held for operator approval instead of reaching GitHub (see [the `.github` approval gate](#the-github-approval-gate)). Requires the integration enabled. |

The access model is **all reads, scoped writes**. An agent's utility comes from
many data-in paths — reading any repository, sample, or reference it needs — so
reads are not restricted. The controlled side is egress: a write must target a
configured `write_repositories` entry, and even for those repositories the
writes that reach past repository content are denied outright: repository
administration (settings, access grants, protections — the full denylist is
under [Access Enforcement](#access-enforcement)), publishing to GitHub Pages,
and dispatching GitHub Actions workflows, which would execute agent-chosen
payloads on GitHub's runners with the repository's secrets, beyond this host's
network controls.

When GitHub is enabled, the GitHub integration directly enforces the following
host and method boundary. Its typed config carries the write-repository list
and `.github` approval toggle:

```json
{
  "github.com": {
    "allow_http_methods": ["GET", "HEAD", "POST"]
  },
  "api.github.com": {
    "allow_http_methods": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
  },
  "uploads.github.com": {
    "allow_http_methods": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
  },
  "codeload.github.com": {"allow_http_methods": ["GET", "HEAD"]},
  "raw.githubusercontent.com": {"allow_http_methods": ["GET", "HEAD"]},
  "objects.githubusercontent.com": {"allow_http_methods": ["GET", "HEAD"]},
  "release-assets.githubusercontent.com": {"allow_http_methods": ["GET", "HEAD"]},
  "github-cloud.githubusercontent.com": {"allow_http_methods": ["GET", "HEAD"]}
}
```

Only the three domains where writes are possible carry the repo guard. The
other five are `GET`/`HEAD`-only, so no write can ever reach them and there is
nothing for a repo guard to gate: the archive and raw-blob hosts serve reads
of any repository, and the signed-URL domains have no owner/repo in their
presigned S3 paths anyway — access control there is the signed URL itself,
and downloads-only closes the other direction (a signed URL can also
authorize an upload).

### Access Enforcement

The guard only ever gates writes; every read passes through. One single
`write_repositories` list drives every guarded GitHub domain. Request paths
are canonicalized before the write check (the mechanics are described in
[../architecture/network-controls.md](../architecture/network-controls.md)),
so an encoded traversal cannot collapse a write path onto a repository the
policy did not list.

`github.com` decisions:

| Request | Class | Allowed when |
| --- | --- | --- |
| `GET`/`HEAD` `/<owner>/<repo>` and subpaths | read | always (integration enabled) |
| `GET` `/<owner>/<repo>[.git]/info/refs?service=git-upload-pack`, `POST` `/<owner>/<repo>[.git]/git-upload-pack` | read (clone/fetch) | always |
| `GET` `/<owner>/<repo>[.git]/info/refs?service=git-receive-pack`, `POST` `/<owner>/<repo>[.git]/git-receive-pack` | write (push) | repository is in `write_repositories` (`github_write_repo_required`) |
| `POST` `/<owner>/<repo>[.git]/info/lfs/objects/batch` | body-inspected: LFS `download` is a read (always allowed); `upload` is always denied (`github_lfs_push_unsupported`) — the follow-up object uploads go to signed URLs whose opaque paths cannot be repo-checked, and the signed-URL domains are deliberately `GET`/`HEAD`-only — so a push with new LFS objects fails at the batch step with a crisp reason instead of mid-transfer; anything else fails closed (`github_lfs_operation_unresolved`) | download always |
| Any other `POST` (web-form mutation) | write | denied (`github_write_repo_required`) — the API is the only mutation surface |

Smart-HTTP `git-upload-pack` is a read even though its data leg uses `POST`
(it only sends want/have negotiation and receives objects). `git-receive-pack`
is always write.

`api.github.com` decisions:

| Request | Class | Allowed when |
| --- | --- | --- |
| `GET`/`HEAD` any path (`/repos/...`, `/search/...`, `/orgs/...`, `/user`, `/rate_limit`, ...) | read | always (integration enabled) |
| `POST` `/graphql` | — | always denied (`github_graphql_denied`) |
| `POST`/`PUT`/`PATCH`/`DELETE` `/repos/<owner>/<repo>/...` | write | repository is in `write_repositories` (`github_write_repo_required`) |
| write methods on `/repos/<owner>/<repo>` exactly (the repo resource: `PATCH` changes settings including `private`→`public`, `DELETE` removes it), on `{forks,generate,transfer}` (escape the repository — fork into the caller's account, template to a new repo, transfer ownership), and on administration sub-resources: `collaborators`, `invitations`, `keys`, `hooks`, `pages`, `environments`, `codespaces`, `dependabot`, `rulesets`, `properties` (custom property values can steer which organization rulesets apply), `interaction-limits`, `releases`, `immutable-releases`, `autolinks`, `topics`, `lfs` (the repository LFS enable/disable toggle), `vulnerability-alerts`, `automated-security-fixes`, `private-vulnerability-reporting`, `code-scanning`, `secret-scanning`, `dependency-graph`, `security-advisories`, `bypass-requests` (approving a push-protection bypass approves the agent's own path around a security gate), `actions/{secrets,variables,runners,permissions,oidc,cache,caches}`, `actions/workflows/*/{enable,disable,dispatches}`, `dispatches`, `statuses`, `check-runs`, `check-suites`, `deployments`, `attestations` (forge supply-chain provenance downstream verifiers trust), `actions/runs/*/{cancel,force-cancel,approve,pending_deployments,deployment_protection_rule}`, `DELETE actions/runs/…`, `actions/artifacts/…`, `branches/*/protection…`, `tags/protection…`, `pulls/*/update-branch` (writes the PR's head branch, which may live in an unlisted fork) | — | always denied, even for a `write_repositories` entry, under one unified reason (`github_repo_admin_write_denied`): these change who or what can reach the repository (access grants, deploy keys, webhooks), mint credentials, publish to the web, dispatch workflows that run agent-chosen payloads on GitHub's runners with the repository's secrets, weaken branch/tag protections (classic protection, rulesets, and the custom properties that select rulesets), turn off security features, erase automation evidence (run and artifact deletion), or forge/stop automation signals (commit statuses, check runs, deployments, run cancellation) that humans and external systems act on — not repository content. **Reads of all of them stay plain reads.** |
| when `require_dot_github_approval` is true: write methods on `contents/.github...`, `git/{refs,trees,commits}`, `merges`, and `pulls/*/merge` | content write with possible `.github` effect | denied (`github_dot_github_rest_write_denied`) unless it goes through the git push approval queue |
| write methods that target no repository at all — `POST /user/repos` (create a repo), `POST /gists` (create a gist, a fresh egress surface), starring, org-level changes | — | denied (`github_write_repo_required`): the write is not one of the configured repositories |

GraphQL is denied entirely: a `POST /graphql` can mutate, and repository
references inside a GraphQL body cannot be verified with path rules — anything
short of a real GraphQL parser can be evaded (argument order, aliased
variables, fragments). `gh` commands that use GraphQL fail with a clear denial
reason; their REST equivalents (`gh api repos/...`, plain `git`) keep working.
A future GraphQL parser can lift this.

Other GitHub domain decisions:

| Domain | Flow and path shape | Class | Allowed when | Notes |
| --- | --- | --- | --- | --- |
| `uploads.github.com` | Release-asset uploads (`gh release upload`, REST *Upload a release asset*) under `/repos/<owner>/<repo>/...` | write | repository is in `write_repositories` | Decided by the same `/repos/<owner>/<repo>` write rule as `api.github.com`. Upload bodies use the normal proxy path, which buffers each request body and rejects bodies over 128 MiB; very large release assets are not supported until the proxy has a streaming path for guard-clean hosts. |
| `codeload.github.com` | Archive downloads such as `/<owner>/<repo>/tar.gz/<ref>` | read | always (integration enabled) | Serves only `GET`/`HEAD`, so every request is an allowed read of any repository. |
| `raw.githubusercontent.com` | Raw file reads such as `/<owner>/<repo>/<ref>/<path>` | read | always (integration enabled) | Serves only `GET`/`HEAD`, so every request is an allowed read of any repository. |
| `objects.githubusercontent.com` | Git object downloads through short-lived signed URLs whose paths are opaque | read | request method is `GET` or `HEAD` | No repo guard is possible because the signed URL path has no owner/repo shape. Writes are never allowed. |
| `release-assets.githubusercontent.com` | Release-asset downloads through short-lived signed URLs whose paths are opaque | read | request method is `GET` or `HEAD` | No repo guard is possible because the signed URL path has no owner/repo shape. Writes are never allowed. |
| `github-cloud.githubusercontent.com` | Git LFS object downloads: an allowed LFS `download` batch returns follow-up `GET` hrefs on this host | read | request method is `GET` or `HEAD` | Same signed-URL shape: opaque paths, no repo guard possible, writes never allowed, Authorization strip-only. |

Every GitHub write denial carries a specific reason in network events —
`github_write_repo_required`, `github_repo_admin_write_denied`,
`github_graphql_denied`, `github_dot_github_rest_write_denied`, `github_lfs_push_unsupported`,
`github_lfs_operation_unresolved` — so an operator can see exactly which rule
fired and which repository would need to be added to `write_repositories`.
One more reason exists as a fail-closed default: `github_repo_scope_required`
fires if the GitHub guard receives a host for which it has no access rules. It
is unreachable with today's fixed dispatch table; it exists so a future wiring
mistake denies instead of allowing.

### The `.github` approval gate

When `require_dot_github_approval` is set, a `git push` to a write repository that
passes the write guard is inspected before it is forwarded. The proxy hands the
buffered `git-receive-pack` body to the push-gate engine (`host/network_integrations/github/push_gate/`), which resolves the thin
pack against a per-repo quarantine mirror (a bare clone under the proxy's state
directory, fetched with the working token) and uses real `git`
(`index-pack` + `diff-tree`) to list the changed paths:

- **No `.github/` change** — the original body is forwarded upstream unchanged.
  Clean pushes are transparent.
- **`.github/` touched** — the pushed objects are retained under
  `refs/pending/<id>` in the quarantine, a `pending_pushes` row is written, and
  the agent's push is answered with a synthesized git `report-status` that
  rejects each ref with "queued for approval as push-<id>" (network event
  reason `github_push_queued_for_approval`). The push fails cleanly; nothing
  reaches GitHub.

The operator lists, approves, or rejects held pushes through the admin API
(`/v1/network-tools/github-pending-pushes`; see
[AdminAPI.md](AdminAPI.md#network)). **Approve** replays the quarantined objects
to GitHub with the working token via the `approve-github-push` root helper (the
admin service has no egress); **reject** drops the pending refs (best-effort)
and marks the row rejected. A replay failure marks the row `failed` with
detail; the recovery is a fresh agent push. Only pkt-line
command framing is parsed in the proxy; pack objects are only ever read by
`git`, and any framing, mirror, or git failure fails closed (the push is denied
with `github_push_gate_unavailable`, never forwarded un-inspected). The
implementation note is in
[../architecture/github-write-path-controls.md](../architecture/github-write-path-controls.md).

### GitHub Credential

There is one fixed GitHub credential, managed through
`PUT /v1/network-tools/github-credential` (see
[AdminAPI.md](AdminAPI.md#network)); it is separate from the write-repository
list and authenticates both reads and writes. Two modes:

| Mode | Operator provides | Behavior |
| --- | --- | --- |
| `pat` | A GitHub fine-grained personal access token. Its scope bounds which private repositories the agent can read and which it can write. | The pasted token is installed as-is. |
| `app` | A GitHub App id, installation id, and the App's PEM private key. | The host mints short-lived, installation-wide tokens and refreshes them before their one-hour expiry. Which repositories the token can reach is bounded by where the App is installed (operator-managed on GitHub). |

Secrets are write-only in both modes: the token and the private key are never
echoed by the UI or returned by any API, and they are encrypted at rest in the
admin database (see
[../architecture/admin-state-storage.md](../architecture/admin-state-storage.md)).
Reads return only metadata
(`configured`, `mode`, app/installation ids, current app-token expiry, last
update time, validation status).

Enablement and credential health are separate concerns: the policy decides
whether GitHub domains are reachable, the credential decides what token the
proxy injects, and the credential's `validation` status reports only its own
health. Any credential failure fails closed: the error is recorded in
`validation`, the working token is withdrawn, and the host retries every
poller cycle until it converges. A policy publish never fails on credential
problems; until the credential converges, GitHub requests simply run
unauthenticated (public reads work, private access fails at GitHub).

The agent never holds the token. The network proxy authenticates GitHub
requests itself: whatever `Authorization` the agent sends on GitHub domains
is stripped, and the active working token is injected after the repository
guard has passed (never on the signed-URL domains, where an Authorization
header breaks the presigned download). There is nothing on the agent side to
copy or exfiltrate, a token smuggled in through a prompt cannot substitute
another identity, and revocation is instant — disabling the integration or
clearing the credential stops injection from the very next request. The
agent's `git` and `gh` (both preinstalled) work with no login and no
configuration; without a credential, public repository reads still work. The
wiring is described in
[../architecture/network-controls.md](../architecture/network-controls.md).

The credential can be stored (or deleted) whether or not the integration is
enabled — staging the credential first and then enabling the policy is the
order that never leaves the proxy allowing writes with no working token.

### Repository audit and warnings

Each configured write repository is audited with the working token (a root
helper makes the GitHub API calls; the admin service has no egress). Reads are
universal, so only write repositories are audited: the warnings are all about
what the agent's pushes and PRs expose, which only write targets can. Facts are
stored in an admin-owned table; warning derivation lives in code, so message
changes need no re-audit. Warnings surface in the credential API response and as
banners in the admin UI.

| Fact | Source | Warning when |
| --- | --- | --- |
| Pages visibility | `GET /repos/{o}/{r}` → `has_pages` (authoritative only when false), then, unless Pages is known disabled, `GET /repos/{o}/{r}/pages` → `public`; a 403/404 on the Pages read leaves the visibility unknown instead of failing the audit, because GitHub answers 404 for missing Pages permission too | **Public Pages site on a private repo:** a push to the Pages source publishes agent-written content to the internet — the same exfiltration sink as a public write repository. **Unknown Pages visibility on a private repo:** warning severity, because the audit could not verify whether Pages is a public leak vector. |
| Visibility | `GET /repos/{o}/{r}` → `private`/`visibility` | **Public write repo:** everything the agent pushes here (branches, commit messages, PRs and descriptions) is world-visible, and because reads are universal an injected agent can copy any private repo the token reaches into a commit, issue, or PR here — a public write target is an exfiltration sink. Highest severity when the token can reach private repositories. |
| Token permissions + default-branch protection | `GET /repos/{o}/{r}` → `permissions`; `GET /repos/{o}/{r}/branches/{default}` → `protected` | `push` present **and** the default branch unprotected (warning severity): the agent identity can push straight to the default branch. Recommend a ruleset/branch protection so the agent only opens PRs and a human merges. |
| Workflows and their triggers | `GET /repos/{o}/{r}/contents/.github/workflows`, then a plain substring search of each file for the dangerous trigger names (false positives over-warn, which is fine) | Any workflow: an agent push or PR can execute agent-written code with whatever secrets and network that repo grants. `pull_request_target` is highest severity — secrets exposed to PR-influenced code. |

Audits warn, never gate: they tell the operator which server-side protections
(rulesets, branch protection, workflow restrictions) to add on GitHub, which
remain the stronger controls because they sit outside the agent's reach. A
re-audit runs on credential set, on a policy publish that changes the GitHub
integration, on a UI re-check, and on a slow daily poll — always with the
same published working token every GitHub request uses. A failed or missing
audit surfaces as a warning and retries on the next poll, and having no working
token while the policy lists write repositories is itself recorded as a warning
on every listed repository — the audit reports what *this* credential can do,
so it never keeps reporting facts computed for a credential that no longer
works.

The proxy is the write-enforcement boundary, whatever the credential itself
would permit: an over-scoped token cannot push to or mutate a repository that
is not in `write_repositories`, and cannot perform repository administration
anywhere. This is why minted app tokens are deliberately not re-scoped to the
write list — narrowing the credential bought little on top of the proxy guard.
Every policy publish that changes the GitHub integration (enablement or the
write-repository list, in either direction) does mint a fresh app token,
though: an installation token covers exactly the repositories granted at mint
time, so it must postdate the list it serves — one rule, no widening
bookkeeping. Publishes that leave the GitHub integration untouched keep the
healthy published token.

## Python Packages Integration

When `network_integrations.python_packages.enabled` is `true`, its
integration directly enforces:

```json
{
  "pypi.org": {
    "allow_http_methods": ["GET", "HEAD"],
    "path_guards": ["^/simple(?:/.*)?$", "^/pypi/[^/]+/json$"]
  },
  "files.pythonhosted.org": {
    "allow_http_methods": ["GET", "HEAD"],
    "path_guards": ["^/packages(?:/.*)?$"]
  }
}
```

Only `GET` and `HEAD` are allowed, so the integration is read-only by
construction; package publishing is not part of it. Package names and versions
do appear in request URLs — we trust these first-party registry domains not to
be a data-exfiltration sink for those URL paths.

## NPM Packages Integration

When `network_integrations.npm_packages.enabled` is `true`, its
integration directly enforces:

```json
{
  "registry.npmjs.org": {
    "allow_http_methods": ["GET", "HEAD"]
  },
  "nodejs.org": {
    "allow_http_methods": ["GET", "HEAD"],
    "path_guards": ["^/dist(?:/.*)?$"]
  }
}
```

As with Python packages, only `GET` and `HEAD` are allowed — read-only by
construction, no publishing — and we trust these first-party registry domains
not to leak data through requested URL paths.

## Domain Rule

Each value in `network_integrations.custom.domains` is a domain rule.

Domain keys must be exact DNS names such as `api.example.com` or wildcard DNS
names such as `*.example.com`. A wildcard must be the first character and must
be followed by `.`, then a concrete DNS suffix. It matches any non-empty prefix
before that suffix, including dots, so `*.example.com` matches
`api.example.com` and `a.b.example.com`, but not `example.com`. If both the apex
domain and its subdomains are needed, configure both `example.com` and
`*.example.com`.

Domain keys are normalized to lowercase. Wildcard domain rules must not
overlap: for example, `*.example.com` and `*.api.example.com` cannot both be
configured, because `x.api.example.com` would match both. Exact domain rules may
coexist with wildcards and override them for that exact hostname.

Prefer exact domains over wildcards. A wildcard lets the agent have the host
resolve any subdomain it chooses (for example `<data>.example.com`), which leaks
that label to the domain's DNS as a low-bandwidth side channel. Only use a
wildcard for a domain whose nameservers you trust not to be an exfiltration
sink.

```json
{
  "allow_http_methods": ["GET", "HEAD"],
  "path_guards": ["^/dist(?:/.*)?$"]
}
```

| Field | Required | Type | Behavior |
| --- | --- | --- | --- |
| `allow_http_methods` | Yes | enum array | HTTP methods allowed for proxied requests to this domain. Valid values are `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, and `DELETE`. An empty array keeps HTTP/HTTPS closed for this domain. |
| `path_guards` | No | string array | Python `re` regular expressions for allowed request targets, evaluated with `re.fullmatch` against the path plus query string when present. If omitted, paths are not restricted beyond the domain and method rule. |

WebSockets use the same domain rule. A WebSocket connection starts with an HTTP
`GET` request that includes an upgrade header, so `GET` must be present in
`allow_http_methods` and the handshake path must pass `path_guards` when path
guards are configured. For `wss://` URLs, proxy `CONNECT` handling is internal
to the host and is not listed in `allow_http_methods`. After the upgrade
succeeds, WebSocket frames continue on the approved connection; they are not
separate HTTP requests. On managed OpenAI domains with the external URL request
guard, each client-to-upstream WebSocket message is additionally inspected with
the same guard as HTTP request bodies; a violating message
closes the connection.

Path guards use Python `re` syntax and must match the full request target path.
For example, `^/dist(?:/.*)?$` allows `/dist` and `/dist/index.js`. If query
strings are allowed, include them in the regex, such as
`^/simple(?:/.*)?(?:\\?.*)?$`.

For Codex, the host also restricts the agent runtime to cached web search, so
the OpenAI proxy guard is the second layer rather than the only one.
