"""Shared types for network integrations.

Pure module: imported by ``host.config`` and every integration ``manifest.py``,
so it must not import ``host.runtime``.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any, Protocol

from host.param_guard import find_denial


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
_CORE_PROXY_DENIAL_REASONS: tuple[DenialReason, ...] = (
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




# --- Outbound request parameter guard on the proxy path ------------------
#
# The same deterministic guard that tools apply through
# ``HostAPI.outbound`` (host.param_guard), run here over the agent-authored
# free-text dimensions of managed-integration requests: decoded URL path
# segments and query values. Route allowlists stay authoritative; this adds
# content strictness on the one dimension they cannot constrain by shape.
# See docs/architecture/tools/outbound-request-filtering.md.

# Cataloged with the proxy-core reasons (codes are globally unique across
# the catalog); several integration guards emit these, so they live here
# rather than in any one integration's manifest.
_PARAM_GUARD_DENIAL_REASONS: tuple[DenialReason, ...] = (
    DenialReason(
        "request_param_too_large",
        "A request URL value exceeded the parameter guard's fixed length limit. "
        "Shorten the value and retry.",
    ),
    DenialReason(
        "request_param_encoded_blob_denied",
        "A request URL value looked like an encoded payload (control characters, "
        "an overlong unbroken token, or a random-looking string). Rewrite it as "
        "plain text and retry.",
    ),
    DenialReason(
        "request_param_secret_denied",
        "A request URL value appeared to contain a secret or credential (API key, "
        "token, private key, password, or similar). Remove it and retry; secrets "
        "must never ride in request parameters.",
    ),
    DenialReason(
        "request_param_pii_denied",
        "A request URL value appeared to contain a personal or financial "
        "identifier (email, phone, card, account, or code). Remove it and retry.",
    ),
)


def request_param_denial(path: str, query: str, *, token_rules: bool = True) -> str | None:
    """Run the parameter guard over a managed-integration request's URL and
    return the denial code for the first finding, else None.

    The whole reconstructed URL (`https://host<path>?<query>`) is decoded and
    scanned as one value rather than parsing path segments and query pairs
    individually. This keeps the proxy path simple and still enforces the
    credential-named-query-key rule, because scanning a full URL routes
    through the same `CRED_URL` guard (G10) that parses the query and denies
    a credential key carrying a long value - so `?access_token=<16+ chars>`
    is caught without the proxy reparsing anything. The unbroken URL is a
    plain `https` URL, so the token-length rule does not fire on it; long or
    encoded payloads inside a path segment are caught by the unnatural-token
    rule instead.

    Percent-decoding is strict: bytes that are not valid UTF-8 would be
    smoothed into replacement characters by lenient decoding (and pass the
    printable rule) while the raw bytes still went upstream - a binary
    exfiltration channel - so invalid encodings deny outright.
    """
    raw = "https://host" + path
    if query:
        raw += "?" + query
    try:
        decoded = urllib.parse.unquote(raw, errors="strict")
    except UnicodeDecodeError:
        return "request_param_encoded_blob_denied"
    denial = find_denial(decoded, token_rules=token_rules)
    return denial.reason if denial is not None else None


PROXY_DENIAL_REASONS: tuple[DenialReason, ...] = (
    _CORE_PROXY_DENIAL_REASONS + _PARAM_GUARD_DENIAL_REASONS
)
