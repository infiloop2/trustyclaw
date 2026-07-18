"""Repository audit and operator warnings for the GitHub integration.

Facts are fetched by the ``audit-github-repo`` root helper (the admin
service has no egress) using the active working token, and stored per
repository in the admin-owned ``github_repo_audit`` table. Warnings are
derived from the stored facts in code — facts in the database, judgments
here — so message changes never need a re-fetch.

Refresh lifecycle: forced after a credential change, after a policy
publish that changes the GitHub integration, and on the UI's re-check
action, and TTL-gated (daily) from the orchestrator poller. Failures record themselves
per repository and retry on the next pass — the same converge-and-retry
shape as the credential itself. Audits exist to warn, never to gate: a
failed or missing audit never blocks the credential or a publish.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from host.runtime.core import state
from host.runtime.admin_api.github_credential import HelperError, _run_helper_json
from host.runtime.core.network_policy import managed_integration

AUDIT_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/audit-github-repo"]
# Repository configuration changes rarely; the forced refreshes on credential
# and repository-list changes cover the moments that matter, and the poller
# re-checks on this TTL to catch drift made directly on GitHub.
AUDIT_TTL = timedelta(days=1)


def refresh(force: bool = False) -> None:
    """Fetch and store audits for the policy's repositories. ``force``
    re-audits everything; otherwise only repositories with no stored audit
    or one older than the TTL. Audits use the published working token — the
    same token every GitHub request uses; callers that need a post-change
    token run ``github_credential.reconcile(mint_fresh=True)`` first. Never
    raises."""
    repositories = _policy_repositories()
    state.prune_github_repo_audits(set(repositories))
    if not repositories:
        return
    token = state.read_proxy_github_token()
    if not token:
        # No token to audit with — the audit's whole point is what *this*
        # credential can do, so every listed repository records the failure
        # (fail closed: the API must never keep reporting facts computed for
        # a credential that no longer works) and retries after convergence.
        for owner, repo in repositories:
            state.save_github_repo_audit(owner, repo, {}, "no credential token to audit with")
        return
    stored = state.read_github_repo_audits()
    due = [
        {"owner": owner, "repo": repo}
        for owner, repo in repositories
        if force or _stale(stored.get((owner, repo)))
    ]
    if not due:
        return
    try:
        result = _run_helper_json(AUDIT_COMMAND, {"token": token, "repositories": due})
    except HelperError as exc:
        for entry in due:
            state.save_github_repo_audit(entry["owner"], entry["repo"], {}, f"audit failed: {exc}")
        return
    audits = result.get("audits")
    audits = audits if isinstance(audits, dict) else {}
    for entry in due:
        audit = audits.get(f"{entry['owner']}/{entry['repo']}")
        if not isinstance(audit, dict):
            state.save_github_repo_audit(entry["owner"], entry["repo"], {}, "audit returned no result")
        elif "error" in audit:
            state.save_github_repo_audit(entry["owner"], entry["repo"], {}, str(audit["error"]))
        else:
            state.save_github_repo_audit(entry["owner"], entry["repo"], audit, None)


def summaries() -> list[dict[str, Any]]:
    """Per-repository audit summaries for the admin API: stored facts turned
    into operator warnings, in the policy's repository order."""
    stored = state.read_github_repo_audits()
    values: list[dict[str, Any]] = []
    for owner, repo in _policy_repositories():
        audit = stored.get((owner, repo))
        summary: dict[str, Any] = {"owner": owner, "repo": repo}
        if audit is None:
            summary["warnings"] = [_incomplete_warning("repository audit has not run yet")]
            values.append(summary)
            continue
        summary["audited_at"] = audit.get("fetched_at")
        if "error" in audit:
            summary["error"] = audit["error"]
            summary["warnings"] = [_incomplete_warning(str(audit["error"]))]
        else:
            summary["warnings"] = _warnings(audit.get("facts") or {})
        values.append(summary)
    return values


def _incomplete_warning(reason: str) -> dict[str, str]:
    return {
        "code": "repository_audit_incomplete",
        "severity": "warning",
        "message": f"Repository audit could not verify this write target: {reason}. TrustyClaw "
        "does not have enough information to check repository visibility, GitHub Pages, default "
        "branch protection, or workflows. Configure a working GitHub credential and re-check "
        "repository audits.",
    }


