from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from typing import cast

from host.tools.json_types import JSONObject, JSONValue
from host.tools.host_api import ConnectionAccount, HostAPI, StoredCredential
from host.tools.tool import (
    ConnectionStatus,
    OAuthCompleteConnectParams,
    OAuthCompleteConnectResult,
    OAuthStartConnectParams,
    OAuthStartConnectResult,
)

GOOGLE_OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_OAUTH_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 60
GOOGLE_DEFAULT_EXPIRES_IN_SECONDS = 3600
GOOGLE_OAUTH_STATE_MAX_AGE_SECONDS = 15 * 60
GOOGLE_UNAUTHORIZED_RECONNECT_MESSAGE = (
    "Google rejected the stored credentials. Please reconnect this tool from the admin UI."
)


class GoogleOAuthInvalidGrantError(RuntimeError):
    pass


class IntegrationReconnectRequired(RuntimeError):
    pass


def now() -> int:
    return int(time.time())


def _base64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _base64url_decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(f"{encoded}{padding}".encode("ascii"))


def build_google_oauth_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
    state: str,
    force_consent: bool,
) -> str:
    query_params = {
        "access_type": "offline",
        "client_id": client_id,
        "include_granted_scopes": "true",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    if force_consent:
        query_params["prompt"] = "consent"
    return f"{GOOGLE_OAUTH_AUTH_URL}?{urllib.parse.urlencode(query_params)}"


def is_google_invalid_grant_payload(payload: bytes) -> bool:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(decoded, dict) and decoded.get("error") == "invalid_grant"


def normalize_email(value: str) -> str:
    return value.strip().lower()


def google_identity_from_userinfo(userinfo: Mapping[str, object]) -> JSONObject:
    sub = userinfo.get("sub")
    if not isinstance(sub, str) or not sub:
        raise RuntimeError("Google did not return a stable account id.")
    email_value = userinfo.get("email")
    if not isinstance(email_value, str) or not email_value.strip():
        raise RuntimeError("Google did not return an email address.")
    if userinfo.get("email_verified") is not True:
        raise RuntimeError("Google email address is not verified.")
    return {
        "email": normalize_email(email_value),
        "email_verified": True,
        "sub": sub,
    }


def granted_google_scopes(token_response: Mapping[str, object]) -> set[str]:
    scope = token_response.get("scope")
    return set(scope.split()) if isinstance(scope, str) else set()


def google_refresh_token_from_payload(token_payload: object) -> str:
    if not isinstance(token_payload, Mapping):
        return ""
    refresh_token = token_payload.get("refresh_token")
    return refresh_token if isinstance(refresh_token, str) else ""


def google_token_for_revoke_from_payload(token_payload: object) -> str:
    if not isinstance(token_payload, Mapping):
        return ""
    refresh_token = token_payload.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token:
        return refresh_token
    access_token = token_payload.get("access_token")
    return access_token if isinstance(access_token, str) else ""


def google_access_token_from_payload(token_payload: Mapping[str, object]) -> str:
    access_token = token_payload.get("access_token")
    return access_token if isinstance(access_token, str) and access_token else ""


def google_access_token_is_fresh(
    token_payload: Mapping[str, object],
    current_time: int,
    *,
    skew_seconds: int = GOOGLE_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
) -> bool:
    access_token = token_payload.get("access_token")
    expires_at = token_payload.get("expires_at")
    return (
        isinstance(access_token, str)
        and bool(access_token)
        and isinstance(expires_at, int)
        and expires_at > current_time + skew_seconds
    )


def google_token_payload_from_response(
    token_response: Mapping[str, object],
    *,
    fallback_refresh_token: str,
    current_time: int,
) -> JSONObject:
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Google OAuth token response returned no access token.")
    refresh_token = token_response.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = fallback_refresh_token
    if not refresh_token:
        raise RuntimeError("Google OAuth token response returned no refresh token.")
    expires_in = token_response.get("expires_in")
    scope = token_response.get("scope")
    token_type = token_response.get("token_type")
    return {
        "access_token": access_token,
        "expires_at": current_time + (expires_in if isinstance(expires_in, int) else GOOGLE_DEFAULT_EXPIRES_IN_SECONDS),
        "refresh_token": refresh_token,
        "scope": scope if isinstance(scope, str) else "",
        "token_type": token_type if isinstance(token_type, str) else "Bearer",
    }


def google_refreshed_token_payload(
    token_payload: Mapping[str, object],
    token_response: Mapping[str, object],
    *,
    current_time: int,
) -> JSONObject:
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Google OAuth token refresh returned no access token.")
    expires_in = token_response.get("expires_in")
    refreshed_payload: JSONObject = {
        key: value
        for key, value in token_payload.items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }
    refreshed_payload["access_token"] = access_token
    refreshed_payload["expires_at"] = current_time + (
        expires_in if isinstance(expires_in, int) else GOOGLE_DEFAULT_EXPIRES_IN_SECONDS
    )
    return refreshed_payload


def _post_google_oauth_form(
    form: Mapping[str, str],
    *,
    failure_message: str,
    invalid_response_message: str,
    invalid_grant_is_special: bool,
) -> dict[str, object]:
    body = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        GOOGLE_OAUTH_TOKEN_URL,
        data=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if invalid_grant_is_special and is_google_invalid_grant_payload(exc.read()):
            raise GoogleOAuthInvalidGrantError("Google OAuth refresh token is invalid.") from exc
        raise RuntimeError(failure_message) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(failure_message) from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(invalid_response_message)
    return cast(dict[str, object], decoded)


def exchange_google_oauth_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    failure_message: str,
    invalid_response_message: str,
) -> dict[str, object]:
    return _post_google_oauth_form(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        failure_message=failure_message,
        invalid_response_message=invalid_response_message,
        invalid_grant_is_special=False,
    )


