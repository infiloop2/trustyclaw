"""Claude managed integration: static contract.

Opens the Anthropic API and the Claude Code OAuth path, pinned to the
configured account by bearer-token hash. The ``web_search`` option opts into
Anthropic's server-side web search; server-side web fetch, code execution,
and remote MCP servers are always denied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from host.network_integrations.base import (
    DenialReason,
    IntegrationConfigError,
    IntegrationManifest,
    reject_extra,
)

MANIFEST = IntegrationManifest(
    integration_id="claude",
    display_name="Claude",
    description=(
        "Claude Code access to the Anthropic API under the pinned account, plus the "
        "Claude Code OAuth login path. Anthropic server-side tools that reach external "
        "URLs (web fetch, code execution, remote MCP) are denied; server-side web search "
        "is allowed only when the operator enabled the web_search option."
    ),
    owned_apexes=("anthropic.com", "claude.ai", "claude.com"),
    denial_reasons=(
        DenialReason(
            "anthropic_token_unavailable",
            "The pinned Claude account token is not available yet (Claude Code login has not "
            "completed on this host), so API requests fail closed. Complete the Claude Code "
            "login or ask the operator to check the agent provider status.",
        ),
        DenialReason(
            "anthropic_token_required",
            "Requests to this domain must carry the bearer token of the configured Claude "
            "account. Use the managed Claude Code runtime, which sends it automatically.",
        ),
        DenialReason(
            "anthropic_token_mismatch",
            "The bearer token does not match the Claude account configured on this host. Only "
            "the configured account may be used.",
        ),
        DenialReason(
            "anthropic_body_undecodable",
            "The request body's Content-Encoding could not be decoded for inspection, so the "
            "request failed closed. Send the request uncompressed.",
        ),
        DenialReason(
            "anthropic_body_not_json",
            "The request body looked like JSON but did not parse, so it could not be inspected "
            "for server-tool declarations. Send valid JSON.",
        ),
        DenialReason(
            "anthropic_web_search_denied",
            "Anthropic server-side web search is disabled by operator policy. The operator can "
            "enable the Claude integration's web_search option in the admin UI's Network tab.",
        ),
        DenialReason(
            "anthropic_server_tool_denied",
            "Anthropic server-side tools that reach external URLs or run code off-box (web "
            "fetch, code execution) are always denied on this host. Remove the tool "
            "declaration.",
        ),
        DenialReason(
            "anthropic_remote_mcp_denied",
            "Remote MCP servers make Anthropic call an external server with request data and "
            "are always denied on this host. Remove the mcp_servers declaration.",
        ),
    ),
)


@dataclass(frozen=True)
class ClaudeIntegration:
    """When enabled, Claude Code reaches Anthropic under the pinned account.
    ``web_search`` opts into Anthropic's server-side web search, which runs on
    Anthropic infrastructure past the proxy; it is off by default. When off,
    the proxy denies the web_search tool declaration on api.anthropic.com.
    Server-side web fetch, code execution, and remote MCP servers are always
    denied regardless of this toggle."""

    enabled: bool
    web_search: bool = False

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {"enabled": self.enabled}
        if self.web_search:
            value["web_search"] = True
        return value


def parse(raw: dict[str, Any]) -> ClaudeIntegration:
    if not raw:
        return ClaudeIntegration(False)
    context = "network_integrations.claude"
    reject_extra(raw, {"enabled", "web_search"}, context)
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise IntegrationConfigError(f"{context}.enabled must be true or false")
    web_search = raw.get("web_search", False)
    if not isinstance(web_search, bool):
        raise IntegrationConfigError(f"{context}.web_search must be true or false")
    if not enabled and web_search:
        raise IntegrationConfigError(f"{context}.web_search requires enabled to be true")
    return ClaudeIntegration(enabled=enabled, web_search=web_search)
