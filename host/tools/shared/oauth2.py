"""Provider-neutral OAuth 2.0 helpers for tool packages.

The Google tools carry their own credential store (`shared/google.py`); this
module holds the pieces the newer OAuth 2.0 tools (X, LinkedIn, Instagram)
share: the HMAC-signed `state` value (which can carry flow data such as a PKCE
verifier through the provider round trip), token freshness checks, and the
compare-before-write credential guards that keep a slow network call from
clobbering an operator disconnect/reconnect that happened meanwhile.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Mapping
from typing import cast

from host.tools.json_types import JSONObject
from host.tools.host_api import HostAPI, StoredCredential

OAUTH_STATE_MAX_AGE_SECONDS = 15 * 60
ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 60


class IntegrationReconnectRequired(RuntimeError):
    """The saved connection is missing, expired, revoked, or superseded; the
    operator must reconnect the tool. Mirrors shared/google.py's exception."""


def now() -> int:
    return int(time.time())


def base64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def base64url_decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(f"{encoded}{padding}".encode("ascii"))


def signed_state(*, secret: str, tool_id: str, extra: JSONObject | None = None) -> str:
    """An HMAC-signed, tamper-evident state value. `extra` rides along for
    values the tool needs back in complete_connect (e.g. the PKCE verifier);
    the payload is signed but readable, so nothing in it may be a lasting
    secret — a PKCE verifier is acceptable for confidential clients because
    the code exchange also requires the client secret."""
    payload: JSONObject = {
        **(extra or {}),
        "issued_at": now(),
        "nonce": uuid.uuid4().hex,
        "tool_id": tool_id,
    }
    encoded_payload = base64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded_payload}.{base64url_encode(signature)}"


def verify_state(state: str, *, secret: str, tool_id: str) -> JSONObject:
    """Verify a signed state and return its payload (including any extras)."""
    encoded_payload, separator, encoded_signature = state.partition(".")
    if not separator or not encoded_payload or not encoded_signature:
        raise ValueError("Invalid OAuth state.")
    expected_signature = base64url_encode(
        hmac.new(secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(expected_signature, encoded_signature):
        raise ValueError("Invalid OAuth state.")
    decoded = json.loads(base64url_decode(encoded_payload).decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Invalid OAuth state.")
    payload = cast(JSONObject, decoded)
    if payload.get("tool_id") != tool_id:
        raise ValueError("Invalid OAuth state.")
    issued_at = payload.get("issued_at")
    age = now() - issued_at if isinstance(issued_at, int) and not isinstance(issued_at, bool) else None
    if age is None or age < -60 or age > OAUTH_STATE_MAX_AGE_SECONDS:
        raise ValueError("OAuth state expired.")
    return payload


def pkce_verifier_and_challenge() -> tuple[str, str]:
    verifier = base64url_encode(uuid.uuid4().bytes + uuid.uuid4().bytes)
    challenge = base64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def access_token_is_fresh(
    token_payload: Mapping[str, object],
    current_time: int,
    *,
    skew_seconds: int = ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
) -> bool:
    access_token = token_payload.get("access_token")
    expires_at = token_payload.get("expires_at")
    return (
        isinstance(access_token, str)
        and bool(access_token)
        and isinstance(expires_at, int)
        and expires_at > current_time + skew_seconds
    )


def save_if_still_connected(
    api: HostAPI, loaded: StoredCredential, credential: StoredCredential, *, reconnect_message: str
) -> None:
    """Persist a refreshed secret only if the stored connection is still
    exactly the one this call loaded (same account id and same secret). An
    operator disconnect or reconnect during the network round trip must win:
    fail closed and let a retry run against the current credential. This
    matters doubly for providers with single-use rotating refresh tokens,
    where a stale save would store an already-spent token."""
    current = api.credentials.load()
    if (
        current is None
        or current["account"]["id"] != loaded["account"]["id"]
        or current["secret"] != loaded["secret"]
    ):
        raise IntegrationReconnectRequired(reconnect_message)
    api.credentials.save(credential)


def clear_if_still_loaded(api: HostAPI, loaded: StoredCredential) -> None:
    """Clear only the credential this call actually inspected, so a stale
    failure (e.g. an invalid_grant from a token the operator already rotated
    by reconnecting) cannot delete a fresh connection."""
    current = api.credentials.load()
    if (
        current is not None
        and current["account"]["id"] == loaded["account"]["id"]
        and current["secret"] == loaded["secret"]
    ):
        api.credentials.clear()
