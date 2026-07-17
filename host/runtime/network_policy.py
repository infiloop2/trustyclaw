"""Network policy access and core request decisions.

The active policy lives in the network policy tables. The admin service
(schema owner) replaces it after validation; the proxy role can only read it.
A missing policy is the fail-closed empty default, and a database outage
denies every request (no fallback cache; see ``network_proxy``).

This module is the provider-agnostic core: policy load plus shared request
helpers (method/path matching, path normalization, bounded body decoding)
that integration guards build on. Each integration owns its reachable hosts
and request decision directly; no expanded generic rule blob exists.
"""

from __future__ import annotations

import gzip
import io
import posixpath
import re
import zlib
from typing import Any
from urllib.parse import unquote

from host.config import parse_network_controls
from host.runtime.state import network_policy_record

# Matches the proxy's MAX_BODY_BYTES: decompressed output is capped so a
# decompression bomb cannot exhaust the proxy's memory.
MAX_DECODED_BODY_BYTES = 128 * 1024 * 1024


def load_policy() -> dict[str, Any]:
    record = network_policy_record()
    if record is None:
        return {"network_integrations": {}}
    controls = record["controls"]
    return controls if isinstance(controls, dict) else {}


def managed_integration(name: str, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """One integration's stored dict ({} when absent) — the single reader
    every consumer derives from instead of re-parsing the policy."""
    if policy is None:
        policy = load_policy()
    integrations = policy.get("network_integrations")
    if not isinstance(integrations, dict):
        return {}
    integration = integrations.get(name)
    return integration if isinstance(integration, dict) else {}


def network_policy_response() -> dict[str, Any]:
    """The operator-facing policy view: stored controls plus updated_at."""
    record = network_policy_record()
    if record is None:
        return {
            "network_controls": {"network_integrations": {}},
            "updated_at": None,
        }
    controls = record["controls"]
    return {
        "network_controls": controls if isinstance(controls, dict) else {},
        "updated_at": record["updated_at"],
    }


def network_status() -> str:
    """Whether the stored policy is enforceable. Any failure — an unreadable
    or unavailable policy table, malformed controls — is the degraded status:
    /v1/health must report it, not 500 (the same any-failure-denies posture
    as the proxy's own policy load)."""
    try:
        parse_network_controls(load_policy())
    except Exception:
        return "error"
    return "active"


def route_allowed(
    method: str,
    path: str,
    query: str,
    allow_http_methods: tuple[str, ...] | frozenset[str],
    path_guards: tuple[str, ...] = (),
) -> bool:
    """Shared method/path matching for an integration-owned route."""
    if method.upper() not in allow_http_methods:
        return False
    if not path_guards:
        return True
    # Match against the path the origin server will actually resolve: decode
    # percent-escapes and collapse ./ and ../ first, so a guard like
    # ^/v1/threads/.* cannot be bypassed with /v1/threads/../../admin (which the
    # upstream would serve as /admin).
    target = normalized_path(path) + (f"?{query}" if query else "")
    return any(re.fullmatch(pattern, target) for pattern in path_guards)


def normalized_path(path: str) -> str:
    """The path the origin server will actually resolve: percent-escapes
    decoded and ``./``/``../`` segments collapsed. Every security comparison
    against a request path — core path guards and integration guards alike —
    goes through this first."""
    decoded = unquote(path or "/")
    normalized = posixpath.normpath(decoded)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if path.endswith("/") and not normalized.endswith("/"):
        normalized += "/"
    return normalized


def decode_body(body: bytes, content_encoding: str) -> bytes | None:
    """Decode a Content-Encoding so a body guard inspects real JSON, not a
    compressed blob. Only the stdlib-decodable encodings are supported;
    anything else (zstd, br, corrupt streams) returns None, which callers
    treat as fail-closed. Decompressed output is bounded so a decompression
    bomb cannot exhaust the proxy's memory. Clients essentially never compress
    request bodies and the agent CLIs are version-pinned, so a live denial is
    the signal to add an encoding, not a fallback decoder."""
    encoding = content_encoding.strip().lower()
    if encoding in ("", "identity"):
        return body
    if encoding == "gzip":
        return _bounded_gzip_decompress(body)
    if encoding in ("deflate", "zlib"):
        return _bounded_zlib_decompress(body)
    return None


def _bounded_gzip_decompress(body: bytes) -> bytes | None:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as handle:
            return _read_decoded_limited(handle)
    except (EOFError, OSError, zlib.error):
        return None


def _bounded_zlib_decompress(body: bytes) -> bytes | None:
    decoder = zlib.decompressobj()
    decoded = bytearray()
    try:
        for offset in range(0, len(body), 64 * 1024):
            chunk = body[offset : offset + 64 * 1024]
            while chunk:
                remaining = MAX_DECODED_BODY_BYTES - len(decoded) + 1
                if remaining <= 0:
                    return None
                decoded.extend(decoder.decompress(chunk, remaining))
                if len(decoded) > MAX_DECODED_BODY_BYTES:
                    return None
                chunk = decoder.unconsumed_tail
        decoded.extend(decoder.flush(MAX_DECODED_BODY_BYTES - len(decoded) + 1))
    except zlib.error:
        return None
    if len(decoded) > MAX_DECODED_BODY_BYTES or not decoder.eof:
        return None
    return bytes(decoded)


def _read_decoded_limited(handle: Any) -> bytes | None:
    decoded = bytearray()
    while True:
        remaining = MAX_DECODED_BODY_BYTES - len(decoded) + 1
        if remaining <= 0:
            return None
        chunk = handle.read(min(64 * 1024, remaining))
        if not chunk:
            return bytes(decoded)
        decoded.extend(chunk)
        if len(decoded) > MAX_DECODED_BODY_BYTES:
            return None
