"""Claude/Anthropic request guard: account pin and server-tool controls.

Runs in the proxy for hosts under the Claude apexes.

Claude Code OAuth requests to api.anthropic.com use opaque bearer tokens and
do not carry an OpenAI-style account header. The enforceable pin is therefore
the bearer credential hash read from the agent user's Claude credentials after
login. A tiny unauthenticated readiness path is allowed before the pin because
Claude Code probes it during startup.

Separately, Messages API requests may declare Anthropic server-side tools that
run on Anthropic's infrastructure and reach external URLs with request data —
web search (``web_search_*``), server-side web fetch (``web_fetch_*``), code
execution (``code_execution_*``), and remote MCP servers. The client's
WebFetch/Bash egress is already gated by the domain allow-list, but these
execute off-box, so the only enforcement point is the request that declares
them; the Claude integration denies them structurally.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from host.network_integrations.claude.manifest import ClaudeIntegration
from host.runtime.network_policy import decode_body, normalized_path, route_allowed
from host.runtime.state import read_proxy_claude_account

ANTHROPIC_PRE_PIN_BOOTSTRAP_GET_PATHS = {
    "/api/oauth/profile",
    "/api/oauth/claude_cli/roles",
    "/api/organization/claude_code_first_token_date",
    "/api/claude_code/policy_limits",
    "/api/claude_code/settings",
}
ROUTES = {
    "api.anthropic.com": (("GET", "POST"), ()),
    "platform.claude.com": (("GET", "POST"), (r"^/v1/oauth(?:/.*)?$",)),
}


def host_allowed(config: ClaudeIntegration, host: str) -> bool:
    del config
    return host.lower() in ROUTES


def request_denied(
    config: ClaudeIntegration,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> str | None:
    """Apply the Claude-owned route, account, and server-tool controls."""
    route = ROUTES.get(host.lower())
    if route is None or not route_allowed(method, path, query, *route):
        return "network_policy_denied"
    if host.lower() != "api.anthropic.com":
        return None
    denial = _server_tool_denial(headers, body, config.web_search)
    if denial is not None:
        return denial
    if method.upper() == "GET" and path == "/api/hello":
        return None
    presented = [
        bearer for key, value in headers
        if key.lower() == "authorization"
        for bearer in [_bearer_token(value)]
        if bearer is not None
    ]
    account = read_proxy_claude_account()
    expected_hash = account.get("access_token_sha256")
    if not isinstance(expected_hash, str) or not expected_hash:
        if _pre_pin_bootstrap_allowed(method, path, presented):
            return None
        return "anthropic_token_unavailable"
    if not presented:
        return "anthropic_token_required"
    if any(hashlib.sha256(value.encode()).hexdigest() != expected_hash for value in presented):
        return "anthropic_token_mismatch"
    return None


def _server_tool_denial(
    headers: list[tuple[str, str]], body: bytes, allow_web_search: bool
) -> str | None:
    """Deny a Messages API request that declares an Anthropic server-side tool
    reaching an external URL or running code off-box. Mirrors the OpenAI body
    guard: decode, confirm the body parses as JSON, then enforce structurally.
    Web search is allowed only when the operator opted in (``allow_web_search``);
    server web fetch, code execution, and remote MCP are always denied. A body
    that cannot be decoded or parsed as declared fails closed."""
    if not body:
        return None
    header_map = {key.lower(): value for key, value in headers}
    decoded = decode_body(body, header_map.get("content-encoding", ""))
    if decoded is None:
        return "anthropic_body_undecodable"
    body = decoded
    content_type = header_map.get("content-type", "").split(";", 1)[0].strip().lower()
    looks_json = content_type == "application/json" or body.lstrip().startswith((b"{", b"["))
    if not looks_json:
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "anthropic_body_not_json"
    return _tool_violation(payload, allow_web_search)


def _tool_violation(payload: Any, allow_web_search: bool) -> str | None:
    """The Messages API declares tools in a top-level ``tools`` array and remote
    MCP servers in a top-level ``mcp_servers`` array. Deny the server-side,
    off-box tool families by ``type`` prefix (dated variants such as
    ``web_search_20260209`` share the prefix); client-executed built-ins
    (``bash_*``, ``text_editor_*``, ``memory_*``) and user-defined tools (which
    carry a ``name`` but no ``type``) do not match and pass. ``web_search`` is
    permitted only when the operator enabled it; the others are always denied."""
    if not isinstance(payload, dict):
        return None
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_type = tool.get("type")
            if not isinstance(tool_type, str):
                continue
            if tool_type.startswith("web_search"):
                if not allow_web_search:
                    return "anthropic_web_search_denied"
            elif tool_type.startswith(("web_fetch", "code_execution")):
                return "anthropic_server_tool_denied"
    mcp_servers = payload.get("mcp_servers")
    if isinstance(mcp_servers, list) and mcp_servers:
        return "anthropic_remote_mcp_denied"
    return None


def _pre_pin_bootstrap_allowed(method: str, path: str, bearer_tokens: list[str]) -> bool:
    # Claude Code exchanges the browser OAuth code with platform.claude.com,
    # then calls a small set of api.anthropic.com profile/settings endpoints
    # before its credential file is ready for the host to hash and pin. Let
    # only those bearer-authenticated bootstrap reads through pre-pin; model
    # traffic such as /v1/messages still fails closed until the hash is stored.
    return (
        method.upper() == "GET"
        and normalized_path(path) in ANTHROPIC_PRE_PIN_BOOTSTRAP_GET_PATHS
        and bool(bearer_tokens)
    )


def _bearer_token(value: str) -> str | None:
    scheme, _, credential = value.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()
