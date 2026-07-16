"""Network policy access and request decisions.

The active policy lives in the ``network_policy`` database row. The admin
service (schema owner) replaces it after validation; the proxy role can only
read it. A missing row is the fail-closed empty default, and a database
outage denies every request (no fallback cache; see ``network_proxy``).
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import posixpath
import re
import zlib
from typing import Any
from urllib.parse import parse_qs, unquote

from host.config import parse_network_controls
from host.runtime import github_push_gate
from host.runtime.state import (
    enqueue_pending_push,
    network_policy_record,
    read_proxy_claude_account,
    read_proxy_github_token,
    read_proxy_openai_account_id,
)

# Codex standalone web search endpoints (code-mode models search here instead
# of declaring a Responses web_search tool). The request must opt into cached
# retrieval via settings.external_web_access; the server default is live.
OPENAI_SEARCH_PATHS = {"/backend-api/codex/alpha/search", "/v1/alpha/search"}
# Hosted tools that make the upstream reach the open web, run server-side code
# with network egress, or drive a remote browser, but whose ``type`` does not
# begin with ``web``. They have no cache-backed form here and are denied
# outright, like ``web_search_preview``. code_interpreter is included because
# OpenAI's hosted container can egress; Codex runs code in its own local
# sandbox and never declares it, so denying it costs nothing and fails closed.
_DENIED_HOSTED_TOOL_TYPES = frozenset(
    {"browser", "computer_use", "computer_use_preview", "code_interpreter"}
)
ANTHROPIC_PRE_PIN_BOOTSTRAP_GET_PATHS = {
    "/api/oauth/profile",
    "/api/oauth/claude_cli/roles",
    "/api/organization/claude_code_first_token_date",
    "/api/claude_code/policy_limits",
    "/api/claude_code/settings",
}
# Matches the proxy's MAX_BODY_BYTES: decompressed output is capped so a
# decompression bomb cannot exhaust the proxy's memory.
MAX_DECODED_BODY_BYTES = 128 * 1024 * 1024


def load_policy() -> dict[str, Any]:
    record = network_policy_record()
    if record is None:
        return {"managed_network_integrations": {}, "allowed_network_access": {}}
    controls = record["controls"]
    return controls if isinstance(controls, dict) else {}


def managed_integration(name: str, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """One managed integration's stored dict ({} when absent) — the single
    reader every consumer derives from instead of re-parsing the policy."""
    if policy is None:
        policy = load_policy()
    integrations = policy.get("managed_network_integrations")
    if not isinstance(integrations, dict):
        return {}
    integration = integrations.get(name)
    return integration if isinstance(integration, dict) else {}


def network_policy_response() -> dict[str, Any]:
    """The operator-facing policy view: stored controls plus updated_at."""
    record = network_policy_record()
    if record is None:
        return {
            "network_controls": {"managed_network_integrations": {}, "allowed_network_access": {}},
            "updated_at": None,
        }
    controls = record["controls"]
    return {
        "network_controls": controls if isinstance(controls, dict) else {},
        "updated_at": record["updated_at"],
    }


def network_status() -> str:
    """Whether the stored policy is enforceable. Any failure — an unreadable
    or unavailable policy table, malformed controls — is the degraded status:
    /v1/health must report it, not 500 (the same any-failure-denies posture
    as the proxy's own policy load)."""
    try:
        parse_network_controls(load_policy())
    except Exception:
        return "error"
    return "active"


def domain_matches(pattern: str, host: str) -> bool:
    pattern = pattern.lower()
    host = host.lower()
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and host != pattern[2:]
    return host == pattern


def find_domain_rule(policy: dict[str, Any], host: str) -> dict[str, Any] | None:
    """Return the rule for ``host``: an exact domain wins over wildcards, and
    the longest matching wildcard wins among wildcards."""
    rules = policy.get("allowed_network_access", {})
    exact = rules.get(host.lower())
    if exact is not None:
        return exact
    wildcards = [domain for domain in rules if domain.startswith("*.") and domain_matches(domain, host)]
    if not wildcards:
        return None
    return rules[max(wildcards, key=len)]


def host_allowed(policy: dict[str, Any], host: str) -> bool:
    """Whether any request to ``host`` could be allowed. Checked before the
    proxy resolves DNS or opens an upstream connection, so denied hosts are
    never even resolved."""
    rule = find_domain_rule(policy, host)
    return bool(rule and rule.get("allow_http_methods"))


def decide_http_request(
    policy: dict[str, Any],
    protocol: str,
    method: str,
    host: str,
    path: str,
    query: str,
) -> bool:
    rule = find_domain_rule(policy, host)
    if rule is None:
        return False
    if method.upper() not in rule.get("allow_http_methods", []):
        return False
    path_guards = rule.get("path_guards") or []
    if not path_guards:
        return True
    # Match against the path the origin server will actually resolve: decode
    # percent-escapes and collapse ./ and ../ first, so a guard like
    # ^/v1/threads/.* cannot be bypassed with /v1/threads/../../admin (which the
    # upstream would serve as /admin).
    target = _normalized_path(path) + (f"?{query}" if query else "")
    return any(re.fullmatch(pattern, target) for pattern in path_guards)


def _normalized_path(path: str) -> str:
    decoded = unquote(path or "/")
    normalized = posixpath.normpath(decoded)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if path.endswith("/") and not normalized.endswith("/"):
        normalized += "/"
    return normalized


def openai_request_denied(
    policy: dict[str, Any],
    host: str,
    headers: list[tuple[str, str]],
    body: bytes,
    path: str | None = None,
) -> str | None:
    """Apply the OpenAI-specific domain controls. Returns a denial reason, or
    None when the request passes (including for non-OpenAI domains)."""
    rule = find_domain_rule(policy, host) or {}
    if rule.get("openai_account_guard"):
        account_id = read_proxy_openai_account_id()
        if not account_id:
            return "OpenAI account id is not available"
        # The header must be present AND match. A missing header is a denial:
        # otherwise the agent omits it and OpenAI resolves the account from the
        # token, defeating the pin.
        presented = [value for key, value in headers if key.lower() == "chatgpt-account-id"]
        if not presented:
            return "OpenAI account id header is required for this domain"
        if any(value != account_id for value in presented):
            return f"OpenAI account {presented[0]!r} is not the configured account"
    if rule.get("openai_external_url_request_guard"):
        return _external_url_request_denial(headers, body, path)
    return None


def anthropic_request_denied(
    policy: dict[str, Any],
    method: str,
    host: str,
    path: str,
    headers: list[tuple[str, str]],
    body: bytes = b"",
) -> str | None:
    """Apply Claude/Anthropic account and server-tool controls.

    Claude Code OAuth requests to api.anthropic.com use opaque bearer tokens and
    do not carry an OpenAI-style account header. The enforceable pin is
    therefore the bearer credential hash read from the agent user's Claude
    credentials after login. A tiny unauthenticated readiness path is allowed
    before the pin because Claude Code probes it during startup.

    Separately, Messages API requests may declare Anthropic server-side tools
    that run on Anthropic's infrastructure and reach external URLs with request
    data — web search (``web_search_*``), server-side web fetch (``web_fetch_*``),
    code execution (``code_execution_*``), and remote MCP servers. The client's
    WebFetch/Bash egress is already gated by the domain allow-list, but these
    execute off-box, so the only enforcement point is the request that declares
    them; ``anthropic_external_url_request_guard`` denies them structurally.
    """
    rule = find_domain_rule(policy, host) or {}
    if rule.get("anthropic_external_url_request_guard"):
        reason = _anthropic_server_tool_denial(headers, body, bool(rule.get("anthropic_allow_web_search")))
        if reason is not None:
            return reason
    if not rule.get("anthropic_account_guard"):
        return None
    if method.upper() == "GET" and path == "/api/hello":
        return None
    presented = [
        bearer for key, value in headers
        if key.lower() == "authorization"
        for bearer in [_bearer_token(value)]
        if bearer is not None
    ]
    account = read_proxy_claude_account()
    expected_hash = account.get("access_token_sha256")
    if not isinstance(expected_hash, str) or not expected_hash:
        if _anthropic_pre_pin_bootstrap_allowed(method, path, presented):
            return None
        return "Claude account token is not available"
    if not presented:
        return "Claude bearer token is required for this domain"
    if any(hashlib.sha256(value.encode()).hexdigest() != expected_hash for value in presented):
        return "Claude bearer token does not match the configured account"
    return None


def _anthropic_server_tool_denial(
    headers: list[tuple[str, str]], body: bytes, allow_web_search: bool
) -> str | None:
    """Deny a Messages API request that declares an Anthropic server-side tool
    reaching an external URL or running code off-box. Mirrors the OpenAI body
    guard: decode, confirm the body parses as JSON, then enforce structurally.
    Web search is allowed only when the operator opted in (``allow_web_search``);
    server web fetch, code execution, and remote MCP are always denied. A body
    that cannot be decoded or parsed as declared fails closed."""
    if not body:
        return None
    header_map = {key.lower(): value for key, value in headers}
    decoded = _decode_body(body, header_map.get("content-encoding", ""))
    if decoded is None:
        return "request body could not be decoded for web tool inspection"
    body = decoded
    content_type = header_map.get("content-type", "").split(";", 1)[0].strip().lower()
    looks_json = content_type == "application/json" or body.lstrip().startswith((b"{", b"["))
    if not looks_json:
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "request body is not valid JSON"
    return _anthropic_tool_violation(payload, allow_web_search)


def _anthropic_tool_violation(payload: Any, allow_web_search: bool) -> str | None:
    """The Messages API declares tools in a top-level ``tools`` array and remote
    MCP servers in a top-level ``mcp_servers`` array. Deny the server-side,
    off-box tool families by ``type`` prefix (dated variants such as
    ``web_search_20260209`` share the prefix); client-executed built-ins
    (``bash_*``, ``text_editor_*``, ``memory_*``) and user-defined tools (which
    carry a ``name`` but no ``type``) do not match and pass. ``web_search`` is
    permitted only when the operator enabled it; the others are always denied."""
    if not isinstance(payload, dict):
        return None
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_type = tool.get("type")
            if not isinstance(tool_type, str):
                continue
            if tool_type.startswith("web_search"):
                if not allow_web_search:
                    return "web search is disabled by operator policy for this deployment"
            elif tool_type.startswith(("web_fetch", "code_execution")):
                return f"server-side {tool_type} tool is disabled for this domain"
    mcp_servers = payload.get("mcp_servers")
    if isinstance(mcp_servers, list) and mcp_servers:
        return "remote MCP servers are disabled for this domain"
    return None


# The domains where the proxy injects the active GitHub credential after the
# repo guard has passed, keyed by the auth scheme each host expects: git
# smart HTTP (and the web/raw/codeload hosts) authenticate tokens as the
# Basic password with GitHub's conventional x-access-token username, while
# the REST hosts take Bearer. The signed-URL domains are strip-only: an
# Authorization header actively breaks a presigned download, and the signed
# URL is the access control there.
GITHUB_BEARER_DOMAINS = {"api.github.com", "uploads.github.com"}
GITHUB_BASIC_DOMAINS = {"github.com", "codeload.github.com", "raw.githubusercontent.com"}
GITHUB_STRIP_ONLY_DOMAINS = {
    "github-cloud.githubusercontent.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


def github_credential_headers(host: str, headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Credential injection: on GitHub domains, strip whatever
    ``Authorization`` the agent sent and inject the active working token
    (``proxy_github_token`` row) instead. The agent never holds the
    credential — there is nothing to copy or exfiltrate, revocation is one
    row delete — and an agent-smuggled token cannot substitute another
    identity. Runs only after the request passed the repo guard; raw rules
    for these domains are rejected at validation, so every allowed request
    here came through the managed integration. Without a working token the
    request goes upstream unauthenticated (public reads work, private access
    gets GitHub's plain 401)."""
    lowered = host.lower()
    if lowered not in GITHUB_BEARER_DOMAINS and lowered not in GITHUB_BASIC_DOMAINS and lowered not in GITHUB_STRIP_ONLY_DOMAINS:
        return headers
    headers = [(key, value) for key, value in headers if key.lower() != "authorization"]
    if lowered in GITHUB_STRIP_ONLY_DOMAINS:
        return headers
    token = read_proxy_github_token()
    if not token:
        return headers
    if lowered in GITHUB_BEARER_DOMAINS:
        headers.append(("Authorization", f"Bearer {token}"))
    else:
        credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        headers.append(("Authorization", f"Basic {credentials}"))
    return headers


def github_request_denied(
    policy: dict[str, Any],
    method: str,
    host: str,
    path: str,
    query: str,
    body: bytes,
) -> str | None:
    """GitHub access model: when the integration is enabled, every read is
    allowed (an agent's utility comes from many data-in paths — read any repo,
    public or private, that the injected token reaches). The controlled side
    is writes: a mutation must target a configured write repository, and the
    subset that reaches past repository content is denied outright. So the
    guard only ever gates writes; reads pass through."""
    rule = find_domain_rule(policy, host) or {}
    guard = rule.get("github_repo_guard")
    if not isinstance(guard, dict):
        return None
    # The guard is only ever produced by config expansion from validated
    # typed GitHubRepository entries (raw rules on GitHub domains are rejected
    # at validation), so the shape is trusted; a malformed guard raises and
    # the proxy's fail-closed handling denies the request.
    write_repos = {(item["owner"], item["repo"]) for item in guard.get("write_repositories") or []}
    require_approval = guard.get("require_dot_github_approval") is True
    method = method.upper()
    host = host.lower()
    path = _normalized_path(path)
    if host == "github.com":
        return _github_com_request_denied(write_repos, method, path, query, body)
    if host == "api.github.com":
        return _github_api_request_denied(write_repos, method, path, host=host, require_approval=require_approval)
    if host == "uploads.github.com":
        return _github_api_request_denied(write_repos, method, path, host=host, require_approval=False)
    return "github_repo_scope_required"


def github_push_gate_response(
    policy: dict[str, Any], method: str, host: str, path: str, body: bytes
) -> tuple[bytes | None, str | None]:
    """The ``.github`` approval gate, applied after a push has passed the write
    guard. Returns ``(response, reason)``:

    - ``(None, None)`` — not a gated push, or a clean one: forward normally.
    - ``(bytes, reason)`` — send ``bytes`` (a git ``report-status``) back to the
      agent instead of forwarding; the push is queued for approval.
    - ``(None, reason)`` — fail closed with a plain proxy denial (the push could
      not be parsed or inspected).
    """
    rule = find_domain_rule(policy, host) or {}
    guard = rule.get("github_repo_guard")
    if not isinstance(guard, dict) or guard.get("require_dot_github_approval") is not True:
        return None, None
    # Trigger: a receive-pack POST to github.com. The write guard has already
    # run (the proxy calls this only after request_denial_reason returned
    # None), so the target is a configured write repository by construction —
    # no re-matching here.
    parts = [part for part in _normalized_path(path).split("/") if part]
    if host.lower() != "github.com" or method.upper() != "POST" or len(parts) != 3 or parts[2] != "git-receive-pack":
        return None, None
    owner, repo = parts[0].lower(), parts[1].removesuffix(".git").lower()
    try:
        result = github_push_gate.inspect(owner, repo, body, read_proxy_github_token())
    except github_push_gate.GateError:
        return None, "github_push_gate_unavailable"
    if not result.touches_github:
        return None, None  # nothing under .github/ changed: forward the push
    push_id = github_push_gate.new_push_id()
    try:
        response = result.hold_for_approval(push_id)
        enqueue_pending_push(push_id, owner, repo, result.ref_updates, sorted(result.paths))
    except Exception:  # noqa: BLE001 - any quarantine/enqueue failure fails closed
        try:
            result.cleanup_pending(push_id)
        except Exception:
            pass
        return None, "github_push_gate_unavailable"
    return response, "github_push_queued_for_approval"


def _github_repo_writable(write_repos: set[tuple[str, str]], owner: str, repo: str) -> bool:
    return (owner.lower(), repo.removesuffix(".git").lower()) in write_repos


def _github_repo_from_path(path: str) -> tuple[str, str] | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0].lower(), parts[1].removesuffix(".git").lower()


def _github_com_request_denied(
    write_repos: set[tuple[str, str]],
    method: str,
    path: str,
    query: str,
    body: bytes,
) -> str | None:
    repo = _github_repo_from_path(path)
    service = parse_qs(query).get("service", [""])[0]
    suffix = "/".join(part for part in path.split("/")[3:] if part) if repo is not None else ""
    # Git push: gated on a configured write repository, at both steps of the
    # smart-HTTP push (the ref-advertisement probe and the pack upload).
    if repo is not None and (
        (method == "GET" and suffix == "info/refs" and service == "git-receive-pack")
        or (method == "POST" and suffix == "git-receive-pack")
    ):
        return None if _github_repo_writable(write_repos, *repo) else "github_write_repo_required"
    # Git LFS negotiates through the batch endpoint; only download (clone/fetch)
    # passes, upload is denied (see helper).
    if method == "POST" and suffix == "info/lfs/objects/batch":
        return _github_lfs_batch_denied(body)
    # Everything else on github.com is a read — git fetch (upload-pack), web
    # pages, compare views, raw blobs — an allowed data-in path.
    if method in {"GET", "HEAD"}:
        return None
    if method == "POST" and suffix == "git-upload-pack":
        return None
    return "github_write_repo_required"


def _github_lfs_batch_denied(body: bytes) -> str | None:
    """Git LFS transfers negotiate through ``info/lfs/objects/batch`` on the
    repo path. ``download`` batches (clone/fetch) are a read and pass. ``upload``
    batches are denied at the batch step — the follow-up object PUTs would go to
    signed URLs whose opaque paths cannot be repo-checked, and GET/HEAD-only on
    those domains is a deliberate boundary — so a push with new LFS objects fails
    immediately with a crisp denial instead of mid-transfer. Anything unparseable
    fails closed. Other LFS endpoints (locks, verify) stay denied until a need is
    proven by a live denial."""
    try:
        payload = json.loads(body or b"")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "github_lfs_operation_unresolved"
    operation = payload.get("operation") if isinstance(payload, dict) else None
    if operation == "download":
        return None
    if operation == "upload":
        return "github_lfs_push_unsupported"
    return "github_lfs_operation_unresolved"


def _github_api_request_denied(
    write_repos: set[tuple[str, str]],
    method: str,
    path: str,
    *,
    host: str,
    require_approval: bool,
) -> str | None:
    # Every read is allowed; the token (if any) bounds what private data is
    # visible. GraphQL is the exception: a POST that can mutate and cannot be
    # checked with path rules (argument order, aliased variables, fragments,
    # and string escapes all evade anything short of a real parser), so it
    # fails closed. REST covers the same repo-scoped operations.
    if method in {"GET", "HEAD"}:
        return None
    if method == "POST" and path == "/graphql":
        return "github_graphql_denied"
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return "github_write_repo_required"
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "repos":
        owner, repo = parts[1].lower(), parts[2].removesuffix(".git").lower()
        denial = _github_repo_admin_write_denied(parts, method, host=host)
        if denial is not None:
            return denial
        if not _github_repo_writable(write_repos, owner, repo):
            return "github_write_repo_required"
        if require_approval:
            dot_github_denial = _github_dot_github_rest_write_denied(parts, method)
            if dot_github_denial is not None:
                return dot_github_denial
        return None
    # A mutation that targets no repository at all — creating a repo or gist
    # (a fresh egress surface), starring, org-level changes — is never one of
    # the configured write repositories.
    return "github_write_repo_required"


def _github_dot_github_rest_write_denied(parts: list[str], method: str) -> str | None:
    """REST writes that can create a .github-changing commit without entering
    git-receive-pack. Path-specific content writes are denied only when the path
    is under .github; lower-level object/ref and merge APIs are denied as
    opaque content-moving surfaces while approval mode is enabled."""
    if len(parts) < 4:
        return None
    sub = parts[3]
    if sub == "contents" and len(parts) >= 5:
        content_path = "/".join(parts[4:])
        if content_path == ".github" or content_path.startswith(".github/"):
            return "github_dot_github_rest_write_denied"
    if sub == "git" and len(parts) >= 5 and parts[4] in {"commits", "refs", "trees"}:
        return "github_dot_github_rest_write_denied"
    if sub in {"merges", "merge-upstream"}:
        return "github_dot_github_rest_write_denied"
    if sub == "pulls" and method == "PUT" and len(parts) >= 6 and parts[5] == "merge":
        return "github_dot_github_rest_write_denied"
    return None


def _github_repo_admin_write_denied(parts: list[str], method: str, *, host: str) -> str | None:
    """Writes that reach past repository content are denied even for a
    configured write repository — reads of all of them stay plain repo reads.
    ``parts`` is the split api.github.com/uploads.github.com path with
    ``parts[:3] == ['repos', owner, repo]``. These mutations change who or what
    can reach the repository (access grants, deploy keys, webhooks), mint
    credentials, publish to the web, run code outside the proxy, weaken
    branch/tag protection (classic and rulesets), turn off security features,
    or forge/stop automation signals (commit statuses, check runs, deployments,
    run cancellation) that humans and external systems act on — not repository
    content."""
    if len(parts) == 3:
        # /repos/<owner>/<repo> itself: PATCH changes settings including
        # private -> public visibility, DELETE removes the repository. Agent
        # writes always target a sub-resource (>=4 segments).
        return "github_repo_admin_write_denied"
    sub = parts[3]
    if sub in {
        # Escape the repository entirely: fork into the caller's account,
        # template to a new repo, move ownership.
        "forks", "generate", "transfer",
        # Access grants, credentials, exfiltration/persistence, publish, and
        # compute or security administration. properties: custom property
        # values can steer which organization rulesets apply, so writing them
        # moves the repository out of a protective ruleset.
        "collaborators", "invitations", "keys", "hooks", "pages", "environments",
        "codespaces", "dependabot", "rulesets", "properties", "interaction-limits",
        "immutable-releases", "autolinks", "topics", "lfs",
        "vulnerability-alerts", "automated-security-fixes", "private-vulnerability-reporting",
        "code-scanning", "secret-scanning", "dependency-graph", "security-advisories",
        "bypass-requests",
        # Automation signals humans and external systems act on; dispatches
        # feeds agent-chosen payloads into workflows that run with secrets.
        "statuses", "check-runs", "check-suites", "deployments", "dispatches", "attestations",
    }:
        return "github_repo_admin_write_denied"
    if host == "api.github.com" and sub == "releases":
        # Creating a release can create a tag. Keep uploads.github.com
        # release-asset uploads as normal repo-scoped writes.
        return "github_repo_admin_write_denied"
    if sub == "branches" and "protection" in parts[4:]:
        return "github_repo_admin_write_denied"
    if sub == "tags" and "protection" in parts[4:]:
        return "github_repo_admin_write_denied"
    if sub == "pulls" and parts[-1] == "update-branch":
        # Merges the base branch into the PR's head branch — a write to the
        # head repository, which may be an unlisted fork.
        return "github_repo_admin_write_denied"
    if sub == "actions":
        tail = parts[4:]
        if tail[:1] and tail[0] in {"secrets", "variables", "runners", "permissions", "oidc", "cache", "caches"}:
            # oidc: PUT actions/oidc/customization/sub sets the repository's
            # OIDC subject-claim template, which changes the cloud trust and
            # identity claims future workflows present — security
            # administration, not repository content.
            return "github_repo_admin_write_denied"
        if tail[:1] == ["workflows"] and parts[-1] in {"enable", "disable", "dispatches"}:
            # Turning a workflow off (CI, security scans) or dispatching one;
            # re-running an existing run stays a plain write.
            return "github_repo_admin_write_denied"
        if tail[:1] == ["runs"] and (
            method == "DELETE"
            or parts[-1] in {"cancel", "force-cancel", "approve", "pending_deployments", "deployment_protection_rule"}
        ):
            # Deleting a run/its logs erases evidence; cancel/approve/gate are
            # automation control that exists so a human reviews first.
            return "github_repo_admin_write_denied"
        if tail[:1] == ["artifacts"]:
            return "github_repo_admin_write_denied"
    return None


def _anthropic_pre_pin_bootstrap_allowed(method: str, path: str, bearer_tokens: list[str]) -> bool:
    # Claude Code exchanges the browser OAuth code with platform.claude.com,
    # then calls a small set of api.anthropic.com profile/settings endpoints
    # before its credential file is ready for the host to hash and pin. Let
    # only those bearer-authenticated bootstrap reads through pre-pin; model
    # traffic such as /v1/messages still fails closed until the hash is stored.
    return (
        method.upper() == "GET"
        and _normalized_path(path) in ANTHROPIC_PRE_PIN_BOOTSTRAP_GET_PATHS
        and bool(bearer_tokens)
    )


def _bearer_token(value: str) -> str | None:
    scheme, _, credential = value.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()


def websocket_inspection_required(policy: dict[str, Any], host: str) -> bool:
    """Whether WebSocket messages to ``host`` must be inspected. Only the
    external URL request guard depends on message bodies; the other controls
    (methods, paths, account pin) are fully decided at the handshake."""
    rule = find_domain_rule(policy, host) or {}
    return bool(rule.get("openai_external_url_request_guard"))


def openai_ws_message_denied(payload: bytes) -> str | None:
    """Apply the external URL request guard to one complete WebSocket message,
    mirroring the HTTP body check. WS payloads carry no Content-Encoding or
    Content-Type, so the message is inspected as-is. Only called for guarded
    hosts: the proxy gates on websocket_inspection_required at the handshake
    and tunnels everything else opaquely."""
    return _external_url_request_denial([], payload)


def _external_url_request_denial(
    headers: list[tuple[str, str]],
    body: bytes,
    path: str | None = None,
) -> str | None:
    """Block requests that would make the upstream reach an external URL with
    request data, while allowing cache-backed search. The upstream enables
    search and remote MCP only from parsed request fields, so prompt text that
    merely mentions a tool name carries no capability and enforcement is
    structural: see _external_url_request_violation for the rule, plus the
    standalone search endpoints, which must opt into cached retrieval because
    the server default there is live."""
    if not body:
        return None
    header_map = {key.lower(): value for key, value in headers}
    decoded = _decode_body(body, header_map.get("content-encoding", ""))
    if decoded is None:
        return "request body could not be decoded for web search inspection"
    body = decoded
    content_type = header_map.get("content-type", "").split(";", 1)[0].strip().lower()
    looks_json = content_type == "application/json" or body.lstrip().startswith((b"{", b"["))
    if not looks_json:
        # The upstream parses requests as JSON; a body it cannot parse cannot
        # declare tools, whatever its content-type label.
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "request body is not valid JSON"

    reason = _external_url_request_violation(payload)
    if reason is not None:
        return reason

    if path is not None and _normalized_path(path).rstrip("/") in OPENAI_SEARCH_PATHS:
        settings = payload.get("settings") if isinstance(payload, dict) else None
        if not isinstance(settings, dict):
            settings = {}
        if settings.get("external_web_access") is not False:
            return "live web search is disabled for this domain (external_web_access must be false)"
        if settings.get("indexed_web_access") not in (False, None):
            return "indexed web search is disabled for this domain (indexed_web_access must be false)"
    return None


def _external_url_request_violation(payload: Any) -> str | None:
    """The upstream must never reach an external URL with request data. Web
    content may be retrieved cache-backed only: the sole permitted web tool is
    exactly ``web_search`` with ``external_web_access`` false *and*
    ``indexed_web_access`` false or absent. Everything else this collects is
    denied, so the rule fails closed: ``web_search_preview`` and dated variants
    (they browse live), a bare ``web`` / ``web_fetch`` tool, ``browser`` /
    ``computer_use`` (a driven browser), ``code_interpreter`` (a hosted
    container that can egress), any tool object carrying a truthy
    ``*_web_access`` flag under a different type — a renamed web tool — and
    remote MCP tools (``type: mcp``, by ``server_url`` or hosted
    ``connector_id``). Chat Completions search (``web_search_options``,
    ``*-search*`` models) has no cached form and is denied outright."""
    for tool in _iter_tool_objects(payload):
        reason = _tool_object_violation(tool)
        if reason is not None:
            return reason
    if _contains_key(payload, "server_url"):
        return "remote MCP tools are disabled for this domain"
    if _contains_key(payload, "web_search_options"):
        return "web_search_options is disabled for this domain"
    model = payload.get("model") if isinstance(payload, dict) else None
    if isinstance(model, str) and "-search" in model:
        return "web search models are disabled for this domain"
    return None


def _tool_object_violation(tool: dict[str, Any]) -> str | None:
    """Decide a single guarded tool object. Only the cached ``web_search`` shape
    passes; any other collected tool is denied, so a renamed or newly added
    web/browse tool fails closed instead of being forwarded."""
    tool_type = tool.get("type")
    if tool_type == "mcp":
        return "remote MCP tools are disabled for this domain"
    if tool_type != "web_search":
        # web_search_preview and dated variants keep their historical reason; a
        # bare web/web_fetch/browser/computer_use/code_interpreter tool, or one
        # that only
        # matched on a *_web_access flag, has no cache-backed form here.
        if isinstance(tool_type, str) and tool_type.startswith("web_search"):
            return "web_search_preview is disabled for this domain"
        label = tool_type if isinstance(tool_type, str) and tool_type else "web browsing"
        return f"{label} tool is disabled for this domain (only cached web_search is allowed)"
    if tool.get("external_web_access") is not False:
        return "live web search is disabled for this domain (external_web_access must be false)"
    if tool.get("indexed_web_access") not in (False, None):
        return "indexed web search is disabled for this domain (indexed_web_access must be false)"
    for key, value in tool.items():
        if (
            isinstance(key, str)
            and key.endswith("_web_access")
            and key not in ("external_web_access", "indexed_web_access")
            and value not in (False, None)
        ):
            return f"{key} is disabled for this domain (must be false)"
    return None


def _contains_key(payload: Any, key: str) -> bool:
    if isinstance(payload, dict):
        if key in payload:
            return True
        return any(_contains_key(value, key) for value in payload.values())
    if isinstance(payload, list):
        return any(_contains_key(item, key) for item in payload)
    return False


def _iter_tool_objects(payload: Any) -> list[dict[str, Any]]:
    """Collect every guarded tool object anywhere in the request, so a tool
    nested under any key is still inspected. Guarded means: a remote-MCP tool
    (``type: mcp``); a web/browse tool named by its type — any ``type`` starting
    with ``web`` (covering ``web_search``, dated ``web_search_preview`` variants,
    and a bare ``web``/``web_fetch``) or a ``_DENIED_HOSTED_TOOL_TYPES`` member
    (``browser``/``computer_use``/``code_interpreter``); or, so
    a renamed web tool still fails closed, any typed object that carries a
    *truthy* ``*_web_access`` flag (a false/absent flag grants no access, so a
    safe tool is not swept in). web_search_call is excluded: it is a history item
    replaying an earlier search, not a tool declaration, and appears in
    legitimate cached-search requests; mcp_call and mcp_list_tools history item
    types do not match either. A ``type``-less object such as the standalone
    search ``settings`` block is not collected here — the endpoint check in
    _external_url_request_denial covers it."""
    matches: list[dict[str, Any]] = []

    def is_guarded(node: dict[str, Any]) -> bool:
        type_value = node.get("type")
        if not isinstance(type_value, str) or type_value == "web_search_call":
            return False
        if type_value == "mcp" or type_value.startswith("web") or type_value in _DENIED_HOSTED_TOOL_TYPES:
            return True
        # A renamed web tool: guarded only if it actually requests access via a
        # truthy ``*_web_access`` flag. A false/default flag on an otherwise safe
        # tool (e.g. a function tool that carries ``external_web_access: false``)
        # grants nothing and must not be swept in and then denied for its type.
        return any(
            isinstance(key, str) and key.endswith("_web_access") and value not in (False, None)
            for key, value in node.items()
        )

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if is_guarded(node):
                matches.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return matches


def _decode_body(body: bytes, content_encoding: str) -> bytes | None:
    """Decode a Content-Encoding so the guard inspects real JSON, not a
    compressed blob. Only the stdlib-decodable encodings are supported;
    anything else (zstd, br, corrupt streams) returns None, which the caller
    treats as fail-closed. Clients essentially never compress request bodies
    and the agent CLIs are version-pinned, so a live denial is the signal to
    add an encoding, not a fallback decoder."""
    encoding = content_encoding.strip().lower()
    if encoding in ("", "identity"):
        return body
    if encoding == "gzip":
        return _bounded_gzip_decompress(body)
    if encoding in ("deflate", "zlib"):
        return _bounded_zlib_decompress(body)
    return None


def _bounded_gzip_decompress(body: bytes) -> bytes | None:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as handle:
            return _read_decoded_limited(handle)
    except (EOFError, OSError, zlib.error):
        return None


def _bounded_zlib_decompress(body: bytes) -> bytes | None:
    decoder = zlib.decompressobj()
    decoded = bytearray()
    try:
        for offset in range(0, len(body), 64 * 1024):
            chunk = body[offset : offset + 64 * 1024]
            while chunk:
                remaining = MAX_DECODED_BODY_BYTES - len(decoded) + 1
                if remaining <= 0:
                    return None
                decoded.extend(decoder.decompress(chunk, remaining))
                if len(decoded) > MAX_DECODED_BODY_BYTES:
                    return None
                chunk = decoder.unconsumed_tail
        decoded.extend(decoder.flush(MAX_DECODED_BODY_BYTES - len(decoded) + 1))
    except zlib.error:
        return None
    if len(decoded) > MAX_DECODED_BODY_BYTES or not decoder.eof:
        return None
    return bytes(decoded)


def _read_decoded_limited(handle: Any) -> bytes | None:
    decoded = bytearray()
    while True:
        remaining = MAX_DECODED_BODY_BYTES - len(decoded) + 1
        if remaining <= 0:
            return None
        chunk = handle.read(min(64 * 1024, remaining))
        if not chunk:
            return bytes(decoded)
        decoded.extend(chunk)
        if len(decoded) > MAX_DECODED_BODY_BYTES:
            return None