def refresh_google_oauth_token(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    failure_message: str,
    invalid_response_message: str,
) -> dict[str, object]:
    return _post_google_oauth_form(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        failure_message=failure_message,
        invalid_response_message=invalid_response_message,
        invalid_grant_is_special=True,
    )


def get_google_userinfo(
    access_token: str,
    *,
    failure_message: str,
    invalid_response_message: str,
) -> dict[str, object]:
    request = urllib.request.Request(
        GOOGLE_USERINFO_URL,
        headers={"authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            # Same treatment as google_json_request: a rejected cached token
            # must surface the reconnect flow, and refresh_identity runs
            # before every write proposal and approved execution.
            raise IntegrationReconnectRequired(GOOGLE_UNAUTHORIZED_RECONNECT_MESSAGE) from exc
        raise RuntimeError(failure_message) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(failure_message) from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(invalid_response_message)
    return cast(dict[str, object], decoded)


def revoke_google_token(token: str) -> JSONObject:
    body = urllib.parse.urlencode({"token": token}).encode("utf-8")
    request = urllib.request.Request(
        GOOGLE_OAUTH_REVOKE_URL,
        data=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30):
            pass
    except urllib.error.HTTPError as exc:
        return {"success": False, "failure_type": "http", "status": exc.code}
    except urllib.error.URLError as exc:
        return {"success": False, "failure_type": "network", "error_type": type(exc).__name__}
    return {"success": True}


def clip_text(value: str, max_bytes: int) -> str:
    """Clip one field to a UTF-8 byte budget for approval summaries. Clipping
    per field keeps every disclosure present when the whole summary must fit
    the host API's 500-byte limit."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[: max_bytes - 3].decode("utf-8", errors="ignore") + "…"


class GoogleCredentialStore:
    def __init__(
        self,
        *,
        tool_id: str,
        scopes: tuple[str, ...],
        required_scopes: frozenset[str],
        reconnect_message: str,
    ) -> None:
        self.tool_id = tool_id
        self.scopes = scopes
        self.required_scopes = required_scopes
        self.reconnect_message = reconnect_message

    def start_connect(self, params: OAuthStartConnectParams, api: HostAPI) -> OAuthStartConnectResult:
        redirect_uri = params["redirect_uri"]
        state = self._signed_state(api)
        return {
            "authorization_url": build_google_oauth_authorization_url(
                client_id=api.config["GOOGLE_OAUTH_CLIENT_ID"],
                redirect_uri=redirect_uri,
                scopes=self.scopes,
                state=state,
                force_consent=True,
            ),
            "state": state,
        }

    def complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult:
        code = params["code"]
        redirect_uri = params["redirect_uri"]
        self._verify_state(params["state"], api)
        token_response = exchange_google_oauth_code(
            client_id=api.config["GOOGLE_OAUTH_CLIENT_ID"],
            client_secret=api.config["GOOGLE_OAUTH_CLIENT_SECRET"],
            code=code,
            redirect_uri=redirect_uri,
            failure_message="Google OAuth token exchange failed.",
            invalid_response_message="Google OAuth token exchange returned an invalid response.",
        )
        access_token = token_response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("Google OAuth token exchange returned no access token.")
        missing_scopes = self.required_scopes - granted_google_scopes(token_response)
        if missing_scopes:
            # The insufficient new grant was never saved, so there is nothing
            # to clean up — and an already-connected account must survive a
            # failed reconnect attempt, so nothing is cleared here.
            raise RuntimeError("Google connection is missing required permissions.")
        identity = google_identity_from_userinfo(
            get_google_userinfo(
                access_token,
                failure_message="Google OAuth user profile lookup failed.",
                invalid_response_message="Google OAuth user profile lookup returned an invalid response.",
            )
        )
        existing = api.credentials.load()
        fallback_refresh_token = ""
        if existing is not None and existing["account"]["id"] == identity["sub"]:
            fallback_refresh_token = google_refresh_token_from_payload(existing["secret"])
        token_payload = google_token_payload_from_response(
            token_response,
            fallback_refresh_token=fallback_refresh_token,
            current_time=now(),
        )
        scope_field = token_payload.get("scope")
        scope_str = scope_field if isinstance(scope_field, str) and scope_field else " ".join(self.scopes)
        scopes = scope_str.split()
        created_at = existing["metadata"].get("created_at") if existing is not None else None
        current_time = now()
        account: ConnectionAccount = {"id": str(identity["sub"]), "label": str(identity["email"]), "scopes": scopes}
        credential: StoredCredential = {
            "account": account,
            "secret": token_payload,
            "metadata": {
                "created_at": created_at if isinstance(created_at, int) else current_time,
                "email_verified": identity["email_verified"],
                "identity_checked_at": current_time,
                "updated_at": current_time,
            },
        }
        api.credentials.save(credential)
        return {"account": account}

    def disconnect(self, api: HostAPI) -> None:
        existing = api.credentials.load()
        if existing is not None:
            token = google_token_for_revoke_from_payload(existing["secret"])
            if token:
                revoke_google_token(token)
        api.credentials.clear()

    # Saves and clears after a network round trip are deliberately unguarded:
    # an operator disconnect/reconnect landing in that multi-second window can
    # clobber the fresh credential or drop a stale one, and the recovery is
    # simply reconnecting once more (disconnect also revokes the token at
    # Google, so a clobbered stale token is dead regardless of what is stored).

    def connection_status(self, api: HostAPI) -> ConnectionStatus:
        existing = api.credentials.load()
        if existing is None:
            return {"connected": False}
        return {"connected": True, "account": existing["account"]}

    def access_token(self, api: HostAPI) -> str:
        existing = api.credentials.load()
        if existing is None:
            raise IntegrationReconnectRequired(f"{self.tool_id} is not connected.")
        token_payload = existing["secret"]
        missing_scopes = self.required_scopes - set(existing["account"]["scopes"])
        if missing_scopes:
            api.credentials.clear()
            raise IntegrationReconnectRequired(self.reconnect_message)
        payload = cast(Mapping[str, object], token_payload)
        if google_access_token_is_fresh(payload, now()):
            return google_access_token_from_payload(payload)
        refresh_token = google_refresh_token_from_payload(payload)
        if not refresh_token:
            api.credentials.clear()
            raise IntegrationReconnectRequired(self.reconnect_message)
        try:
            token_response = refresh_google_oauth_token(
                client_id=api.config["GOOGLE_OAUTH_CLIENT_ID"],
                client_secret=api.config["GOOGLE_OAUTH_CLIENT_SECRET"],
                refresh_token=refresh_token,
                failure_message="Google OAuth token refresh failed.",
                invalid_response_message="Google OAuth token refresh returned an invalid response.",
            )
        except GoogleOAuthInvalidGrantError as exc:
            api.credentials.clear()
            raise IntegrationReconnectRequired(self.reconnect_message) from exc
        updated_payload = google_refreshed_token_payload(payload, token_response, current_time=now())
        api.credentials.save({
            "account": existing["account"],
            "secret": updated_payload,
            "metadata": {**existing["metadata"], "updated_at": now()},
        })
        return google_access_token_from_payload(updated_payload)

    def refresh_identity(self, api: HostAPI, access_token: str) -> ConnectionAccount:
        existing = api.credentials.load()
        if existing is None:
            raise IntegrationReconnectRequired(self.reconnect_message)
        identity = google_identity_from_userinfo(
            get_google_userinfo(
                access_token,
                failure_message="Google OAuth user profile lookup failed.",
                invalid_response_message="Google OAuth user profile lookup returned an invalid response.",
            )
        )
        if existing["account"]["id"] != identity["sub"]:
            api.credentials.clear()
            raise IntegrationReconnectRequired(self.reconnect_message)
        account: ConnectionAccount = {
            "id": str(identity["sub"]),
            "label": str(identity["email"]),
            "scopes": existing["account"]["scopes"],
        }
        api.credentials.save({
            "account": account,
            "secret": existing["secret"],
            "metadata": {
                **existing["metadata"],
                "email_verified": identity["email_verified"],
                "identity_checked_at": now(),
                "updated_at": now(),
            },
        })
        return account

    def _signed_state(self, api: HostAPI) -> str:
        payload: JSONObject = {
            "issued_at": now(),
            "nonce": uuid.uuid4().hex,
            "tool_id": self.tool_id,
        }
        encoded_payload = _base64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = hmac.new(
            api.config["GOOGLE_OAUTH_CLIENT_SECRET"].encode("utf-8"),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{encoded_payload}.{_base64url_encode(signature)}"

    def _verify_state(self, state: str, api: HostAPI) -> None:
        encoded_payload, separator, encoded_signature = state.partition(".")
        if not separator or not encoded_payload or not encoded_signature:
            raise ValueError("Invalid Google OAuth state.")
        expected_signature = _base64url_encode(
            hmac.new(
                api.config["GOOGLE_OAUTH_CLIENT_SECRET"].encode("utf-8"),
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(expected_signature, encoded_signature):
            raise ValueError("Invalid Google OAuth state.")
        decoded = json.loads(_base64url_decode(encoded_payload).decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("Invalid Google OAuth state.")
        payload = cast(dict[str, object], decoded)
        if payload.get("tool_id") != self.tool_id:
            raise ValueError("Invalid Google OAuth state.")
        issued_at = payload.get("issued_at")
        if not isinstance(issued_at, int) or now() - issued_at > GOOGLE_OAUTH_STATE_MAX_AGE_SECONDS:
            raise ValueError("Google OAuth state expired.")


def google_json_request(
    method: str,
    url: str,
    access_token: str,
    *,
    body: JSONObject | None = None,
    failure_message: str,
    invalid_response_message: str,
) -> JSONObject:
    data = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "authorization": f"Bearer {access_token}",
            **({"content-type": "application/json"} if body is not None else {}),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw_body = response.read().decode("utf-8")
            decoded = json.loads(raw_body) if raw_body.strip() else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            # A still-cached token Google no longer accepts (for example the
            # operator revoked the app) is a connection problem, not a
            # generic API failure: surface the reconnect-required flow.
            raise IntegrationReconnectRequired(GOOGLE_UNAUTHORIZED_RECONNECT_MESSAGE) from exc
        raise RuntimeError(failure_message) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(failure_message) from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(invalid_response_message)
    return cast(JSONObject, decoded)
