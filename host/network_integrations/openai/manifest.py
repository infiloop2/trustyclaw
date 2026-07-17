"""OpenAI managed integration: static contract.

Opens the OpenAI/ChatGPT domains for the Codex runtime, pinned to the
configured account and restricted to cache-backed web retrieval (the guard
denies any request that would make OpenAI reach an external URL with request
data).
"""

from __future__ import annotations

from typing import Any

from host.network_integrations.base import (
    DenialReason,
    IntegrationManifest,
    ManagedIntegration,
    parse_simple_integration,
)

MANIFEST = IntegrationManifest(
    integration_id="openai",
    display_name="OpenAI",
    description=(
        "Codex runtime access to the OpenAI API and ChatGPT backend, pinned to the "
        "configured account. Web content may be retrieved cache-backed only: live web "
        "search, hosted browsing/code tools, and remote MCP servers are denied."
    ),
    owned_apexes=("openai.com", "chatgpt.com"),
    denial_reasons=(
        DenialReason(
            "openai_account_unavailable",
            "The pinned OpenAI account id is not available yet (Codex login has not completed "
            "on this host), so account-guarded requests fail closed. Complete the Codex login "
            "or ask the operator to check the agent provider status.",
        ),
        DenialReason(
            "openai_account_header_required",
            "Requests to this domain must carry the chatgpt-account-id header matching the "
            "configured account. Use the managed Codex runtime, which sends it automatically.",
        ),
        DenialReason(
            "openai_account_mismatch",
            "The chatgpt-account-id header does not match the account configured on this host. "
            "Only the configured OpenAI account may be used.",
        ),
        DenialReason(
            "openai_body_undecodable",
            "The request body's Content-Encoding could not be decoded for inspection, so the "
            "request failed closed. Send the request uncompressed.",
        ),
        DenialReason(
            "openai_body_not_json",
            "The request body looked like JSON but did not parse, so it could not be inspected "
            "for web-tool declarations. Send valid JSON.",
        ),
        DenialReason(
            "openai_remote_mcp_denied",
            "Remote MCP tools make OpenAI call an external server with request data and are "
            "always denied on this host. Remove the MCP tool declaration.",
        ),
        DenialReason(
            "openai_web_tool_denied",
            "Only cached web search (web_search with external_web_access: false) is allowed; "
            "web_search_preview, live/browsing tools, hosted code execution, and search models "
            "are denied. Reissue with the cached web_search tool shape.",
        ),
    ),
)


def parse(raw: dict[str, Any]) -> ManagedIntegration:
    return parse_simple_integration(raw, "network_integrations.openai")
