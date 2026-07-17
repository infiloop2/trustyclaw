"""Shared types for network integrations.

Pure module: imported by ``host.config`` and every integration ``manifest.py``,
so it must not import ``host.runtime``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class DenialReason:
    """Catalog entry for one denial code — the stable snake_case string a
    guard returns, sent in the 403 body, and stored in network events. The
    guidance says what it means and what to do about it, written for the
    agent (and naming the operator action that would change the outcome)."""

    code: str
    guidance: str


class IntegrationConfig(Protocol):
    """The parsed, validated config of one integration: at minimum an
    ``enabled`` flag and the exact operator-facing JSON it round-trips to.
    A disabled integration carries no other state (validation enforces it),
    so serializers emit only enabled entries."""

    @property
    def enabled(self) -> bool: ...

    def to_json(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class IntegrationManifest:
    """The static contract of one network integration.

    ``owned_apexes`` are the fixed domain apexes the integration owns. The
    proxy dispatches those hosts to this integration even while it is disabled,
    so the custom-domain integration can never bypass a managed guard. Apex
    claims must be disjoint across the registry.

    ``denial_reasons`` catalogs every denial code the integration's guard can
    emit, with agent-facing guidance; the agent introspection tools join
    network events against it.
    """

    integration_id: str
    display_name: str
    description: str
    owned_apexes: tuple[str, ...]
    denial_reasons: tuple[DenialReason, ...] = ()


@dataclass(frozen=True)
class ManagedIntegration:
    """Config for an integration with no options beyond on/off."""

    enabled: bool

    def to_json(self) -> dict[str, Any]:
        return {"enabled": self.enabled}


class IntegrationConfigError(ValueError):
    """Invalid integration config. ``host.config`` re-raises it as
    ``ConfigError`` so operator-facing validation errors stay uniform."""


def reject_extra(raw: dict[str, Any], allowed: set[str], context: str) -> None:
    extra = sorted(set(raw) - allowed)
    if extra:
        raise IntegrationConfigError(f"{context} has unsupported fields: {', '.join(extra)}")


def parse_simple_integration(raw: dict[str, Any], context: str) -> ManagedIntegration:
    if not raw:
        return ManagedIntegration(False)
    reject_extra(raw, {"enabled"}, context)
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise IntegrationConfigError(f"{context}.enabled must be true or false")
    return ManagedIntegration(enabled)


# Denials produced by the proxy core rather than an integration guard: the
# domain/method/path decision, transport rules, and inspection limits. Kept
# next to the integration catalogs so the agent introspection tools serve one
# uniform reason lookup.
PROXY_DENIAL_REASONS: tuple[DenialReason, ...] = (
    DenialReason(
        "network_policy_denied",
        "No network policy rule allows this host, method, and path. The operator can add a "
        "custom-domain rule or enable a managed network integration covering it in the admin UI's "
        "Network tab.",
    ),
    DenialReason(
        "host_not_allowed",
        "The host is not in the allowed network policy, so the connection was refused before "
        "DNS resolution. The operator can add a custom-domain rule or enable a managed network "
        "integration covering it.",
    ),
    DenialReason(
        "network_policy_unavailable",
        "The stored network policy could not be loaded or parsed, so every request is denied "
        "until it is restored. The operator should check the host's health status.",
    ),
    DenialReason(
        "connect_port_denied",
        "Only HTTPS on port 443 is allowed. Reissue the request against the standard HTTPS "
        "port.",
    ),
    DenialReason(
        "plain_http_denied",
        "Plain HTTP is not supported; every allowed destination speaks HTTPS. Reissue the "
        "request over https://.",
    ),
    DenialReason(
        "request_target_invalid",
        "The request target was not origin-form. Use a standard HTTP client; hand-built "
        "request lines with absolute-form targets are refused.",
    ),
    DenialReason(
        "host_header_invalid",
        "The Host header was missing, duplicated, or did not match the connected host. Use a "
        "standard HTTP client that sends one matching Host header.",
    ),
    DenialReason(
        "request_body_malformed",
        "The request body framing (Content-Length or chunked encoding) was malformed, so the "
        "body could not be inspected. Resend with valid framing.",
    ),
    DenialReason(
        "request_body_too_large",
        "The request body exceeds the proxy's inspection limit (128 MiB). Split the upload or "
        "send less data per request.",
    ),
    DenialReason(
        "websocket_uninspectable",
        "A WebSocket message on this guarded domain could not be safely inspected (unsupported "
        "framing, extension, or size), so the connection was closed. Reconnect without "
        "extensions and keep messages under the inspection limit.",
    ),
)