def _warnings(facts: dict[str, Any]) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    if facts.get("visibility") != "private":
        warnings.append(
            {
                "code": "public_repository",
                "severity": "critical",
                "message": "Repository is public and a write target: everything the agent pushes "
                "here (branches, commit messages, pull requests and descriptions) is world-visible, "
                "and because reads are universal an injected agent can copy any private repository "
                "the token can reach into a commit, issue, or PR here — a public write repository is "
                "an exfiltration sink. Make write repositories private, or scope the token so it "
                "cannot read what must not leak.",
            }
        )
    if facts.get("visibility") == "private" and facts.get("pages_public") is True:
        warnings.append(
            {
                "code": "public_pages_site",
                "severity": "critical",
                "message": "The repository is private but its GitHub Pages site is public: a push "
                "to the Pages source publishes agent-written content to the internet — the same "
                "exfiltration sink as a public write repository. Make the Pages site private or "
                "disable Pages.",
            }
        )
    if facts.get("visibility") == "private" and facts.get("pages_public") is None:
        warnings.append(
            {
                "code": "pages_visibility_unknown",
                "severity": "warning",
                "message": "GitHub Pages visibility could not be verified for this private repository: "
                "GitHub did not conclusively report that Pages is disabled, and denied or hid the "
                "Pages settings, so the audit cannot prove whether pushes to a Pages source would "
                "publish agent-written content to the internet. Grant the token Pages read access, "
                "make the Pages site private, or disable Pages.",
            }
        )
    permissions = facts.get("permissions") or {}
    if permissions.get("push") is True and facts.get("default_branch_protected") is False:
        warnings.append(
            {
                "code": "unprotected_default_branch",
                "severity": "warning",
                "message": "The token can push and the default branch is unprotected: the agent "
                "identity can push straight to the default branch. Add a GitHub ruleset or branch "
                "protection (require pull requests) so the agent only opens PRs and a human merges.",
            }
        )
    workflows = facts.get("workflows") or []
    if isinstance(workflows, list) and workflows:
        dangerous = sorted(
            {
                trigger
                for workflow in workflows
                if isinstance(workflow, dict)
                for trigger in (workflow.get("triggers") or [])
                if trigger == "pull_request_target"
            }
        )
        if dangerous:
            warnings.append(
                {
                    "code": "secrets_exposed_to_pr_workflows",
                    "severity": "critical",
                    "message": f"Workflows use {', '.join(dangerous)}, which runs with the base "
                    "repository's secrets against PR-influenced context — the classic Actions "
                    "exfiltration primitive. Restrict or remove these triggers, or require approval "
                    "for outside workflow runs.",
                }
            )
        warnings.append(
            {
                "code": "workflows_execute_pushes",
                "severity": "warning",
                "message": "The repository has GitHub Actions workflows: a push runs the workflow "
                "as it exists on the pushed branch, on any branch, so agent pushes can execute "
                "agent-written code with whatever secrets and network the repository grants. "
                "Run workflows for untrusted code in a no-internet container with "
                "permissions: {}.",
            }
        )
    return warnings


def _stale(audit: dict[str, Any] | None) -> bool:
    if not audit:
        return True
    if "error" in audit:
        # A failed fetch is never fresh: the promise is that failures retry
        # on the next pass, not after a full TTL.
        return True
    fetched_at = audit.get("fetched_at")
    if not isinstance(fetched_at, str):
        return True
    try:
        fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) - fetched >= AUDIT_TTL


def _policy_repositories() -> list[tuple[str, str]]:
    """The policy's GitHub write repositories (validation ties them to an
    enabled integration). Reads are universal, so only write targets are
    worth auditing — the visibility and workflow warnings are about what the
    agent's pushes and PRs expose."""
    repositories: list[tuple[str, str]] = []
    for item in managed_integration("github").get("write_repositories") or []:
        if isinstance(item, dict) and isinstance(item.get("owner"), str) and isinstance(item.get("repo"), str):
            repositories.append((item["owner"], item["repo"]))
    return repositories
