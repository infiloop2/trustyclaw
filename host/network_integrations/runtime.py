"""One proxy dispatch path for every network integration.

Fixed managed apexes select their integration even while disabled. Every other
host belongs to the custom-domain integration. The selected integration gets
its typed config and decides host, method, path, and specialized request rules
directly; there is no expanded cross-integration policy shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from host.config import NetworkControls
from host.network_integrations.bedrock import guard as bedrock_guard
from host.network_integrations.claude import guard as claude_guard
from host.network_integrations.custom import guard as custom_guard
from host.network_integrations.github import guard as github_guard
from host.network_integrations.npm_packages import guard as npm_guard
from host.network_integrations.openai import guard as openai_guard
from host.network_integrations.python_packages import guard as python_guard
from host.network_integrations.registry import managed_domain_owner

HostAllowed = Callable[[Any, str], bool]
RequestDenied = Callable[
    [Any, str, str, str, str, list[tuple[str, str]], bytes], str | None
]
RewriteRequestHeaders = Callable[
    [Any, str, str, str, str, list[tuple[str, str]], bytes], list[tuple[str, str]]
]


@dataclass(frozen=True)
class IntegrationGuard:
    """One integration's request-time hooks. The dispatch layer owns the
    ``config.enabled`` gate: every hook is invoked only for an enabled config
    (a disabled integration is denied here), and ``gate_response``,
    ``rewrite_request_headers``, and the WebSocket hooks additionally run only
    after ``request_denied`` allowed the request."""

    host_allowed: HostAllowed
    request_denied: RequestDenied
    rewrite_request_headers: RewriteRequestHeaders | None = None
    gate_response: Callable[[Any, str, str, str, bytes], tuple[bytes | None, str | None]] | None = None
    ws_inspection_required: Callable[[Any, str], bool] | None = None
    ws_message_denied: Callable[[bytes], str | None] | None = None
    response_meter: Callable[[Any, str, str, str, str, list[tuple[str, str]], bytes], Any] | None = None


GUARDS: dict[str, IntegrationGuard] = {
    "openai": IntegrationGuard(
        host_allowed=openai_guard.host_allowed,
        request_denied=openai_guard.request_denied,
        ws_inspection_required=openai_guard.ws_inspection_required,
        ws_message_denied=openai_guard.ws_message_denied,
    ),
    "claude": IntegrationGuard(
        host_allowed=claude_guard.host_allowed,
        request_denied=claude_guard.request_denied,
    ),
    "bedrock": IntegrationGuard(
        host_allowed=bedrock_guard.host_allowed,
        request_denied=bedrock_guard.request_denied,
        rewrite_request_headers=bedrock_guard.rewrite_request_headers,
        response_meter=bedrock_guard.response_meter,
    ),
    "github": IntegrationGuard(
        host_allowed=github_guard.host_allowed,
        request_denied=github_guard.request_denied,
        rewrite_request_headers=github_guard.rewrite_request_headers,
        gate_response=github_guard.gate_response,
    ),
    "python_packages": IntegrationGuard(
        host_allowed=python_guard.host_allowed,
        request_denied=python_guard.request_denied,
    ),
    "npm_packages": IntegrationGuard(
        host_allowed=npm_guard.host_allowed,
        request_denied=npm_guard.request_denied,
    ),
    "custom": IntegrationGuard(
        host_allowed=custom_guard.host_allowed,
        request_denied=custom_guard.request_denied,
    ),
}


def _selection(controls: NetworkControls, host: str) -> tuple[IntegrationGuard, Any]:
    integration_id = managed_domain_owner(host.lower()) or "custom"
    return GUARDS[integration_id], controls.integrations[integration_id]


def host_allowed(controls: NetworkControls, host: str) -> bool:
    guard, config = _selection(controls, host)
    return config.enabled and guard.host_allowed(config, host)


def request_denied(
    controls: NetworkControls,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> str | None:
    guard, config = _selection(controls, host)
    if not config.enabled:
        return "network_policy_denied"
    return guard.request_denied(config, method, host, path, query, headers, body)


def gate_response(
    controls: NetworkControls, method: str, host: str, path: str, body: bytes
) -> tuple[bytes | None, str | None]:
    guard, config = _selection(controls, host)
    if not config.enabled or guard.gate_response is None:
        return None, None
    return guard.gate_response(config, method, host, path, body)


def rewrite_request_headers(
    controls: NetworkControls,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> list[tuple[str, str]]:
    guard, config = _selection(controls, host)
    if guard.rewrite_request_headers is None:
        return headers
    return guard.rewrite_request_headers(config, method, host, path, query, headers, body)


def response_meter(
    controls: NetworkControls,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> Any:
    """The owning integration's passive response observer for this allowed
    request (an object with ``feed(bytes)`` and ``finish()``), or None. Only
    Bedrock defines one: it turns the token usage AWS reports in each
    response into the live per-runtime usage counters."""
    guard, config = _selection(controls, host)
    if not config.enabled or guard.response_meter is None:
        return None
    return guard.response_meter(config, method, host, path, query, headers, body)


def ws_message_guard(
    controls: NetworkControls, host: str
) -> Callable[[bytes], str | None] | None:
    guard, config = _selection(controls, host)
    if not config.enabled or guard.ws_inspection_required is None or guard.ws_message_denied is None:
        return None
    return guard.ws_message_denied if guard.ws_inspection_required(config, host) else None
