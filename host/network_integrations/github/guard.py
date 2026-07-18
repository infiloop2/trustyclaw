"""GitHub request guard: all reads allowed, writes scoped to listed repos.

Runs in the proxy for hosts under the GitHub apexes. When the integration is
enabled, every read is allowed (an agent's utility comes from many data-in
paths — read any repo, public or private, that the injected token reaches).
The controlled side is writes: a mutation must target a configured write
repository, and the subset that reaches past repository content is denied
outright. So the guard only ever gates writes; reads pass through.

This module also owns the two post-decision hooks on GitHub domains: the
``.github`` push-approval gate (``gate_response``, engine in ``push_gate``)
and working-credential injection (``rewrite_request_headers``).
"""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs

from host.network_integrations.github import push_gate
from host.network_integrations.github.manifest import GitHubIntegration
from host.runtime.core.network_policy import normalized_path, route_allowed
from host.runtime.core.state import enqueue_pending_push, read_proxy_github_token

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
ROUTES = {
    "github.com": (("GET", "HEAD", "POST"), ()),
    "api.github.com": (("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"), ()),
    "uploads.github.com": (("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"), ()),
    "codeload.github.com": (("GET", "HEAD"), ()),
    "raw.githubusercontent.com": (("GET", "HEAD"), ()),
    "objects.githubusercontent.com": (("GET", "HEAD"), ()),
    "github-cloud.githubusercontent.com": (("GET", "HEAD"), ()),
    "release-assets.githubusercontent.com": (("GET", "HEAD"), ()),
}
GUARDED_HOSTS = frozenset({"github.com", "api.github.com", "uploads.github.com"})


def host_allowed(config: GitHubIntegration, host: str) -> bool:
    del config
    return host.lower() in ROUTES


def rewrite_request_headers(host: str, headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Credential injection: on GitHub domains, strip whatever
    ``Authorization`` the agent sent and inject the active working token
    (``proxy_github_token`` row) instead. The agent never holds the
    credential — there is nothing to copy or exfiltrate, revocation is one
    row delete — and an agent-smuggled token cannot substitute another
    identity. Runs only after the request passed the repo guard; the custom
    integration cannot claim these domains, so every allowed request here
    came through the GitHub integration. Without a working token the
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


def request_denied(
    config: GitHubIntegration,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> str | None:
    """Apply the GitHub-owned route and repository write guard."""
    route = ROUTES.get(host.lower())
    if route is None or not route_allowed(method, path, query, *route):
        return "network_policy_denied"
    if host.lower() not in GUARDED_HOSTS:
        return None
    write_repos = {(item.owner, item.repo) for item in config.write_repositories}
    require_approval = config.require_dot_github_approval
    method = method.upper()
    host = host.lower()
    path = normalized_path(path)
    if host == "github.com":
        return _github_com_request_denied(write_repos, method, path, query, body)
    if host == "api.github.com":
        return _github_api_request_denied(write_repos, method, path, host=host, require_approval=require_approval)
    if host == "uploads.github.com":
        return _github_api_request_denied(write_repos, method, path, host=host, require_approval=False)
    return "github_repo_scope_required"


def gate_response(
    config: GitHubIntegration, method: str, host: str, path: str, body: bytes
) -> tuple[bytes | None, str | None]:
    """The ``.github`` approval gate, applied after a push has passed the write
    guard. Returns ``(response, denial)``:

    - ``(None, None)`` — not a gated push, or a clean one: forward normally.
    - ``(bytes, code)`` — send ``bytes`` (a git ``report-status``) back to the
      agent instead of forwarding; the push is queued for approval.
    - ``(None, code)`` — fail closed with a plain proxy denial (the push could
      not be parsed or inspected).
    """
    if not config.require_dot_github_approval:
        return None, None
    # Trigger: a receive-pack POST to github.com. The write guard has already
    # run (the proxy calls this only after the deny decision passed), so the
    # target is a configured write repository by construction — no re-matching
    # here.
    parts = [part for part in normalized_path(path).split("/") if part]
    if host.lower() != "github.com" or method.upper() != "POST" or len(parts) != 3 or parts[2] != "git-receive-pack":
        return None, None
    owner, repo = parts[0].lower(), parts[1].removesuffix(".git").lower()
    try:
        result = push_gate.inspect(owner, repo, body, read_proxy_github_token())
    except push_gate.GateError:
        return None, "github_push_gate_unavailable"
    if not result.touches_github:
        return None, None  # nothing under .github/ changed: forward the push
    push_id = push_gate.new_push_id()
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
