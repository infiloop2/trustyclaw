from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from pathlib import Path
from typing import Any, Mapping


ALLOWED_HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
GITHUB_OWNER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$")
GITHUB_REPO_RE = re.compile(r"^[a-z0-9._-]{1,100}$")
EXACT_DOMAIN_RE = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
WILDCARD_DOMAIN_RE = re.compile(r"^\*\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
OPENAI_PROVIDER_RULES: dict[str, dict[str, Any]] = {
    "api.openai.com": {
        "allow_http_methods": ("POST",),
        "openai_account_guard": True,
        "openai_disable_live_web_search": True,
    },
    "auth.openai.com": {
        "allow_http_methods": ("GET", "POST"),
    },
    "chatgpt.com": {
        "allow_http_methods": ("GET", "POST"),
        "openai_account_guard": True,
        "openai_disable_live_web_search": True,
    },
}
CLAUDE_PROVIDER_RULES: dict[str, dict[str, Any]] = {
    "api.anthropic.com": {
        "allow_http_methods": ("GET", "POST"),
        "anthropic_account_guard": True,
    },
    "platform.claude.com": {
        "allow_http_methods": ("GET", "POST"),
        "path_guards": ("^/v1/oauth(?:/.*)?$",),
    },
}
PYTHON_PACKAGE_PROVIDER_RULES: dict[str, dict[str, Any]] = {
    "pypi.org": {
        "allow_http_methods": ("GET", "HEAD"),
        "path_guards": ("^/simple(?:/.*)?$", "^/pypi/[^/]+/json$"),
    },
    "files.pythonhosted.org": {
        "allow_http_methods": ("GET", "HEAD"),
        "path_guards": ("^/packages(?:/.*)?$",),
    },
}
NPM_PACKAGE_PROVIDER_RULES: dict[str, dict[str, Any]] = {
    "registry.npmjs.org": {
        "allow_http_methods": ("GET", "HEAD"),
    },
    "nodejs.org": {
        "allow_http_methods": ("GET", "HEAD"),
        "path_guards": ("^/dist(?:/.*)?$",),
    },
}
AGENT_RUNTIMES = {"codex", "claude_code"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DomainRule:
    allow_http_methods: tuple[str, ...]
    path_guards: tuple[str, ...]
    openai_disable_live_web_search: bool | None = None
    openai_account_guard: bool | None = None
    anthropic_account_guard: bool | None = None
    github_repo_guard: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "allow_http_methods": list(self.allow_http_methods),
        }
        if self.path_guards:
            value["path_guards"] = list(self.path_guards)
        if self.openai_disable_live_web_search is not None:
            value["openai_disable_live_web_search"] = self.openai_disable_live_web_search
        if self.openai_account_guard is not None:
            value["openai_account_guard"] = self.openai_account_guard
        if self.anthropic_account_guard is not None:
            value["anthropic_account_guard"] = self.anthropic_account_guard
        if self.github_repo_guard is not None:
            value["github_repo_guard"] = self.github_repo_guard
        return value


@dataclass(frozen=True)
class ManagedIntegration:
    enabled: bool

    def to_json(self) -> dict[str, Any]:
        return {"enabled": True} if self.enabled else {"enabled": False}


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


@dataclass(frozen=True)
class ManagedNetworkIntegrations:
    openai: ManagedIntegration = field(default_factory=lambda: ManagedIntegration(False))
    claude: ManagedIntegration = field(default_factory=lambda: ManagedIntegration(False))
    github: GitHubIntegration = field(default_factory=lambda: GitHubIntegration(False))
    python_packages: ManagedIntegration = field(default_factory=lambda: ManagedIntegration(False))
    npm_packages: ManagedIntegration = field(default_factory=lambda: ManagedIntegration(False))

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key in ("openai", "claude", "github", "python_packages", "npm_packages"):
            integration = getattr(self, key)
            # A disabled integration carries no other state, so it serializes
            # away entirely.
            if integration.enabled:
                value[key] = integration.to_json()
        return value


@dataclass(frozen=True)
class NetworkControls:
    managed_network_integrations: ManagedNetworkIntegrations
    allowed_network_access: dict[str, DomainRule]

    def to_json(self) -> dict[str, Any]:
        return {
            "managed_network_integrations": self.managed_network_integrations.to_json(),
            "allowed_network_access": {
                domain: rule.to_json() for domain, rule in sorted(self.allowed_network_access.items())
            },
        }


@dataclass(frozen=True)
class OperatorConnection:
    mode: str
    ssh_public_key: str | None = None
    hostname: str | None = None
    tunnel_token_env: str | None = None

    def to_input_json(self) -> dict[str, Any]:
        if self.mode == "ssh":
            return {
                "mode": "ssh",
                "ssh_public_key": self.ssh_public_key,
            }
        if self.mode == "cloudflare_access":
            return {
                "mode": "cloudflare_access",
                "hostname": self.hostname,
                "tunnel_token_env": self.tunnel_token_env,
            }
        raise ValueError(f"unsupported operator connection mode: {self.mode}")


@dataclass(frozen=True)
class RuntimeOperatorConnection:
    mode: str
    ssh_public_key: str | None = None
    hostname: str | None = None
    tunnel_token: str | None = None

    def to_json(self) -> dict[str, Any]:
        if self.mode == "ssh":
            return {
                "mode": "ssh",
                "ssh_public_key": self.ssh_public_key,
            }
        if self.mode == "cloudflare_access":
            return {
                "mode": "cloudflare_access",
                "hostname": self.hostname,
                "tunnel_token": self.tunnel_token,
            }
        raise ValueError(f"unsupported operator connection mode: {self.mode}")


@dataclass(frozen=True)
class InputConfig:
    agent_name: str
    aws_region: str
    aws_access_key_id_env: str
    aws_secret_access_key_env: str
    operator_connections: tuple[OperatorConnection, ...] | None


def load_input_config(
    path: str | Path,
    *,
    require_operator_connections: bool = True,
) -> InputConfig:
    try:
        raw = json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    return parse_input_config(raw, require_operator_connections=require_operator_connections)


def parse_input_config(
    raw: dict[str, Any],
    *,
    require_operator_connections: bool = True,
) -> InputConfig:
    allowed_keys = {
        "agent_name",
        "aws_region",
        "aws_access_key_id_env",
        "aws_secret_access_key_env",
    }
    if require_operator_connections:
        allowed_keys.add("operator_connections")
    _reject_extra(raw, allowed_keys, "config")
    agent_name = _string(raw, "agent_name")
    if not AGENT_NAME_RE.fullmatch(agent_name):
        raise ConfigError("agent_name must be 1-50 characters of letters, numbers, '-' or '_'")
    aws_region = _string(raw, "aws_region")
    if not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", aws_region):
        raise ConfigError("aws_region must look like an AWS region, e.g. us-east-1")
    operator_connections: tuple[OperatorConnection, ...] | None = None
    if require_operator_connections:
        operator_connections = parse_operator_connections(_list(raw, "operator_connections"))
    return InputConfig(
        agent_name=agent_name,
        aws_region=aws_region,
        aws_access_key_id_env=_string(raw, "aws_access_key_id_env"),
        aws_secret_access_key_env=_string(raw, "aws_secret_access_key_env"),
        operator_connections=operator_connections,
    )


def parse_operator_connections(raw: list[Any]) -> tuple[OperatorConnection, ...]:
    if not raw:
        raise ConfigError("operator_connections must contain at least one connection")
    connections = tuple(parse_operator_connection(_list_object(raw, index)) for index in range(len(raw)))
    modes = [connection.mode for connection in connections]
    duplicates = sorted({mode for mode in modes if modes.count(mode) > 1})
    if duplicates:
        raise ConfigError(f"operator_connections must not contain duplicate modes: {', '.join(duplicates)}")
    return connections


def parse_operator_connection(raw: dict[str, Any]) -> OperatorConnection:
    mode = _string(raw, "mode")
    if mode == "ssh":
        _reject_extra(raw, {"mode", "ssh_public_key"}, "operator_connections[]")
        ssh_public_key = _string(raw, "ssh_public_key")
        if not (ssh_public_key.startswith("ssh-ed25519 ") or ssh_public_key.startswith("ssh-rsa ")):
            raise ConfigError("operator_connections[].ssh_public_key must be an OpenSSH public key")
        return OperatorConnection(mode=mode, ssh_public_key=ssh_public_key)
    if mode == "cloudflare_access":
        _reject_extra(raw, {"mode", "hostname", "tunnel_token_env"}, "operator_connections[]")
        hostname = _string(raw, "hostname").lower()
        if not EXACT_DOMAIN_RE.fullmatch(hostname):
            raise ConfigError("operator_connections[].hostname must be an exact domain like 'trustyclaw.example.com'")
        tunnel_token_env = _string(raw, "tunnel_token_env")
        if not ENV_NAME_RE.fullmatch(tunnel_token_env):
            raise ConfigError("operator_connections[].tunnel_token_env must be a valid environment variable name")
        return OperatorConnection(mode=mode, hostname=hostname, tunnel_token_env=tunnel_token_env)
    raise ConfigError("operator_connections[].mode must be 'ssh' or 'cloudflare_access'")


def runtime_operator_connections_from_input(
    connections: tuple[OperatorConnection, ...],
    env: Mapping[str, str],
) -> tuple[RuntimeOperatorConnection, ...]:
    runtime_connections: list[RuntimeOperatorConnection] = []
    for connection in connections:
        if connection.mode == "ssh":
            runtime_connections.append(
                RuntimeOperatorConnection(mode="ssh", ssh_public_key=connection.ssh_public_key)
            )
            continue
        if connection.mode == "cloudflare_access":
            if connection.tunnel_token_env is None:
                raise ConfigError("cloudflare_access connection is missing tunnel_token_env")
            token = env.get(connection.tunnel_token_env)
            if not token:
                raise ConfigError(
                    f"environment variable {connection.tunnel_token_env} is not set or is empty"
                )
            if any(character.isspace() for character in token):
                raise ConfigError(
                    f"environment variable {connection.tunnel_token_env} must contain a single Cloudflare tunnel token"
                )
            runtime_connections.append(
                RuntimeOperatorConnection(
                    mode="cloudflare_access",
                    hostname=connection.hostname,
                    tunnel_token=token,
                )
            )
            continue
        raise ConfigError(f"unsupported operator connection mode: {connection.mode}")
    return tuple(runtime_connections)


def public_operator_connections(connections: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    public: list[dict[str, Any]] = []
    for connection in connections:
        mode = connection.get("mode")
        if mode == "ssh":
            public.append({"mode": "ssh"})
        elif mode == "cloudflare_access":
            public.append({"mode": "cloudflare_access", "hostname": connection.get("hostname")})
    return public


def parse_network_controls(raw: dict[str, Any]) -> NetworkControls:
    _reject_extra(raw, {"managed_network_integrations", "allowed_network_access"}, "network_controls")
    managed_network_integrations = parse_managed_network_integrations(
        _object(raw, "managed_network_integrations", required=False)
    )
    allowed_raw = _object(raw, "allowed_network_access", required=False)
    allowed: dict[str, DomainRule] = {}
    for domain, rule_raw in allowed_raw.items():
        if not isinstance(domain, str) or not domain:
            raise ConfigError("allowed_network_access keys must be non-empty domain strings")
        if not (EXACT_DOMAIN_RE.fullmatch(domain) or WILDCARD_DOMAIN_RE.fullmatch(domain)):
            raise ConfigError(
                f"allowed_network_access[{domain!r}] must be an exact domain or wildcard like '*.example.com'"
            )
        normalized_domain = domain.lower()
        managed_owner = managed_domain_owner(normalized_domain)
        if managed_owner is not None:
            raise ConfigError(
                f"allowed_network_access[{domain!r}] is managed by managed_network_integrations.{managed_owner}; "
                f"remove this domain rule and configure network_controls.managed_network_integrations.{managed_owner}"
            )
        if normalized_domain in allowed:
            raise ConfigError(
                "allowed_network_access has duplicate domain rules after lowercase normalization: "
                f"{domain!r} conflicts with {normalized_domain!r}"
            )
        allowed[normalized_domain] = parse_domain_rule(_object(allowed_raw, domain), normalized_domain)
    _reject_overlapping_wildcards(allowed)
    return NetworkControls(
        managed_network_integrations=managed_network_integrations,
        allowed_network_access=allowed,
    )


def parse_managed_network_integrations(raw: dict[str, Any]) -> ManagedNetworkIntegrations:
    _reject_extra(raw, {"openai", "claude", "github", "python_packages", "npm_packages"}, "managed_network_integrations")
    return ManagedNetworkIntegrations(
        openai=parse_simple_managed_integration(_object(raw, "openai", required=False), "managed_network_integrations.openai"),
        claude=parse_simple_managed_integration(_object(raw, "claude", required=False), "managed_network_integrations.claude"),
        github=parse_github_integration(_object(raw, "github", required=False)),
        python_packages=parse_simple_managed_integration(
            _object(raw, "python_packages", required=False),
            "managed_network_integrations.python_packages",
        ),
        npm_packages=parse_simple_managed_integration(
            _object(raw, "npm_packages", required=False),
            "managed_network_integrations.npm_packages",
        ),
    )


def parse_simple_managed_integration(raw: dict[str, Any], context: str) -> ManagedIntegration:
    if not raw:
        return ManagedIntegration(False)
    _reject_extra(raw, {"enabled"}, context)
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{context}.enabled must be true or false")
    return ManagedIntegration(enabled)


def parse_github_integration(raw: dict[str, Any]) -> GitHubIntegration:
    if not raw:
        return GitHubIntegration(False)
    context = "managed_network_integrations.github"
    _reject_extra(raw, {"enabled", "write_repositories", "require_dot_github_approval"}, context)
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{context}.enabled must be true or false")
    require_dot_github_approval = raw.get("require_dot_github_approval", False)
    if not isinstance(require_dot_github_approval, bool):
        raise ConfigError(f"{context}.require_dot_github_approval must be true or false")
    write_repositories = parse_github_write_repositories(raw.get("write_repositories", []), context)
    # A disabled GitHub integration carries no other state.
    if not enabled and (write_repositories or require_dot_github_approval):
        raise ConfigError(f"{context}.write_repositories and require_dot_github_approval require enabled to be true")
    return GitHubIntegration(
        enabled=enabled, write_repositories=write_repositories, require_dot_github_approval=require_dot_github_approval
    )


def parse_github_write_repositories(raw: Any, context: str) -> tuple[GitHubRepository, ...]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ConfigError(f"{context}.write_repositories must be an array")
    repositories: list[GitHubRepository] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"{context}.write_repositories[{index}] must be an object")
        _reject_extra(item, {"owner", "repo"}, f"{context}.write_repositories[{index}]")
        owner = _string(item, "owner").lower()
        # Accept the commonly pasted "repo.git" form: enforcement strips the
        # suffix from request paths before matching, so a stored ".git" name
        # would never match anything (GitHub forbids real names ending .git).
        repo = _string(item, "repo").lower().removesuffix(".git")
        if not GITHUB_OWNER_RE.fullmatch(owner):
            raise ConfigError(f"{context}.write_repositories[{index}].owner is not a valid GitHub owner")
        if not GITHUB_REPO_RE.fullmatch(repo):
            raise ConfigError(f"{context}.write_repositories[{index}].repo is not a valid GitHub repository name")
        key = (owner, repo)
        if key in seen:
            raise ConfigError(f"{context}.write_repositories has duplicate repository {owner}/{repo}")
        seen.add(key)
        repositories.append(GitHubRepository(owner=owner, repo=repo))
    return tuple(repositories)


def expand_network_controls(controls: NetworkControls | dict[str, Any]) -> dict[str, Any]:
    """Return the root-proxy enforcement shape for parsed network controls.

    Parsing/storage preserve the operator-facing config exactly. Expansion is
    a separate in-memory enforcement step so managed provider domains cannot
    accidentally leak back into config/bootstrap inputs that will be parsed
    again.
    """
    parsed = controls if isinstance(controls, NetworkControls) else parse_network_controls(controls)
    policy = parsed.to_json()
    integrations = parsed.managed_network_integrations
    if integrations.openai.enabled:
        policy["allowed_network_access"] = {
            **policy["allowed_network_access"],
            **_openai_provider_rules_json(),
        }
    if integrations.claude.enabled:
        policy["allowed_network_access"] = {
            **policy["allowed_network_access"],
            **_claude_provider_rules_json(),
        }
    if integrations.github.enabled:
        policy["allowed_network_access"] = {
            **policy["allowed_network_access"],
            **_github_provider_rules_json(integrations.github),
        }
    if integrations.python_packages.enabled:
        policy["allowed_network_access"] = {
            **policy["allowed_network_access"],
            **_provider_rules_json(PYTHON_PACKAGE_PROVIDER_RULES),
        }
    if integrations.npm_packages.enabled:
        policy["allowed_network_access"] = {
            **policy["allowed_network_access"],
            **_provider_rules_json(NPM_PACKAGE_PROVIDER_RULES),
        }
    return policy


def parse_domain_rule(raw: dict[str, Any], domain: str) -> DomainRule:
    allowed_keys = {
        "allow_http_methods",
        "path_guards",
    }
    _reject_extra(raw, allowed_keys, f"allowed_network_access[{domain!r}]")
    methods = tuple(method.upper() for method in _string_list(raw, "allow_http_methods"))
    for method in methods:
        if method not in ALLOWED_HTTP_METHODS:
            raise ConfigError(f"allowed_network_access[{domain!r}].allow_http_methods has invalid method {method!r}")
    path_guards = tuple(_string_list(raw, "path_guards", required=False))
    for pattern in path_guards:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"allowed_network_access[{domain!r}].path_guards invalid regex {pattern!r}: {exc}") from exc
    return DomainRule(
        allow_http_methods=methods,
        path_guards=path_guards,
    )


