"""Root helper behind ``audit-github-repo``.

Fetches the repository facts the admin service turns into operator warnings
(see ``github_repo_audit``): visibility, the working token's own effective
permissions, default-branch protection, Pages visibility, and the workflows
with their triggers. It runs as root because the admin service that owns the
audit table has no outbound network access. Facts only — no judgments: the
warning derivation lives admin-side so message changes never need a
re-fetch.

Input (stdin JSON):
``{"token": "...", "repositories": [{"owner": "...", "repo": "..."}, ...]}``

Output: ``{"audits": {"<owner>/<repo>": {facts} | {"error": "..."}}}`` —
per-repository errors (404, permission gaps) land inside that repository's
entry so one broken repo never blocks the others' audits. Exit code 2 with
``{"error": {"message": "..."}}`` only for malformed input.
"""

from __future__ import annotations

import base64
import json
import sys
from typing import Any, NoReturn
import urllib.error
import urllib.parse
import urllib.request

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 30
MAX_WORKFLOW_FILES = 50
MAX_WORKFLOW_BYTES = 262144


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    if not isinstance(payload, dict):
        _fail("payload must be an object")
    token = payload.get("token")
    repositories = payload.get("repositories")
    if not isinstance(token, str) or not token:
        _fail("token must be a non-empty string")
    if not isinstance(repositories, list):
        _fail("repositories must be an array")
    audits: dict[str, Any] = {}
    for item in repositories:
        if not isinstance(item, dict) or not isinstance(item.get("owner"), str) or not isinstance(item.get("repo"), str):
            _fail("repositories entries must be {owner, repo} objects")
        owner, repo = item["owner"], item["repo"]
        try:
            audits[f"{owner}/{repo}"] = audit_repository(token, owner, repo)
        except AuditError as exc:
            audits[f"{owner}/{repo}"] = {"error": str(exc)}
    print(json.dumps({"audits": audits}, sort_keys=True))


class AuditError(Exception):
    pass


def audit_repository(token: str, owner: str, repo: str) -> dict[str, Any]:
    repository = _get(token, f"/repos/{owner}/{repo}")
    has_pages = repository.get("has_pages")
    has_pages = has_pages if isinstance(has_pages, bool) else None
    facts: dict[str, Any] = {
        "visibility": repository.get("visibility") or ("private" if repository.get("private") else "public"),
        "permissions": {
            name: value is True
            for name, value in (repository.get("permissions") or {}).items()
            if name in ("pull", "push", "admin", "maintain", "triage")
        },
    }
    # pages_public is the one stored Pages fact, a tri-state: True (public
    # site), False (no site, or a private one), None (unknown — the token
    # could not read the Pages settings).
    if has_pages is False:
        # The repository response is authoritative for "no Pages site".
        facts["pages_public"] = False
    else:
        try:
            pages = _get(token, f"/repos/{owner}/{repo}/pages")
            facts["pages_public"] = isinstance(pages, dict) and pages.get("public") is True
        except AuditError as exc:
            # The repo says it has Pages but this token cannot read the
            # Pages settings (GitHub answers 403 or 404 for missing
            # permission): the visibility is recorded as unknown (null)
            # rather than as private or as a failed audit.
            if getattr(exc, "status", None) not in (403, 404):
                raise
            facts["pages_public"] = None
    default_branch = repository.get("default_branch")
    if isinstance(default_branch, str) and default_branch:
        # Branch names may carry slashes and other URL-special characters;
        # encode the whole name as one path segment. A failed fetch fails the
        # whole repository audit — one top-level error per repo, no partial
        # facts — and retries on the next pass.
        branch = _get(token, f"/repos/{owner}/{repo}/branches/{urllib.parse.quote(default_branch, safe='')}")
        facts["default_branch_protected"] = branch.get("protected") is True
    facts["workflows"] = _workflows(token, owner, repo)
    return facts


def _workflows(token: str, owner: str, repo: str) -> list[dict[str, Any]]:
    """Workflow files and their ``on:`` triggers — one check, one fact. A 404
    on the directory means no workflows."""
    try:
        listing = _get(token, f"/repos/{owner}/{repo}/contents/.github/workflows")
    except AuditError as exc:
        if getattr(exc, "status", None) == 404:
            return []
        raise
    if not isinstance(listing, list):
        return []
    workflows: list[dict[str, Any]] = []
    for entry in listing[:MAX_WORKFLOW_FILES]:
        if not isinstance(entry, dict) or entry.get("type") != "file":
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path.endswith((".yml", ".yaml")):
            continue
        # Percent-encode the listed path: a filename with '#' or '?' must
        # stay a path character, not a fragment/query delimiter. A failed
        # fetch fails the whole repository audit (one top-level error).
        content = _get(token, f"/repos/{owner}/{repo}/contents/{urllib.parse.quote(path, safe='/')}")
        encoded = content.get("content") if isinstance(content, dict) else None
        text = ""
        if isinstance(encoded, str):
            text = base64.b64decode(encoded, validate=False)[:MAX_WORKFLOW_BYTES].decode("utf-8", "replace")
        workflows.append({"path": path, "triggers": _triggers(text)})
    return workflows


# The trigger names the warnings care about: triggers that expose the base
# repository's secrets to PR-influenced code.
DANGEROUS_TRIGGERS = ("pull_request_target",)


def _triggers(text: str) -> list[str]:
    """The dangerous trigger names appearing anywhere in the workflow file —
    a plain substring search (stdlib has no YAML, and a parser here earns
    nothing: a false positive over-warns, which is fine, and the workflow's
    *presence* already warns on its own)."""
    return [name for name in DANGEROUS_TRIGGERS if name in text]


def _get(token: str, path: str) -> Any:
    request = urllib.request.Request(
        f"{GITHUB_API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        error = AuditError(f"GET {path} failed with {exc.code}")
        error.status = exc.code  # type: ignore[attr-defined]
        raise error from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"GET {path} failed: {exc}") from exc


def _fail(message: str) -> NoReturn:
    print(json.dumps({"error": {"message": message}}, sort_keys=True))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
