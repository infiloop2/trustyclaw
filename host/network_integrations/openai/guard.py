"""OpenAI request guard: account pin and external-URL enforcement.

Runs in the proxy for hosts under the OpenAI apexes. The integration owns its
exact hosts and methods plus two request controls:

Data-plane traffic must carry the ``chatgpt-account-id`` header matching the
account pinned from Codex login status, failing closed while that id is
unavailable. Requests that would make the upstream reach an external URL with
request data are denied while cache-backed search remains allowed. The upstream
enables search and remote MCP only from parsed request fields, so prompt text
that merely mentions a tool name carries no capability and enforcement is
structural. The same decision applies per-message on WebSocket connections.
"""

from __future__ import annotations

import json
from typing import Any

from host.network_integrations.base import ManagedIntegration
from host.runtime.core.network_policy import decode_body, normalized_path, route_allowed
from host.runtime.core.state import read_proxy_openai_account_id

# Codex standalone web search endpoints (code-mode models search here instead
# of declaring a Responses web_search tool). The request must opt into cached
# retrieval via settings.external_web_access; the server default is live.
OPENAI_SEARCH_PATHS = {"/backend-api/codex/alpha/search", "/v1/alpha/search"}
# Hosted tools that make the upstream reach the open web, run server-side code
# with network egress, or drive a remote browser, but whose ``type`` does not
# begin with ``web``. They have no cache-backed form here and are denied
# outright, like ``web_search_preview``. code_interpreter is included because
# OpenAI's hosted container can egress; Codex runs code in its own local
# sandbox and never declares it, so denying it costs nothing and fails closed.
_DENIED_HOSTED_TOOL_TYPES = frozenset(
    {"browser", "computer_use", "computer_use_preview", "code_interpreter"}
)
ROUTES = {
    "api.openai.com": (("POST",), ()),
    "auth.openai.com": (("GET", "POST"), ()),
    "chatgpt.com": (("GET", "POST"), ()),
}
GUARDED_HOSTS = frozenset({"api.openai.com", "chatgpt.com"})


def host_allowed(config: ManagedIntegration, host: str) -> bool:
    del config
    return host.lower() in ROUTES


