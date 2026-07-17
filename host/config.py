from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import Any, Mapping

from host.network_integrations.base import (
    IntegrationConfig,
    IntegrationConfigError,
)
from host.network_integrations.custom.manifest import CustomIntegration
from host.network_integrations.registry import NETWORK_INTEGRATIONS, managed_domain_owner


AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
EXACT_DOMAIN_RE = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
AGENT_RUNTIMES = {"codex", "claude_code"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class NetworkControls:
    # One typed config per registered integration, keyed by integration id —
    # the same shape the operator configures under ``network_integrations``.
    # A disabled integration carries no state, so it serializes away entirely.
    integrations: dict[str, IntegrationConfig]

    def to_json(self) -> dict[str, Any]:
        return {
            "network_integrations": {
                integration_id: config.to_json()
                for integration_id, config in self.integrations.items()
                if config.enabled
            },
        }


@dataclass(frozen=True)
class OperatorConnection:
    mode: str
    ssh_public_key: str | None = None
    hostname: str | None = None
    tunnel_token_env: str | None = None

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
    aws_session_token_env: str | None
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
        "aws_session_token_env",
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
    aws_session_token_env: str | None = None
    if "aws_session_token_env" in raw:
        aws_session_token_env = _string(raw, "aws_session_token_env")
        if not ENV_NAME_RE.fullmatch(aws_session_token_env):
            raise ConfigError("aws_session_token_env must be a valid environment variable name")
    return InputConfig(
        agent_name=agent_name,
        aws_region=aws_region,
        aws_access_key_id_env=_string(raw, "aws_access_key_id_env"),
        aws_secret_access_key_env=_string(raw, "aws_secret_access_key_env"),
        aws_session_token_env=aws_session_token_env,
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