def managed_domain_owner(domain: str) -> str | None:
    """The managed integration owning ``domain``, or None. A wildcard rule is
    owned both when it sits under a managed apex (``*.openai.com``) and when
    it would cover one (``*.com`` matches ``api.openai.com``), so a broad
    wildcard cannot smuggle unguarded access to managed domains."""
    wildcard = domain.startswith("*.")
    suffix = domain[2:] if wildcard else domain
    managed_suffixes = {
        "openai": ("openai.com", "chatgpt.com"),
        "claude": ("anthropic.com", "claude.ai", "claude.com"),
        "github": ("github.com", "githubusercontent.com"),
        "python_packages": ("pypi.org", "pythonhosted.org"),
        "npm_packages": ("npmjs.org", "nodejs.org"),
    }
    for owner, suffixes in managed_suffixes.items():
        for apex in suffixes:
            if suffix == apex or suffix.endswith(f".{apex}"):
                return owner
            if wildcard and apex.endswith(f".{suffix}"):
                return owner
    return None


def _openai_provider_rules_json() -> dict[str, dict[str, Any]]:
    return {
        domain: DomainRule(
            allow_http_methods=tuple(rule["allow_http_methods"]),
            path_guards=(),
            openai_disable_live_web_search=rule.get("openai_disable_live_web_search"),
            openai_account_guard=rule.get("openai_account_guard"),
        ).to_json()
        for domain, rule in OPENAI_PROVIDER_RULES.items()
    }


