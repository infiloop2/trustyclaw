"""Compute and store the effective host config during bootstrap.

Reads ``{"mode": <operation mode>, "runtime_config": <deploy payload config>}``
from stdin, runs as ``trustyclaw-admin`` after ``migrate up``. For ``deploy``
and ``reconfigure`` the admin password hash and operator connections come from
the payload; for ``upgrade`` and ``recover`` they are carried over from the
existing config table, so those operations never need (or accept) new
credentials. The validated result replaces the config table and is printed to
stdout so the root bootstrap can stage a copy for later root-only steps (SSH
key application, cloudflared) that run without database access.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from host.runtime.state import load_config, save_config

PAYLOAD_MODES = {"deploy", "reconfigure"}
CARRY_OVER_MODES = {"upgrade", "recover"}


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def validate_operator_connections(connections: Any) -> str | None:
    if not isinstance(connections, list) or not connections:
        return "runtime config is missing operator_connections"
    modes = []
    for connection in connections:
        if not isinstance(connection, dict):
            return "operator_connections entries must be objects"
        mode = connection.get("mode")
        modes.append(mode)
        if mode == "ssh":
            ssh_key = connection.get("ssh_public_key")
            if not isinstance(ssh_key, str) or not (
                ssh_key.startswith("ssh-ed25519 ") or ssh_key.startswith("ssh-rsa ")
            ):
                return "ssh operator connection is missing a valid ssh_public_key"
        elif mode == "cloudflare_access":
            hostname = connection.get("hostname")
            token = connection.get("tunnel_token")
            if not isinstance(hostname, str) or not hostname:
                return "cloudflare_access operator connection is missing hostname"
            if not isinstance(token, str) or not token or any(character.isspace() for character in token):
                return "cloudflare_access operator connection is missing a single-line tunnel_token"
        else:
            return f"unsupported operator connection mode: {mode!r}"
    if len(modes) != len(set(modes)):
        return "duplicate operator connection mode(s)"
    return None


def main() -> int:
    try:
        raw = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        return _fail(f"invalid JSON on stdin: {exc}")
    if not isinstance(raw, dict) or not isinstance(raw.get("runtime_config"), dict):
        return _fail("stdin must be an object with mode and runtime_config")
    mode = raw.get("mode")
    payload_config = raw["runtime_config"]
    if mode in CARRY_OVER_MODES:
        existing = load_config()
        admin_password_sha256 = existing.get("admin_password_sha256")
        operator_connections = existing.get("operator_connections")
    elif mode in PAYLOAD_MODES:
        admin_password_sha256 = payload_config.get("admin_password_sha256")
        operator_connections = payload_config.get("operator_connections")
    else:
        return _fail(f"unknown deploy operation mode {mode!r}")
    agent_name = payload_config.get("agent_name")
    if not isinstance(agent_name, str) or not agent_name:
        return _fail("runtime config is missing agent_name")
    if not isinstance(admin_password_sha256, str) or not admin_password_sha256:
        return _fail("runtime config is missing admin_password_sha256")
    error = validate_operator_connections(operator_connections)
    if error is not None:
        return _fail(error)
    runtime_config = {
        "agent_name": agent_name,
        "admin_password_sha256": admin_password_sha256,
        "operator_connections": operator_connections,
    }
    save_config(runtime_config)
    print(json.dumps(runtime_config, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
