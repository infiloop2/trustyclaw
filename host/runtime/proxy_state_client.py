"""Admin-side client for narrow root helpers that cross into proxy-owned state."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from host.config import ConfigError, parse_network_controls
from host.runtime.network_policy import load_policy, load_policy_updated_at
from host.runtime.state import page_network_events

READ_NETWORK_STATE_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/read-network-state"]
UPDATE_PROVIDER_ACCOUNT_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/update-provider-account"]
HELPER_TIMEOUT_SECONDS = 10


def _helper_available(command: list[str]) -> bool:
    # Production helpers live under a root-owned directory that service users
    # need not be able to stat. In tests/local harnesses TRUSTYCLAW_STATE_DIR is
    # set without installing sudo helpers, so keep the direct fallback there.
    return "TRUSTYCLAW_STATE_DIR" not in os.environ


def _run_json(command: list[str], *, input_value: dict[str, Any] | None = None) -> dict[str, Any]:
    proc = subprocess.run(
        command,
        input=json.dumps(input_value) if input_value is not None else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=HELPER_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"{command[-1]} failed")
    value = json.loads(proc.stdout)
    if not isinstance(value, dict):
        raise RuntimeError(f"{command[-1]} returned invalid JSON")
    return value


def network_status() -> str:
    if _helper_available(READ_NETWORK_STATE_COMMAND):
        value = _run_json([*READ_NETWORK_STATE_COMMAND, "status"]).get("status")
        return value if isinstance(value, str) else "error"
    try:
        parse_network_controls(load_policy())
    except (ConfigError, json.JSONDecodeError, OSError, KeyError, TypeError):
        return "error"
    return "active"


def network_policy_response() -> dict[str, Any]:
    if _helper_available(READ_NETWORK_STATE_COMMAND):
        return _run_json([*READ_NETWORK_STATE_COMMAND, "policy"])
    return {"network_controls": load_policy(), "updated_at": load_policy_updated_at()}


def network_policy() -> dict[str, Any]:
    value = network_policy_response().get("network_controls")
    return value if isinstance(value, dict) else {}


def network_events(since: int | None) -> list[dict[str, Any]]:
    if _helper_available(READ_NETWORK_STATE_COMMAND):
        command = [*READ_NETWORK_STATE_COMMAND, "events"]
        if since is not None:
            command.append(str(since))
        value = _run_json(command).get("events")
        return value if isinstance(value, list) else []
    return page_network_events(since).items


def sync_openai_account_id(account_id: str | None) -> None:
    payload = {"provider": "openai", "account_id": account_id}
    if _helper_available(UPDATE_PROVIDER_ACCOUNT_COMMAND):
        _run_json(UPDATE_PROVIDER_ACCOUNT_COMMAND, input_value=payload)
    else:
        from host.runtime.state import save_proxy_openai_account_id

        save_proxy_openai_account_id(account_id)


def sync_claude_account(account: dict[str, Any] | None) -> None:
    payload = {"provider": "claude", "account": account}
    if _helper_available(UPDATE_PROVIDER_ACCOUNT_COMMAND):
        _run_json(UPDATE_PROVIDER_ACCOUNT_COMMAND, input_value=payload)
    else:
        from host.runtime.state import save_proxy_claude_account

        save_proxy_claude_account(account)