def _claude_provider_rules_json() -> dict[str, dict[str, Any]]:
    return {
        domain: DomainRule(
            allow_http_methods=tuple(rule["allow_http_methods"]),
            path_guards=tuple(rule.get("path_guards", ())),
            anthropic_account_guard=rule.get("anthropic_account_guard"),
        ).to_json()
        for domain, rule in CLAUDE_PROVIDER_RULES.items()
    }


def _provider_rules_json(rules: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        domain: DomainRule(
            allow_http_methods=tuple(rule["allow_http_methods"]),
            path_guards=tuple(rule.get("path_guards", ())),
        ).to_json()
        for domain, rule in rules.items()
    }


def _github_provider_rules_json(integration: GitHubIntegration) -> dict[str, dict[str, Any]]:
    write_repositories = [repo.to_json() for repo in integration.write_repositories]
    guard: dict[str, Any] = {"write_repositories": write_repositories}
    if integration.require_dot_github_approval:
        guard["require_dot_github_approval"] = True
    return {
        "github.com": DomainRule(
            allow_http_methods=("GET", "HEAD", "POST"),
            path_guards=(),
            github_repo_guard=guard,
        ).to_json(),
        "api.github.com": DomainRule(
            allow_http_methods=("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"),
            path_guards=(),
            github_repo_guard=guard,
        ).to_json(),
        "uploads.github.com": DomainRule(
            allow_http_methods=("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"),
            path_guards=(),
            github_repo_guard=guard,
        ).to_json(),
        # The read-only domains carry no repo guard: the guard only ever
        # gates writes, and GET/HEAD-only is the whole rule, structurally.
        # For the archive and raw-blob hosts every request is an allowed
        # read of any repository; for the signed-URL domains presigned S3
        # paths have no owner/repo to parse, and the signed URL is the
        # access control (minted only through an already-authorized
        # repo-scoped request).
        "codeload.github.com": DomainRule(
            allow_http_methods=("GET", "HEAD"),
            path_guards=(),
        ).to_json(),
        "raw.githubusercontent.com": DomainRule(
            allow_http_methods=("GET", "HEAD"),
            path_guards=(),
        ).to_json(),
        "objects.githubusercontent.com": DomainRule(
            allow_http_methods=("GET", "HEAD"),
            path_guards=(),
        ).to_json(),
        "github-cloud.githubusercontent.com": DomainRule(
            allow_http_methods=("GET", "HEAD"),
            path_guards=(),
        ).to_json(),
        "release-assets.githubusercontent.com": DomainRule(
            allow_http_methods=("GET", "HEAD"),
            path_guards=(),
        ).to_json(),
    }


