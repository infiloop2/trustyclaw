from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from host.network_integrations.base import (
    IntegrationConfig,
    IntegrationConfigError,
)
from host.network_integrations.custom.manifest import CustomIntegration
from host.network_integrations.registry import NETWORK_INTEGRATIONS, managed_domain_owner


AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
EXACT_DOMAIN_RE = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
AGENT_RUNTIMES = {"codex", "claude_code", "pi", "hermes"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class NetworkControls:
    # One typed config per registered integration, keyed by integration id —
    # the same shape the operator configures under ``network_integrations``.
    # A disabled integration carries no state, so it serializes away entirely.
    integrations: dict[str, IntegrationConfig]

    def to_json(self) -> dict[str, Any]:
        serialized = {
            integration_id: config.to_json()
            for integration_id, config in self.integrations.items()
            if config.enabled
        }
        return {
            "network_integrations": {
                integration_id: value for integration_id, value in serialized.items()
            },
        }


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


def build_input_config(agent_name: str, aws_region: str) -> InputConfig:
    """Validate the CLI-supplied identity: the agent name and its region."""
    agent_name = agent_name.strip()
    if not AGENT_NAME_RE.fullmatch(agent_name):
        raise ConfigError("agent name must be 1-50 characters of letters, numbers, '-' or '_'")
    aws_region = aws_region.strip()
    if not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", aws_region):
        raise ConfigError("AWS region must look like an AWS region, e.g. us-east-1")
    return InputConfig(agent_name=agent_name, aws_region=aws_region)


def build_operator_connections(
    ssh_public_key: str | None,
    cloudflare_hostname: str | None,
    tunnel_token: str | None,
) -> tuple[RuntimeOperatorConnection, ...]:
    """Validate the CLI-supplied operator endpoints: at most one per mode,
    at least one overall. The tunnel token comes from the caller's fixed
    environment variable, never from an argument."""
    connections: list[RuntimeOperatorConnection] = []
    if ssh_public_key is not None:
        ssh_public_key = ssh_public_key.strip()
        if not (ssh_public_key.startswith("ssh-ed25519 ") or ssh_public_key.startswith("ssh-rsa ")):
            raise ConfigError("the operator SSH public key must be an OpenSSH public key")
        connections.append(RuntimeOperatorConnection(mode="ssh", ssh_public_key=ssh_public_key))
    if cloudflare_hostname is not None:
        hostname = cloudflare_hostname.strip().lower()
        if not EXACT_DOMAIN_RE.fullmatch(hostname):
            raise ConfigError(
                "the operator Cloudflare hostname must be an exact domain like 'trustyclaw.example.com'"
            )
        if not tunnel_token:
            raise ConfigError(
                "a Cloudflare operator endpoint needs the tunnel token; set the "
                "TRUSTYCLAW_CLOUDFLARE_TUNNEL_TOKEN environment variable"
            )
        if any(character.isspace() for character in tunnel_token):
            raise ConfigError(
                "TRUSTYCLAW_CLOUDFLARE_TUNNEL_TOKEN must contain a single Cloudflare tunnel token"
            )
        connections.append(
            RuntimeOperatorConnection(
                mode="cloudflare_access",
                hostname=hostname,
                tunnel_token=tunnel_token,
            )
        )
    if not connections:
        raise ConfigError(
            "at least one operator endpoint is required: pass --operator-ssh-public-key "
            "and/or --operator-cloudflare-hostname"
        )
    return tuple(connections)


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
    _reject_extra(raw, {"network_integrations"}, "network_controls")
    raw_integrations = _object(raw, "network_integrations", required=False)
    _reject_extra(raw_integrations, set(NETWORK_INTEGRATIONS), "network_integrations")
    configs: dict[str, IntegrationConfig] = {}
    for integration_id, registered in NETWORK_INTEGRATIONS.items():
        value = _object(raw_integrations, integration_id, required=False)
        try:
            configs[integration_id] = registered.parse(value)
        except IntegrationConfigError as exc:
            raise ConfigError(str(exc)) from exc
    custom = configs["custom"]
    assert isinstance(custom, CustomIntegration)
    for domain in custom.domains:
        managed_owner = managed_domain_owner(domain)
        if managed_owner is not None:
            raise ConfigError(
                f"network_integrations.custom.domains[{domain!r}] is owned by the "
                f"{managed_owner} integration; remove this domain rule and configure "
                f"network_integrations.{managed_owner}"
            )
    return NetworkControls(integrations=configs)


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
