"""Admin-side access to the network-control state shared with the proxy.

Policy, provider account pins, and network events all live in the database
now: the admin service (schema owner) reads and writes them directly, and the
proxy role holds read-only grants (plus insert on its own event table). The
root-owned sudo helpers that used to bridge the admin/proxy file boundary for
this state are gone; the remaining helpers deal only with the agent user's
home and privileged host actions.
"""

from __future__ import annotations

import json
from typing import Any

from host.config import ConfigError, parse_network_controls
from host.runtime import pgclient
from host.runtime.network_policy import load_policy, load_policy_updated_at
from host.runtime.state import (
    EVENT_PAGE_LIMIT,
    page_network_events_before,
    save_proxy_claude_account,
    save_proxy_openai_account_id,
)


def network_status() -> str:
    # pgclient.Error covers an unreadable/unavailable policy table while the
    # rest of the database still works: /v1/health must report the degraded
    # network status, not fail with a 500.
    try:
        parse_network_controls(load_policy())
    except (ConfigError, pgclient.Error, json.JSONDecodeError, OSError, KeyError, TypeError):
        return "error"
    return "active"


def network_policy_response() -> dict[str, Any]:
    return {"network_controls": load_policy(), "updated_at": load_policy_updated_at()}


def network_policy() -> dict[str, Any]:
    value = network_policy_response().get("network_controls")
    return value if isinstance(value, dict) else {}


def network_events_before(
    before: int | None,
    *,
    decision: str | None = None,
    limit: int = EVENT_PAGE_LIMIT,
) -> list[dict[str, Any]]:
    return page_network_events_before(before, decision=decision, limit=limit).items


def sync_openai_account_id(account_id: str | None) -> None:
    save_proxy_openai_account_id(account_id)


def sync_claude_account(account: dict[str, Any] | None) -> None:
    save_proxy_claude_account(account)