def _reject_overlapping_wildcards(rules: dict[str, DomainRule]) -> None:
    wildcards = sorted(domain for domain in rules if domain.startswith("*."))
    for index, left in enumerate(wildcards):
        for right in wildcards[index + 1:]:
            if left.endswith(right[1:]) or right.endswith(left[1:]):
                raise ConfigError(
                    "allowed_network_access wildcard domains must not overlap: "
                    f"{left!r} and {right!r} can both match the same host"
                )


def _reject_extra(raw: dict[str, Any], allowed: set[str], context: str) -> None:
    extra = sorted(set(raw) - allowed)
    if extra:
        raise ConfigError(f"{context} has unsupported fields: {', '.join(extra)}")


def _object(raw: dict[str, Any], key: str, *, required: bool = True) -> dict[str, Any]:
    value = raw.get(key)
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be an object")
    return value


def _list(raw: dict[str, Any], key: str) -> list[Any]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise ConfigError(f"{key} must be an array")
    return value


def _list_object(raw: list[Any], index: int) -> dict[str, Any]:
    value = raw[index]
    if not isinstance(value, dict):
        raise ConfigError(f"operator_connections[{index}] must be an object")
    return value


def _string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _string_list(raw: dict[str, Any], key: str, *, required: bool = True) -> list[str]:
    value = raw.get(key)
    if value is None and not required:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{key} must be a string array")
    values: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{key} must be a string array")
        values.append(item.strip())
    return values
