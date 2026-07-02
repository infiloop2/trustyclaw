"""Network policy access and request decisions.

The active policy lives in the ``network_policy`` database row. The admin
service (schema owner) replaces it after validation; the proxy role can only
read it. A missing row is the fail-closed empty default, and a database
outage denies every request (no fallback cache; see ``network_proxy``).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import posixpath
import re
import subprocess
import threading
import zlib
from typing import Any
from urllib.parse import unquote

from host.runtime.state import (
    network_policy_record,
    read_proxy_claude_account,
    read_proxy_openai_account_id,
    save_network_policy,
)

WEB_SEARCH_TOOL_TYPES = {"web_search", "web_search_preview"}
ANTHROPIC_PRE_PIN_BOOTSTRAP_GET_PATHS = {
    "/api/oauth/profile",
    "/api/oauth/claude_cli/roles",
    "/api/organization/claude_code_first_token_date",
    "/api/claude_code/policy_limits",
    "/api/claude_code/settings",
}
# zstd and brotli have no stdlib decoder, so those bodies go through the
# system binaries (installed by bootstrap), called with argument lists at
# absolute paths — the same posture as the rest of the host's shell-outs.
ZSTD_BIN = "/usr/bin/zstd"
BROTLI_BIN = "/usr/bin/brotli"
DECOMPRESS_TIMEOUT_SECONDS = 30
# Matches the proxy's MAX_BODY_BYTES: decompressed output is capped so a
# decompression bomb cannot exhaust the proxy's memory.
MAX_DECODED_BODY_BYTES = 128 * 1024 * 1024


def load_policy() -> dict[str, Any]:
    record = network_policy_record()
    if record is None:
        return {"managed_ai_provider_network_access": {}, "allowed_network_access": {}}
    controls = record["controls"]
    return controls if isinstance(controls, dict) else {}


def load_policy_updated_at() -> str | None:
    record = network_policy_record()
    return record["updated_at"] if record else None


def save_policy(policy: dict[str, Any], updated_at: str) -> None:
    save_network_policy(policy, updated_at)


def domain_matches(pattern: str, host: str) -> bool:
    pattern = pattern.lower()
    host = host.lower()
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and host != pattern[2:]
    return host == pattern


def find_domain_rule(policy: dict[str, Any], host: str) -> dict[str, Any] | None:
    """Return the rule for ``host``: an exact domain wins over wildcards, and
    the longest matching wildcard wins among wildcards."""
    rules = policy.get("allowed_network_access", {})
    exact = rules.get(host.lower())
    if exact is not None:
        return exact
    wildcards = [domain for domain in rules if domain.startswith("*.") and domain_matches(domain, host)]
    if not wildcards:
        return None
    return rules[max(wildcards, key=len)]


def host_allowed(policy: dict[str, Any], host: str) -> bool:
    """Whether any request to ``host`` could be allowed. Checked before the
    proxy resolves DNS or opens an upstream connection, so denied hosts are
    never even resolved."""
    rule = find_domain_rule(policy, host)
    return bool(rule and rule.get("allow_http_methods"))


def decide_http_request(
    policy: dict[str, Any],
    protocol: str,
    method: str,
    host: str,
    path: str,
    query: str,
) -> bool:
    if protocol not in {"http", "https", "ws", "wss"}:
        return False
    rule = find_domain_rule(policy, host)
    if rule is None:
        return False
    if method.upper() not in rule.get("allow_http_methods", []):
        return False
    path_guards = rule.get("path_guards") or []
    if not path_guards:
        return True
    # Match against the path the origin server will actually resolve: decode
    # percent-escapes and collapse ./ and ../ first, so a guard like
    # ^/v1/threads/.* cannot be bypassed with /v1/threads/../../admin (which the
    # upstream would serve as /admin).
    target = _normalized_path(path) + (f"?{query}" if query else "")
    return any(re.fullmatch(pattern, target) for pattern in path_guards)


def _normalized_path(path: str) -> str:
    decoded = unquote(path or "/")
    normalized = posixpath.normpath(decoded)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if path.endswith("/") and not normalized.endswith("/"):
        normalized += "/"
    return normalized


def openai_request_denied(
    policy: dict[str, Any],
    host: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> str | None:
    """Apply the OpenAI-specific domain controls. Returns a denial reason, or
    None when the request passes (including for non-OpenAI domains)."""
    rule = find_domain_rule(policy, host) or {}
    if rule.get("openai_account_guard"):
        account_id = read_proxy_openai_account_id()
        if not account_id:
            return "OpenAI account id is not available"
        # The header must be present AND match. A missing header is a denial:
        # otherwise the agent omits it and OpenAI resolves the account from the
        # token, defeating the pin.
        presented = [value for key, value in headers if key.lower() == "chatgpt-account-id"]
        if not presented:
            return "OpenAI account id header is required for this domain"
        if any(value != account_id for value in presented):
            return f"OpenAI account {presented[0]!r} is not the configured account"
    if rule.get("openai_disable_live_web_search"):
        return _live_web_search_denial(headers, body)
    return None


def anthropic_request_denied(
    policy: dict[str, Any],
    method: str,
    host: str,
    path: str,
    headers: list[tuple[str, str]],
) -> str | None:
    """Apply Claude/Anthropic account controls.

    Claude Code OAuth requests to api.anthropic.com use opaque bearer tokens and
    do not carry an OpenAI-style account header. The enforceable pin is
    therefore the bearer credential hash read from the agent user's Claude
    credentials after login. A tiny unauthenticated readiness path is allowed
    before the pin because Claude Code probes it during startup.
    """
    rule = find_domain_rule(policy, host) or {}
    if not rule.get("anthropic_account_guard"):
        return None
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
        if _anthropic_pre_pin_bootstrap_allowed(method, path, presented):
            return None
        return "Claude account token is not available"
    if not presented:
        return "Claude bearer token is required for this domain"
    if any(hashlib.sha256(value.encode()).hexdigest() != expected_hash for value in presented):
        return "Claude bearer token does not match the configured account"
    return None


def _anthropic_pre_pin_bootstrap_allowed(method: str, path: str, bearer_tokens: list[str]) -> bool:
    # Claude Code exchanges the browser OAuth code with platform.claude.com,
    # then calls a small set of api.anthropic.com profile/settings endpoints
    # before its credential file is ready for the host to hash and pin. Let
    # only those bearer-authenticated bootstrap reads through pre-pin; model
    # traffic such as /v1/messages still fails closed until the hash is stored.
    return (
        method.upper() == "GET"
        and _normalized_path(path) in ANTHROPIC_PRE_PIN_BOOTSTRAP_GET_PATHS
        and bool(bearer_tokens)
    )


def _bearer_token(value: str) -> str | None:
    scheme, _, credential = value.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()


def websocket_inspection_required(policy: dict[str, Any], host: str) -> bool:
    """Whether WebSocket messages to ``host`` must be inspected. Only the live
    web search guard depends on message bodies; the other controls (methods,
    paths, account pin) are fully decided at the handshake."""
    rule = find_domain_rule(policy, host) or {}
    return bool(rule.get("openai_disable_live_web_search"))


def openai_ws_message_denied(policy: dict[str, Any], host: str, payload: bytes) -> str | None:
    """Apply the live web search guard to one complete WebSocket message,
    mirroring the HTTP body check. WS payloads carry no Content-Encoding or
    Content-Type, so the message is inspected as-is."""
    if not websocket_inspection_required(policy, host):
        return None
    return _live_web_search_denial([], payload)


def _live_web_search_denial(headers: list[tuple[str, str]], body: bytes) -> str | None:
    """Block live web search while allowing the cached tool, matching the OpenAI
    request guard: the legacy ``web_search_preview`` tool is always denied, and a
    ``web_search`` tool is allowed only with ``external_web_access: false``
    (cached). A bare ``web_search`` marker with no parseable tool is denied as an
    anti-evasion measure."""
    if not body:
        return None
    header_map = {key.lower(): value for key, value in headers}
    decoded = _decode_body(body, header_map.get("content-encoding", ""))
    if decoded is None:
        return "request body could not be decoded for web search inspection"
    body = decoded
    # Anti-evasion first, before any content-type/shape gate: the legacy marker
    # is disallowed wherever it appears and whatever the declared content-type,
    # so the agent cannot skip inspection by mislabeling the body or prefixing a
    # non-JSON byte (OpenAI would still parse such a body).
    if b"web_search_preview" in body:
        return "web_search_preview is disabled for this domain"
    content_type = header_map.get("content-type", "").split(";", 1)[0].strip().lower()
    looks_json = content_type == "application/json" or body.lstrip().startswith((b"{", b"["))
    if not looks_json:
        # Genuinely non-JSON body; allow unless it still smuggles a web_search.
        if b"web_search" in body:
            return "web_search requested without a parseable tool"
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "request body is not valid JSON"
    tools = _iter_tool_objects(payload)
    if any(tool.get("type") == "web_search_preview" for tool in tools):
        return "web_search_preview is disabled for this domain"
    web_search_tools = [tool for tool in tools if tool.get("type") == "web_search"]
    if not web_search_tools:
        if b"web_search" in body:
            return "web_search requested without a parseable tool"
        return None
    for tool in web_search_tools:
        if tool.get("external_web_access") is not False:
            return "live web search is disabled for this domain (external_web_access must be false)"
    return None


def _iter_tool_objects(payload: Any) -> list[dict[str, Any]]:
    """Collect every tool object (``type`` in WEB_SEARCH_TOOL_TYPES) anywhere in
    the request, so a tool nested under any key is still inspected."""
    matches: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") in WEB_SEARCH_TOOL_TYPES:
                matches.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return matches


def _decode_body(body: bytes, content_encoding: str) -> bytes | None:
    """Decode a Content-Encoding so the guard inspects real JSON, not a
    compressed blob. Returns None when the body cannot be decoded (corrupt or an
    encoding we cannot read), which the caller treats as fail-closed."""
    encoding = content_encoding.strip().lower()
    if encoding in ("", "identity"):
        return body
    if encoding == "gzip":
        return _bounded_gzip_decompress(body)
    if encoding in ("deflate", "zlib"):
        return _bounded_zlib_decompress(body)
    if encoding == "zstd":
        return _run_decompressor([ZSTD_BIN, "-dc"], body)
    if encoding == "br":
        return _run_decompressor([BROTLI_BIN, "-dc"], body)
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


def _run_decompressor(argv: list[str], body: bytes) -> bytes | None:
    """Decompress through a system binary (stdin -> stdout) with the output
    size capped, so a decompression bomb cannot exhaust the proxy's memory.
    Returns None — fail closed — when the binary is missing, the stream is
    corrupt, or the output exceeds the cap."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    assert proc.stdin is not None and proc.stdout is not None
    stdin = proc.stdin

    def feed() -> None:
        # A separate writer avoids the classic pipe deadlock: the child can
        # block writing output while we would block writing its input.
        try:
            stdin.write(body)
        except OSError:
            pass  # the child died; wait() below surfaces it
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    # This read can only block while the child is alive and silent. The child
    # has its complete input (stdin is closed after the write), so a real
    # decompressor either produces output or exits with an error; the wait()
    # timeout below covers one that lingers after closing its stdout.
    decoded = bytearray()
    overflow = False
    while chunk := proc.stdout.read(64 * 1024):
        decoded.extend(chunk)
        if len(decoded) > MAX_DECODED_BODY_BYTES:
            overflow = True
            proc.kill()
            break
    proc.stdout.close()
    try:
        returncode = proc.wait(timeout=DECOMPRESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return None
    writer.join(timeout=5)
    if overflow or returncode != 0:
        return None
    return bytes(decoded)