def request_denied(
    config: ManagedIntegration,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> str | None:
    """Apply the OpenAI-owned route, account, and body controls."""
    del config
    route = ROUTES.get(host.lower())
    if route is None or not route_allowed(method, path, query, *route):
        return "network_policy_denied"
    if host.lower() not in GUARDED_HOSTS:
        return None
    account_id = read_proxy_openai_account_id()
    if not account_id:
        return "openai_account_unavailable"
    # The header must be present AND match. A missing header is a denial:
    # otherwise the agent omits it and OpenAI resolves the account from the
    # token, defeating the pin.
    presented = [value for key, value in headers if key.lower() == "chatgpt-account-id"]
    if not presented:
        return "openai_account_header_required"
    if any(value != account_id for value in presented):
        return "openai_account_mismatch"
    return _external_url_request_denial(headers, body, path)


def ws_inspection_required(config: ManagedIntegration, host: str) -> bool:
    """Whether WebSocket messages to ``host`` must be inspected. Only the
    external URL request guard depends on message bodies; the other controls
    (methods, paths, account pin) are fully decided at the handshake."""
    del config
    return host.lower() in GUARDED_HOSTS


def ws_message_denied(payload: bytes) -> str | None:
    """Apply the external URL request guard to one complete WebSocket message,
    mirroring the HTTP body check. WS payloads carry no Content-Encoding or
    Content-Type, so the message is inspected as-is. Only called for guarded
    hosts: the proxy gates on ws_inspection_required at the handshake and
    tunnels everything else opaquely."""
    return _external_url_request_denial([], payload)


def _external_url_request_denial(
    headers: list[tuple[str, str]],
    body: bytes,
    path: str | None = None,
) -> str | None:
    """Block requests that would make the upstream reach an external URL with
    request data, while allowing cache-backed search: see
    _external_url_request_violation for the rule, plus the standalone search
    endpoints, which must opt into cached retrieval because the server default
    there is live."""
    if not body:
        return None
    header_map = {key.lower(): value for key, value in headers}
    decoded = decode_body(body, header_map.get("content-encoding", ""))
    if decoded is None:
        return "openai_body_undecodable"
    body = decoded
    content_type = header_map.get("content-type", "").split(";", 1)[0].strip().lower()
    looks_json = content_type == "application/json" or body.lstrip().startswith((b"{", b"["))
    if not looks_json:
        # The upstream parses requests as JSON; a body it cannot parse cannot
        # declare tools, whatever its content-type label.
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "openai_body_not_json"

    denial = _external_url_request_violation(payload)
    if denial is not None:
        return denial

    if path is not None and normalized_path(path).rstrip("/") in OPENAI_SEARCH_PATHS:
        settings = payload.get("settings") if isinstance(payload, dict) else None
        if not isinstance(settings, dict):
            settings = {}
        if settings.get("external_web_access") is not False:
            return "openai_web_tool_denied"
        if settings.get("indexed_web_access") not in (False, None):
            return "openai_web_tool_denied"
    return None


def _external_url_request_violation(payload: Any) -> str | None:
    """The upstream must never reach an external URL with request data. Web
    content may be retrieved cache-backed only: the sole permitted web tool is
    exactly ``web_search`` with ``external_web_access`` false *and*
    ``indexed_web_access`` false or absent. Everything else this collects is
    denied, so the rule fails closed: ``web_search_preview`` and dated variants
    (they browse live), a bare ``web`` / ``web_fetch`` tool, ``browser`` /
    ``computer_use`` (a driven browser), ``code_interpreter`` (a hosted
    container that can egress), any tool object carrying a truthy
    ``*_web_access`` flag under a different type — a renamed web tool — and
    remote MCP tools (``type: mcp``, by ``server_url`` or hosted
    ``connector_id``). Chat Completions search (``web_search_options``,
    ``*-search*`` models) has no cached form and is denied outright."""
    for tool in _iter_tool_objects(payload):
        denial = _tool_object_violation(tool)
        if denial is not None:
            return denial
    if _contains_key(payload, "server_url"):
        return "openai_remote_mcp_denied"
    if _contains_key(payload, "web_search_options"):
        return "openai_web_tool_denied"
    model = payload.get("model") if isinstance(payload, dict) else None
    if isinstance(model, str) and "-search" in model:
        return "openai_web_tool_denied"
    return None


def _tool_object_violation(tool: dict[str, Any]) -> str | None:
    """Decide a single guarded tool object. Only the cached ``web_search`` shape
    passes; any other collected tool is denied, so a renamed or newly added
    web/browse tool fails closed instead of being forwarded."""
    tool_type = tool.get("type")
    if tool_type == "mcp":
        return "openai_remote_mcp_denied"
    if tool_type != "web_search":
        # web_search_preview and dated variants, a bare
        # web/web_fetch/browser/computer_use/code_interpreter tool, or one that
        # only matched on a *_web_access flag: none has a cache-backed form.
        return "openai_web_tool_denied"
    if tool.get("external_web_access") is not False:
        return "openai_web_tool_denied"
    if tool.get("indexed_web_access") not in (False, None):
        return "openai_web_tool_denied"
    for key, value in tool.items():
        if (
            isinstance(key, str)
            and key.endswith("_web_access")
            and key not in ("external_web_access", "indexed_web_access")
            and value not in (False, None)
        ):
            return "openai_web_tool_denied"
    return None


def _contains_key(payload: Any, key: str) -> bool:
    if isinstance(payload, dict):
        if key in payload:
            return True
        return any(_contains_key(value, key) for value in payload.values())
    if isinstance(payload, list):
        return any(_contains_key(item, key) for item in payload)
    return False


def _iter_tool_objects(payload: Any) -> list[dict[str, Any]]:
    """Collect every guarded tool object anywhere in the request, so a tool
    nested under any key is still inspected. Guarded means: a remote-MCP tool
    (``type: mcp``); a web/browse tool named by its type — any ``type`` starting
    with ``web`` (covering ``web_search``, dated ``web_search_preview`` variants,
    and a bare ``web``/``web_fetch``) or a ``_DENIED_HOSTED_TOOL_TYPES`` member
    (``browser``/``computer_use``/``code_interpreter``); or, so
    a renamed web tool still fails closed, any typed object that carries a
    *truthy* ``*_web_access`` flag (a false/absent flag grants no access, so a
    safe tool is not swept in). web_search_call is excluded: it is a history item
    replaying an earlier search, not a tool declaration, and appears in
    legitimate cached-search requests; mcp_call and mcp_list_tools history item
    types do not match either. A ``type``-less object such as the standalone
    search ``settings`` block is not collected here — the endpoint check in
    _external_url_request_denial covers it."""
    matches: list[dict[str, Any]] = []

    def is_guarded(node: dict[str, Any]) -> bool:
        type_value = node.get("type")
        if not isinstance(type_value, str) or type_value == "web_search_call":
            return False
        if type_value == "mcp" or type_value.startswith("web") or type_value in _DENIED_HOSTED_TOOL_TYPES:
            return True
        # A renamed web tool: guarded only if it actually requests access via a
        # truthy ``*_web_access`` flag. A false/default flag on an otherwise safe
        # tool (e.g. a function tool that carries ``external_web_access: false``)
        # grants nothing and must not be swept in and then denied for its type.
        return any(
            isinstance(key, str) and key.endswith("_web_access") and value not in (False, None)
            for key, value in node.items()
        )

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if is_guarded(node):
                matches.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return matches
