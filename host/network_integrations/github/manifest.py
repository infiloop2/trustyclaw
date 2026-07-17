"""GitHub managed integration: static contract.

Opens the GitHub domain set with an all-reads, scoped-writes guard: every
read is allowed (the injected token bounds what private data is visible),
writes must target a configured write repository, and writes that reach past
repository content are denied outright. The proxy injects the active working
token on GitHub domains, so the agent never holds the credential.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from host.network_integrations.base import (
    DenialReason,
    IntegrationConfigError,
    IntegrationManifest,
    reject_extra,
)

GITHUB_OWNER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$")
GITHUB_REPO_RE = re.compile(r"^[a-z0-9._-]{1,100}$")

MANIFEST = IntegrationManifest(
    integration_id="github",
    display_name="GitHub",
    description=(
        "GitHub access with the host-injected working credential: reads of any repository "
        "the credential reaches are allowed, pushes and API writes must target a configured "
        "write repository, and repository administration (access grants, hooks, publishing, "
        "workflow control) is denied even there. Optionally holds pushes that change "
        ".github/ paths for operator approval."
    ),
    owned_apexes=("github.com", "githubusercontent.com"),
    denial_reasons=(
        DenialReason(
            "github_write_repo_required",
            "This mutation does not target a configured write repository, so it was denied "
            "(reads are always allowed). The operator can add the repository to the GitHub "
            "integration's write repositories in the admin UI's Network tab.",
        ),
        DenialReason(
            "github_repo_admin_write_denied",
            "This write reaches past repository content — repository settings, access grants, "
            "hooks, publishing, protection rules, or workflow/automation control — and is "
            "always denied, even for a configured write repository. Repository administration "
            "is done by the operator directly on GitHub.",
        ),
        DenialReason(
            "github_graphql_denied",
            "The GitHub GraphQL API is denied entirely: a GraphQL POST can mutate and cannot "
            "be checked with path rules. Use the REST API, which covers the same repo-scoped "
            "operations.",
        ),
        DenialReason(
            "github_lfs_push_unsupported",
            "Pushing new Git LFS objects is not supported: LFS uploads go to signed URLs the "
            "proxy cannot repo-check. Keep large files out of LFS or have the operator upload "
            "them.",
        ),
        DenialReason(
            "github_lfs_operation_unresolved",
            "The Git LFS batch request could not be parsed (or used an unsupported LFS "
            "endpoint), so it failed closed. Only LFS download (clone/fetch) is supported.",
        ),
        DenialReason(
            "github_repo_scope_required",
            "Requests to this GitHub host cannot be scoped to a repository, so writes are "
            "denied there. Use github.com or api.github.com paths that name the repository.",
        ),
        DenialReason(
            "github_dot_github_rest_write_denied",
            "While .github push approval is enabled, REST writes that could change .github/ "
            "content without a push (contents under .github/, low-level git object/ref APIs, "
            "merges) are denied. Push the change instead so it goes through the approval "
            "gate.",
        ),
        DenialReason(
            "github_push_queued_for_approval",
            "The push changes a .github/ path, so it was held for operator approval instead "
            "of being forwarded; each ref was reported as 'queued for approval as "
            "push-<id>'. Tell the user to approve or deny it in the admin UI's Network tab, "
            "then check the repository on GitHub — do not re-push the same commits while it "
            "is pending.",
        ),
        DenialReason(
            "github_push_gate_unavailable",
            "The push changes are gated on .github approval, but the push could not be "
            "inspected (git or quarantine failure), so it failed closed. Retry the push; if "
            "it keeps failing, ask the operator to check the host.",
        ),
    ),
)


@dataclass(frozen=True)
class GitHubRepository:
    owner: str
    repo: str

    def to_json(self) -> dict[str, Any]:
        return {"owner": self.owner, "repo": self.repo}


@dataclass(frozen=True)
class GitHubIntegration:
    """When enabled, the agent may read any repository the injected token
    reaches; ``write_repositories`` is the list it may also push to and mutate
    through the API (repository administration stays denied even there). The
    list is what the audit inspects. ``require_dot_github_approval`` holds a push
    that changes any ``.github/`` path for operator approval instead of
    forwarding it to GitHub."""

    enabled: bool
    write_repositories: tuple[GitHubRepository, ...] = ()
    require_dot_github_approval: bool = False

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {"enabled": self.enabled}
        if self.write_repositories:
            value["write_repositories"] = [repo.to_json() for repo in self.write_repositories]
        if self.require_dot_github_approval:
            value["require_dot_github_approval"] = True
        return value


def parse(raw: dict[str, Any]) -> GitHubIntegration:
    if not raw:
        return GitHubIntegration(False)
    context = "network_integrations.github"
    reject_extra(raw, {"enabled", "write_repositories", "require_dot_github_approval"}, context)
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise IntegrationConfigError(f"{context}.enabled must be true or false")
    require_dot_github_approval = raw.get("require_dot_github_approval", False)
    if not isinstance(require_dot_github_approval, bool):
        raise IntegrationConfigError(f"{context}.require_dot_github_approval must be true or false")
    write_repositories = parse_write_repositories(raw.get("write_repositories", []), context)
    # A disabled GitHub integration carries no other state.
    if not enabled and (write_repositories or require_dot_github_approval):
        raise IntegrationConfigError(
            f"{context}.write_repositories and require_dot_github_approval require enabled to be true"
        )
    return GitHubIntegration(
        enabled=enabled, write_repositories=write_repositories, require_dot_github_approval=require_dot_github_approval
    )


def parse_write_repositories(raw: Any, context: str) -> tuple[GitHubRepository, ...]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise IntegrationConfigError(f"{context}.write_repositories must be an array")
    repositories: list[GitHubRepository] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise IntegrationConfigError(f"{context}.write_repositories[{index}] must be an object")
        reject_extra(item, {"owner", "repo"}, f"{context}.write_repositories[{index}]")
        owner = _string(item, "owner", context, index).lower()
        # Accept the commonly pasted "repo.git" form: enforcement strips the
        # suffix from request paths before matching, so a stored ".git" name
        # would never match anything (GitHub forbids real names ending .git).
        repo = _string(item, "repo", context, index).lower().removesuffix(".git")
        if not GITHUB_OWNER_RE.fullmatch(owner):
            raise IntegrationConfigError(
                f"{context}.write_repositories[{index}].owner is not a valid GitHub owner"
            )
        if not GITHUB_REPO_RE.fullmatch(repo):
            raise IntegrationConfigError(
                f"{context}.write_repositories[{index}].repo is not a valid GitHub repository name"
            )
        key = (owner, repo)
        if key in seen:
            raise IntegrationConfigError(
                f"{context}.write_repositories has duplicate repository {owner}/{repo}"
            )
        seen.add(key)
        repositories.append(GitHubRepository(owner=owner, repo=repo))
    return tuple(repositories)


def _string(item: dict[str, Any], key: str, context: str, index: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise IntegrationConfigError(f"{context}.write_repositories[{index}].{key} must be a non-empty string")
    return value.strip()
