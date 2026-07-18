"""X (Twitter) tool package."""

from __future__ import annotations

import base64
import re
import urllib.parse
from collections.abc import Mapping
from typing import cast

from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink, DataSummaryPoint, SetupStep, ToolManifest
from host.tools.results import (
    ActionExecuted,
    ActionFailed,
    ActionPendingApproval,
    ActionResult,
    ApprovalExecuted,
    ApprovalResult,
)
from host.tools.tool import (
    ConnectionStatus,
    CredentialFlow,
    OAuthCompleteConnectParams,
    OAuthCompleteConnectResult,
    OAuthStartConnectParams,
    OAuthStartConnectResult,
)
from host.tools.host_api import ApprovalRecord, ConnectionAccount, HostAPI, StoredCredential
from host.tools.shared.google import clip_text
from host.tools.shared.oauth2 import (
    IntegrationReconnectRequired,
    access_token_is_fresh,
    clear_if_still_loaded,
    now,
    pkce_verifier_and_challenge,
    save_if_still_connected,
    signed_state,
    verify_state,
)
from host.tools.shared.web import WebRequestError, encode_query, json_request

X_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
X_REVOKE_URL = "https://api.x.com/2/oauth2/revoke"
X_API_BASE_URL = "https://api.x.com/2"
X_OAUTH_SCOPES = ("tweet.read", "users.read", "tweet.write", "offline.access")
# offline.access is required at connect time: without it X issues no refresh
# token, and the 2-hour access token would strand the connection.
REQUIRED_X_SCOPES = frozenset({"tweet.read", "users.read", "tweet.write", "offline.access"})
X_RECONNECT_MESSAGE = "X (Twitter) is no longer connected. Please reconnect the tool."
DEFAULT_TOKEN_LIFETIME_SECONDS = 7200
MAX_QUERY_CHARS = 512
MAX_TWEET_CHARS = 4_000
TWEET_ID_RE = re.compile(r"^[0-9]{1,25}$")
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{1,15}$")
WOEID_RE = re.compile(r"^[0-9]{1,10}$")
WORLDWIDE_WOEID = "1"
TWEET_FIELDS = "created_at,author_id,public_metrics,conversation_id"
# Host approval summaries are capped at 500 UTF-8 bytes (tools_host SUMMARY_MAX_BYTES).
SUMMARY_MAX_BYTES = 500
X_READ_POLICY = (
    "Read-only. Sends the listed query values to the X API authenticated as the "
    "connected account and returns public post data. Each call is billed to the "
    "deployment's X API pay-per-use credits. The result enters active model context. "
    "Runs directly with no approval."
)
X_PERSONALIZED_TRENDS_POLICY = (
    "Read-only. Sends an authenticated request for the connected account's personalized "
    "trend topics (not public post data) and returns them to active model context. Each "
    "call is billed to the deployment's X API pay-per-use credits and requires X Premium. "
    "Runs directly with no approval."
)


class ToolInputValidationError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _schema(properties: JSONObject, required: list[str] | None = None) -> JSONObject:
    schema: JSONObject = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = cast(list[JSONValue], required)
    return schema


X_OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}},
    "additionalProperties": True,
}


MANIFEST = ToolManifest(
    tool_id="twitter",
    display_name="X (Twitter)",
    description="Connect your X account and let your agent search and read X posts, and publish posts, replies, and quotes with your approval.",
    connection="oauth",
    actions=(
        ActionSpec(id="search_tweets",
            description="Search public X posts from the last seven days with X query syntax and return post text, author, timestamp, and metrics. Use get_trends to discover trend names first; reads are billed per post returned.",
            data_policy=X_READ_POLICY,
            input_schema=_schema(
                {
                    "query": {"type": "string", "description": "X search query (up to 512 chars)."},
                    "max_results": {"type": "string", "description": "10-100 (default 10). Reads are billed per post returned."},
                },
                ["query"],
            ),
            output_schema=X_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="read_tweet",
            description="Read one public X post by numeric post id and return its text, author, timestamp, and metrics. This does not read a thread or timeline.",
            data_policy=X_READ_POLICY,
            input_schema=_schema({"tweet_id": {"type": "string", "description": "Numeric X post id from a URL or another X action result."}}, ["tweet_id"]),
            output_schema=X_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="user_tweets",
            description="Read one public user's recent posts and return text, timestamps, and metrics. Provide exactly one username or numeric user_id; this is that user's timeline, not the connected account's home feed.",
            data_policy=X_READ_POLICY,
            input_schema=_schema(
                {
                    "username": {"type": "string", "description": "X handle with or without @; mutually exclusive with user_id."},
                    "user_id": {"type": "string", "description": "Permanent numeric X user id; mutually exclusive with username."},
                    "max_results": {"type": "string", "description": "5-100 (default 10)."},
                }
            ),
            output_schema=X_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="get_trends",
            description="Read public trending topic names and optional post counts for one geographic WOEID, worldwide by default. This returns topics, not posts; follow with search_tweets to find and rank posts about a trend.",
            data_policy=(
                "Read-only. Sends only the requested location id to the X API using the "
                "deployment's app Bearer token and returns public trending topic names "
                "and post counts. Each call is billed to the deployment's X API "
                "pay-per-use credits. The result enters active model context. Runs directly with no approval."
            ),
            input_schema=_schema(
                {
                    "woeid": {"type": "string", "description": "Where-On-Earth id of the place (default 1 = worldwide; e.g. 23424977 = United States, 23424848 = India)."},
                    "max_trends": {"type": "string", "description": "1-50 (default 20)."},
                }
            ),
            output_schema=X_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="get_personalized_trends",
            description="Read the connected account's personalized For You trend names, categories, counts, and start times. Requires X Premium and returns topics, not posts; follow with search_tweets for matching public posts.",
            data_policy=X_PERSONALIZED_TRENDS_POLICY,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema=X_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="post_tweet",
            description="Queue approval to publish exactly one standalone post, reply, or quote post from the connected account. Set neither target id for standalone, or exactly one reply/quote id; the target post is fetched and shown in the approval.",
            data_policy=(
                "Publishes a post from the connected X account, visible per the account's "
                "audience settings. Queued for explicit approval; nothing reaches X until "
                "you approve. Posting is billed to the deployment's X API credits. The "
                "proposal and sanitized created-post id are available to the active model."
            ),
            input_schema=_schema(
                {
                    "text": {"type": "string", "description": "Post text."},
                    "in_reply_to_tweet_id": {"type": "string", "description": "Reply to this post id."},
                    "quote_tweet_id": {"type": "string", "description": "Quote this post id."},
                },
                ["text"],
            ),
            output_schema=X_OUTPUT_SCHEMA,
            approval="operator",
        ),
    ),
    config=(
        ConfigRequirement(key="X_OAUTH_CLIENT_ID", description="X developer app OAuth 2.0 client id."),
        ConfigRequirement(key="X_OAUTH_CLIENT_SECRET", description="X developer app OAuth 2.0 client secret (confidential client)."),
        ConfigRequirement(key="X_BEARER_TOKEN", description="X developer app Bearer Token (app-only auth; used by trends lookups, which do not accept user-context tokens)."),
    ),
    protections=(
        "Reading does not require approval. Publishing a post, reply, or quote post happens only after your approval.",
        "Public reads and writes consume X pay-per-use credits. Your X credentials stay encrypted in write-only tool config.",
    ),
    setup_steps=(
        SetupStep(
            title="Create an X developer project and app",
            description="Sign in to the X Developer Console, create or select the project, and create the app that will own this integration. Enable the current pay-per-use API access and add enough credits for recent search, lookups, trends, and intended writes. Keep this app dedicated enough that its billing and credentials can be revoked without affecting unrelated systems.",
            link_url="https://developer.x.com/",
            link_label="Open the X Developer Portal",
        ),
        SetupStep(
            title="Configure user authentication",
            show_callback=True,
            description="Open the app's User authentication settings and choose Set up or Edit. Enable OAuth 2.0, set App permissions to Read and write, and choose Web App, Automated App or Bot so X issues a confidential-client secret. Add the exact callback URI displayed in this guide. If X requires a Website URL, use your public TrustyClaw base URL: for a callback such as https://host.example/oauth/callback, use https://host.example. You do not need a separate website. Then save. TrustyClaw requests exactly tweet.read, users.read, tweet.write, and offline.access; offline.access is what lets X issue refresh tokens after the two-hour access token expires.",
            link_url="https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code",
            link_label="View X OAuth 2.0 authorization-code documentation",
        ),
        SetupStep(
            title="Copy all three app values",
            description="Open Keys and tokens for the same app. Copy the OAuth 2.0 Client ID and Client Secret, then copy or regenerate the app-only Bearer Token under Authentication Tokens. Regenerating any value invalidates the old one, so update TrustyClaw immediately. The Bearer Token is required for public trend endpoints that do not accept the connected user's token; do not substitute an OAuth 1.0a access-token pair.",
        ),
        SetupStep(
            title="Configure and connect TrustyClaw",
            show_config=True,
            description="Expand X in Internet Access and Tools. Save the OAuth 2.0 values as X_OAUTH_CLIENT_ID and X_OAUTH_CLIENT_SECRET and the app-only token as X_BEARER_TOKEN. Enable the tool, choose Connect, sign in as the account the agent may read and publish from, and approve the four displayed scopes. Confirm the row shows the expected @username.",
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                points=(
                    DataSummaryPoint(label="Reads", text="Search queries, post ids, usernames, trend locations, and paging values go to X directly. Query text is received and logged like any other request, so it is itself data sent to X."),
                    DataSummaryPoint(label="Posts, replies, and quote posts", text="A post, reply, or quote post reaches X only after your approval. It sends exactly the approved text and, for a reply or quote post, the target post id."),
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(label="X", text="Reads and the OAuth connection stay within X's services under the connected account."),
                    DataSummaryPoint(label="The public internet", text="An approved post is published on X under your account's audience settings; public posts are broadly visible and reusable under X's terms."),
                ),
            ),
            DataSummaryCard(
                title="What X can do with it",
                description=(
                    "X processes searches, retrieved content, API activity, and request metadata under its Privacy Policy and "
                    "developer terms: service operation, personalization, analytics, advertising, safety, and legal uses."
                ),
                links=(
                    DataSummaryLink(label="X Privacy Policy", url="https://x.com/en/privacy"),
                    DataSummaryLink(label="X Developer Agreement and Policy", url="https://developer.x.com/en/developer-terms/agreement-and-policy"),
                ),
            ),
            DataSummaryCard(
                title="How long X retains it",
                description=(
                    "A published post stays on X until you or X remove it. X keeps account, security, and API records under its "
                    "Privacy Policy with no single fixed period. Disconnect revokes the token where possible and always clears "
                    "the local credential, but does not delete X's own records."
                ),
                links=(
                    DataSummaryLink(label="X Privacy Policy", url="https://x.com/en/privacy"),
                ),
            ),
        ),
    ),
)


def _basic_auth_header(api: HostAPI) -> str:
    raw = f"{api.config['X_OAUTH_CLIENT_ID']}:{api.config['X_OAUTH_CLIENT_SECRET']}".encode("utf-8")
    return f"Basic {base64.b64encode(raw).decode('ascii')}"


def _token_payload_from_response(token_response: Mapping[str, object], *, fallback_refresh_token: str) -> JSONObject:
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("X OAuth token response returned no access token.")
    refresh_token = token_response.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = fallback_refresh_token
    expires_in = token_response.get("expires_in")
    scope = token_response.get("scope")
    return {
        "access_token": access_token,
        "expires_at": now() + (expires_in if isinstance(expires_in, int) else DEFAULT_TOKEN_LIFETIME_SECONDS),
        "refresh_token": refresh_token,
        "scope": scope if isinstance(scope, str) else "",
        "token_type": "bearer",
    }


def _is_invalid_grant(body: bytes) -> bool:
    return b"invalid_grant" in body or b"invalid_request" in body


def _fetch_me(access_token: str) -> ConnectionAccount:
    response = json_request(
        "GET",
        f"{X_API_BASE_URL}/users/me?{encode_query({'user.fields': 'id,name,username'})}",
        headers={"authorization": f"Bearer {access_token}"},
        failure_message="X profile lookup failed.",
        invalid_response_message="X profile lookup returned an invalid response.",
    )
    data = response.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("X profile lookup returned an invalid response.")
    user_id = data.get("id")
    username = data.get("username")
    if not isinstance(user_id, str) or not TWEET_ID_RE.fullmatch(user_id):
        raise RuntimeError("X did not return a stable account id.")
    label = f"@{username}" if isinstance(username, str) and username else user_id
    return {"id": user_id, "label": label, "scopes": []}


class XCredentialStore:
    """OAuth 2.0 authorization-code + PKCE against X, with rotating single-use
    refresh tokens. The PKCE verifier rides in the HMAC-signed state (see
    shared/oauth2.signed_state for why that is acceptable for a confidential
    client)."""

    def start_connect(self, params: OAuthStartConnectParams, api: HostAPI) -> OAuthStartConnectResult:
        verifier, challenge = pkce_verifier_and_challenge()
        state = signed_state(
            secret=api.config["X_OAUTH_CLIENT_SECRET"], tool_id=MANIFEST.tool_id, extra={"verifier": verifier}
        )
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": api.config["X_OAUTH_CLIENT_ID"],
                "redirect_uri": params["redirect_uri"],
                "scope": " ".join(X_OAUTH_SCOPES),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return {"authorization_url": f"{X_AUTHORIZE_URL}?{query}", "state": state}

    def complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult:
        state_payload = verify_state(
            params["state"], secret=api.config["X_OAUTH_CLIENT_SECRET"], tool_id=MANIFEST.tool_id
        )
        verifier = state_payload.get("verifier")
        if not isinstance(verifier, str) or not verifier:
            raise ValueError("Invalid OAuth state.")
        token_response = json_request(
            "POST",
            X_TOKEN_URL,
            headers={"authorization": _basic_auth_header(api)},
            form={
                "grant_type": "authorization_code",
                "code": params["code"],
                "redirect_uri": params["redirect_uri"],
                "code_verifier": verifier,
                "client_id": api.config["X_OAUTH_CLIENT_ID"],
            },
            failure_message="X OAuth token exchange failed.",
            invalid_response_message="X OAuth token exchange returned an invalid response.",
        )
        granted_scopes = str(token_response.get("scope") or "").split()
        missing = REQUIRED_X_SCOPES - set(granted_scopes)
        if missing:
            raise RuntimeError("X connection is missing required permissions.")
        existing = api.credentials.load()
        token_payload = _token_payload_from_response(token_response, fallback_refresh_token="")
        if not token_payload["refresh_token"]:
            raise RuntimeError("X connection returned no refresh token. Reconnect and grant offline access.")
        access_token = str(token_payload["access_token"])
        identity = _fetch_me(access_token)
        account: ConnectionAccount = {"id": identity["id"], "label": identity["label"], "scopes": granted_scopes}
        created_at = existing["metadata"].get("created_at") if existing is not None else None
        current_time = now()
        api.credentials.save(
            {
                "account": account,
                "secret": token_payload,
                "metadata": {
                    "created_at": created_at if isinstance(created_at, int) else current_time,
                    "updated_at": current_time,
                },
            }
        )
        return {"account": account}

    def disconnect(self, api: HostAPI) -> None:
        existing = api.credentials.load()
        if existing is not None:
            secret = existing["secret"]
            token = secret.get("refresh_token") or secret.get("access_token")
            if isinstance(token, str) and token:
                try:
                    json_request(
                        "POST",
                        X_REVOKE_URL,
                        headers={"authorization": _basic_auth_header(api)},
                        form={"token": token},
                        failure_message="X token revoke failed.",
                        invalid_response_message="X token revoke returned an invalid response.",
                    )
                except Exception:
                    pass  # Best-effort revoke; the credential is cleared regardless.
        api.credentials.clear()

    def connection_status(self, api: HostAPI) -> ConnectionStatus:
        existing = api.credentials.load()
        if existing is None:
            return {"connected": False}
        return {"connected": True, "account": existing["account"]}

    def access_token(self, api: HostAPI) -> str:
        existing = api.credentials.load()
        if existing is None:
            raise IntegrationReconnectRequired(X_RECONNECT_MESSAGE)
        payload = cast(Mapping[str, object], existing["secret"])
        if access_token_is_fresh(payload, now()):
            return str(payload.get("access_token") or "")
        refresh_token = payload.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            clear_if_still_loaded(api, existing)
            raise IntegrationReconnectRequired(X_RECONNECT_MESSAGE)
        try:
            token_response = json_request(
                "POST",
                X_TOKEN_URL,
                headers={"authorization": _basic_auth_header(api)},
                form={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": api.config["X_OAUTH_CLIENT_ID"],
                },
                failure_message="X OAuth token refresh failed.",
                invalid_response_message="X OAuth token refresh returned an invalid response.",
            )
        except WebRequestError as exc:
            if exc.status == 400 and _is_invalid_grant(exc.body):
                clear_if_still_loaded(api, existing)
                raise IntegrationReconnectRequired(X_RECONNECT_MESSAGE) from exc
            raise
        # X rotates refresh tokens: the one just used is spent, so the new
        # payload (with the newly issued refresh token) must win or the
        # connection is lost. Keep the old one only if none was returned.
        updated_payload = _token_payload_from_response(token_response, fallback_refresh_token=refresh_token)
        save_if_still_connected(
            api,
            existing,
            {
                "account": existing["account"],
                "secret": updated_payload,
                "metadata": {**existing["metadata"], "updated_at": now()},
            },
            reconnect_message=X_RECONNECT_MESSAGE,
        )
        return str(updated_payload["access_token"])

    def refresh_identity(self, api: HostAPI, access_token: str) -> ConnectionAccount:
        existing = api.credentials.load()
        if existing is None:
            raise IntegrationReconnectRequired(X_RECONNECT_MESSAGE)
        identity = _fetch_me(access_token)
        if existing["account"]["id"] != identity["id"]:
            clear_if_still_loaded(api, existing)
            raise IntegrationReconnectRequired(X_RECONNECT_MESSAGE)
        return {"id": identity["id"], "label": identity["label"], "scopes": existing["account"]["scopes"]}


X_CREDENTIALS = XCredentialStore()


class XCredentialFlow(CredentialFlow):
    def start_connect(self, params: OAuthStartConnectParams, api: HostAPI) -> OAuthStartConnectResult:
        return X_CREDENTIALS.start_connect(params, api)

    def complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult:
        return X_CREDENTIALS.complete_connect(params, api)

    def disconnect(self, api: HostAPI) -> None:
        X_CREDENTIALS.disconnect(api)

    def connection_status(self, api: HostAPI) -> ConnectionStatus:
        return X_CREDENTIALS.connection_status(api)


def _api_get(access_token: str, path_and_query: str, *, what: str) -> JSONObject:
    try:
        return json_request(
            "GET",
            f"{X_API_BASE_URL}{path_and_query}",
            headers={"authorization": f"Bearer {access_token}"},
            failure_message=f"X {what} request failed.",
            invalid_response_message=f"X {what} returned an invalid response.",
        )
    except WebRequestError as exc:
        raise _mapped_web_error(exc, what) from exc


def _mapped_web_error(exc: WebRequestError, what: str) -> Exception:
    if exc.status == 401:
        return IntegrationReconnectRequired(X_RECONNECT_MESSAGE)
    if exc.status == 429:
        return RuntimeError("X API rate limit was reached.")
    if exc.status == 403:
        return RuntimeError(f"X declined the {what} request (forbidden).")
    if exc.status:
        return RuntimeError(f"X API returned HTTP {exc.status}.")
    return RuntimeError(f"X {what} request failed.")


def _int_field(tool_input: JSONObject, key: str, *, default: int, low: int, high: int) -> int:
    value = tool_input.get(key)
    if value is None:
        return default
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        digits = value.strip()
        if len(digits) > 10:
            raise ToolInputValidationError(
                f"X tool_input.{key} must be between {low} and {high}."
            )
        value = int(digits)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolInputValidationError(f"X tool_input.{key} must be an integer or digit string.")
    if not low <= value <= high:
        raise ToolInputValidationError(f"X tool_input.{key} must be between {low} and {high}.")
    return value


def _usernames_by_id(response: JSONObject) -> dict[str, str]:
    includes = response.get("includes")
    users = includes.get("users") if isinstance(includes, dict) else None
    output: dict[str, str] = {}
    if isinstance(users, list):
        for user in users:
            if isinstance(user, dict) and isinstance(user.get("id"), str) and isinstance(user.get("username"), str):
                output[str(user["id"])] = str(user["username"])
    return output


def _tweet_summary(tweet: JSONObject, usernames: dict[str, str]) -> JSONObject:
    metrics = tweet.get("public_metrics")
    author_id = tweet.get("author_id")
    author_id_str = author_id if isinstance(author_id, str) else ""
    return {
        "id": str(tweet.get("id") or ""),
        "text": clip_text(str(tweet.get("text") or ""), 1_200),
        "author_id": author_id_str,
        "author_username": usernames.get(author_id_str, ""),
        "created_at": str(tweet.get("created_at") or ""),
        "metrics": cast(JSONObject, metrics) if isinstance(metrics, dict) else {},
    }


def _search_tweets(access_token: str, tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"query", "max_results"}
    if extra:
        raise ToolInputValidationError("X search tool input only supports query and max_results.")
    query = tool_input.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolInputValidationError("X tool_input.query is required.")
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise ToolInputValidationError(
            f"X search query must be at most {MAX_QUERY_CHARS} characters."
        )
    max_results = _int_field(tool_input, "max_results", default=10, low=10, high=100)
    params = encode_query(
        {
            "query": query,
            "max_results": str(max_results),
            "tweet.fields": TWEET_FIELDS,
            "expansions": "author_id",
            "user.fields": "username",
        }
    )
    response = _api_get(access_token, f"/tweets/search/recent?{params}", what="search")
    usernames = _usernames_by_id(response)
    data = response.get("data")
    tweets = [
        cast(JSONValue, _tweet_summary(cast(JSONObject, tweet), usernames))
        for tweet in (data if isinstance(data, list) else [])[:max_results]
        if isinstance(tweet, dict)
    ]
    return {
        "status": "success_executed",
        "message": f"X search returned {len(tweets)} post(s).",
        "tweets": tweets,
    }


def _read_tweet(access_token: str, tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"tweet_id"}
    if extra:
        raise ToolInputValidationError("X read tool input only supports tweet_id.")
    tweet_id = _valid_tweet_id(tool_input.get("tweet_id"), field="tweet_id")
    params = encode_query({"tweet.fields": TWEET_FIELDS, "expansions": "author_id", "user.fields": "username"})
    response = _api_get(access_token, f"/tweets/{tweet_id}?{params}", what="post lookup")
    data = response.get("data")
    if not isinstance(data, dict):
        return {"status": "success_executed", "message": "X post was not found.", "tweet": None}
    return {
        "status": "success_executed",
        "message": "X post loaded.",
        "tweet": _tweet_summary(cast(JSONObject, data), _usernames_by_id(response)),
    }


def _user_tweets(access_token: str, tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"username", "user_id", "max_results"}
    if extra:
        raise ToolInputValidationError("X user posts tool input only supports username, user_id, and max_results.")
    username = tool_input.get("username")
    user_id = tool_input.get("user_id")
    if (username is None) == (user_id is None):
        raise ToolInputValidationError("X user posts require exactly one of tool_input.username or tool_input.user_id.")
    if user_id is not None:
        if not isinstance(user_id, str) or not TWEET_ID_RE.fullmatch(user_id.strip()):
            raise ToolInputValidationError("X tool_input.user_id must be a numeric id string.")
        resolved_id = user_id.strip()
        resolved_username = ""
    else:
        if not isinstance(username, str) or not USERNAME_RE.fullmatch(username.strip()):
            raise ToolInputValidationError("X tool_input.username must be a valid X handle.")
        handle = username.strip().lstrip("@")
        response = _api_get(access_token, f"/users/by/username/{handle}", what="user lookup")
        data = response.get("data")
        resolved = data.get("id") if isinstance(data, dict) else None
        if not isinstance(resolved, str) or not TWEET_ID_RE.fullmatch(resolved):
            return {"status": "success_executed", "message": "X user was not found.", "tweets": []}
        resolved_id = resolved
        resolved_username = handle
    max_results = _int_field(tool_input, "max_results", default=10, low=5, high=100)
    params = encode_query({"max_results": str(max_results), "tweet.fields": TWEET_FIELDS})
    response = _api_get(access_token, f"/users/{resolved_id}/tweets?{params}", what="user posts")
    data = response.get("data")
    tweets: list[JSONValue] = []
    for tweet in (data if isinstance(data, list) else [])[:max_results]:
        if isinstance(tweet, dict):
            summary = _tweet_summary(cast(JSONObject, tweet), {})
            summary["author_id"] = resolved_id
            summary["author_username"] = resolved_username
            tweets.append(summary)
    return {
        "status": "success_executed",
        "message": f"X returned {len(tweets)} post(s) for the user.",
        "tweets": tweets,
    }


def _get_trends(api: HostAPI, tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"woeid", "max_trends"}
    if extra:
        raise ToolInputValidationError("X trends tool input only supports woeid and max_trends.")
    woeid = tool_input.get("woeid")
    if woeid is None:
        woeid = WORLDWIDE_WOEID
    if not isinstance(woeid, str) or not WOEID_RE.fullmatch(woeid.strip()):
        raise ToolInputValidationError("X tool_input.woeid must be a numeric WOEID string.")
    max_trends = _int_field(tool_input, "max_trends", default=20, low=1, high=50)
    params = encode_query({"max_trends": str(max_trends), "trend.fields": "trend_name,tweet_count"})
    # Trends lookups accept only app-only auth, so this action uses the
    # configured app Bearer token instead of the connected user's token. A 401
    # here therefore means the configured token is bad, not that the operator
    # account needs a reconnect.
    try:
        response = json_request(
            "GET",
            f"{X_API_BASE_URL}/trends/by/woeid/{woeid.strip()}?{params}",
            headers={"authorization": f"Bearer {api.config['X_BEARER_TOKEN']}"},
            failure_message="X trends request failed.",
            invalid_response_message="X trends returned an invalid response.",
        )
    except WebRequestError as exc:
        if exc.status == 401:
            raise RuntimeError(
                "X rejected the configured Bearer token. Update X_BEARER_TOKEN in the admin UI's Tools tab."
            ) from exc
        raise _mapped_web_error(exc, "trends") from exc
    data = response.get("data")
    trends: list[JSONValue] = []
    for trend in (data if isinstance(data, list) else [])[:max_trends]:
        if isinstance(trend, dict):
            tweet_count = trend.get("tweet_count")
            trends.append(
                {
                    "trend_name": str(trend.get("trend_name") or ""),
                    # tweet_count is optional in the provider response.
                    "tweet_count": tweet_count if isinstance(tweet_count, int) else None,
                }
            )
    place = "worldwide" if woeid.strip() == WORLDWIDE_WOEID else f"WOEID {woeid.strip()}"
    return {
        "status": "success_executed",
        "message": f"X returned {len(trends)} trending topic(s) for {place}.",
        "trends": trends,
    }


def _personalized_trends(access_token: str, tool_input: JSONObject) -> JSONObject:
    if tool_input:
        raise ToolInputValidationError("X personalized trends take no tool input.")
    params = encode_query({"personalized_trend.fields": "trend_name,category,post_count,trending_since"})
    response = _api_get(access_token, f"/users/personalized_trends?{params}", what="personalized trends")
    data = response.get("data")
    trends: list[JSONValue] = []
    for trend in (data if isinstance(data, list) else [])[:50]:
        if isinstance(trend, dict):
            post_count = trend.get("post_count")
            trends.append(
                {
                    "trend_name": str(trend.get("trend_name") or ""),
                    "category": str(trend.get("category") or ""),
                    "post_count": post_count if isinstance(post_count, int) else None,
                    "trending_since": str(trend.get("trending_since") or ""),
                }
            )
    return {
        "status": "success_executed",
        "message": f"X returned {len(trends)} personalized (For You) trend(s) for the connected account.",
        "trends": trends,
    }


def _valid_tweet_id(value: JSONValue | None, *, field: str) -> str:
    if not isinstance(value, str) or not TWEET_ID_RE.fullmatch(value.strip()):
        raise ToolInputValidationError(f"X tool_input.{field} must be a numeric post id string.")
    return value.strip()


def _post_proposal(tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"text", "in_reply_to_tweet_id", "quote_tweet_id"}
    if extra:
        raise ToolInputValidationError("X post tool input only supports text, in_reply_to_tweet_id, and quote_tweet_id.")
    text = tool_input.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ToolInputValidationError("X tool_input.text is required.")
    text = text.strip()
    # Reject rather than silently truncate: a truncated post would publish text
    # the agent did not intend and the operator could not see in full.
    if len(text) > MAX_TWEET_CHARS:
        raise ToolInputValidationError(f"X post text must be at most {MAX_TWEET_CHARS} characters.")
    proposal: JSONObject = {"text": text}
    if tool_input.get("in_reply_to_tweet_id") is not None:
        proposal["in_reply_to_tweet_id"] = _valid_tweet_id(tool_input.get("in_reply_to_tweet_id"), field="in_reply_to_tweet_id")
    if tool_input.get("quote_tweet_id") is not None:
        proposal["quote_tweet_id"] = _valid_tweet_id(tool_input.get("quote_tweet_id"), field="quote_tweet_id")
    if "in_reply_to_tweet_id" in proposal and "quote_tweet_id" in proposal:
        raise ToolInputValidationError("X post supports either in_reply_to_tweet_id or quote_tweet_id, not both.")
    return proposal


def _target_tweet_preview(access_token: str, tweet_id: str) -> JSONObject:
    """Current state of the post being replied to or quoted, captured at
    proposal time so the approval names the actual target and
    execute_approved can detect deletion (rule 8)."""
    params = encode_query({"tweet.fields": "author_id,created_at", "expansions": "author_id", "user.fields": "username"})
    response = _api_get(access_token, f"/tweets/{tweet_id}?{params}", what="post lookup")
    data = response.get("data")
    if not isinstance(data, dict):
        raise ToolInputValidationError("The referenced X post was not found.")
    # Require a definite id: the post-approval re-check compares this against the
    # approved id, so falling back to the requested id would make a response
    # with no id trivially "match" and skip real verification.
    resolved_id = data.get("id")
    if not isinstance(resolved_id, str) or resolved_id != tweet_id:
        raise ToolInputValidationError("The referenced X post was not found.")
    usernames = _usernames_by_id(response)
    author_id = data.get("author_id")
    return {
        "id": resolved_id,
        "author_username": usernames.get(author_id if isinstance(author_id, str) else "", ""),
        "text": clip_text(str(data.get("text") or ""), 300),
    }


def _post_summary(proposal: JSONObject, account_label: str, target: JSONObject | None) -> str:
    text = str(proposal.get("text") or "")
    # Disclose the full length so a summary-only reader knows how much is
    # clipped; the operator can expand the exact payload to read all of it.
    count = f"{len(text)}-char post"
    account_label = clip_text(account_label, 80)
    # Clip progressively so the whole summary stays within the host's
    # 500-byte cap while every disclosure stays present.
    for text_clip, target_clip in ((240, 120), (160, 80), (100, 40)):
        if target is not None and "in_reply_to_tweet_id" in proposal:
            summary = (
                f"Reply on X as {account_label} to post {target.get('id')} by "
                f"@{target.get('author_username') or 'unknown'} (\"{clip_text(str(target.get('text') or ''), target_clip)}\"), "
                f"{count}: \"{clip_text(text, text_clip)}\""
            )
        elif target is not None:
            summary = (
                f"Quote-post on X as {account_label} of post {target.get('id')} by "
                f"@{target.get('author_username') or 'unknown'} (\"{clip_text(str(target.get('text') or ''), target_clip)}\"), "
                f"{count}: \"{clip_text(text, text_clip)}\""
            )
        else:
            summary = f"Post on X as {account_label}, {count}: \"{clip_text(text, text_clip)}\""
        if len(summary.encode("utf-8")) <= SUMMARY_MAX_BYTES:
            return summary
    return summary


class XTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> CredentialFlow:
        return XCredentialFlow()

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            if action == "search_tweets":
                return ActionExecuted(_search_tweets(X_CREDENTIALS.access_token(api), tool_input))
            if action == "read_tweet":
                return ActionExecuted(_read_tweet(X_CREDENTIALS.access_token(api), tool_input))
            if action == "user_tweets":
                return ActionExecuted(_user_tweets(X_CREDENTIALS.access_token(api), tool_input))
            if action == "get_trends":
                return ActionExecuted(_get_trends(api, tool_input))
            if action == "get_personalized_trends":
                return ActionExecuted(_personalized_trends(X_CREDENTIALS.access_token(api), tool_input))
            if action == "post_tweet":
                proposal = _post_proposal(tool_input)
                access_token = X_CREDENTIALS.access_token(api)
                account = X_CREDENTIALS.refresh_identity(api, access_token)
                target_id = proposal.get("in_reply_to_tweet_id") or proposal.get("quote_tweet_id")
                target: JSONObject | None = None
                if isinstance(target_id, str):
                    target = _target_tweet_preview(access_token, target_id)
                payload: JSONObject = {
                    "action": action,
                    "tool_id": MANIFEST.tool_id,
                    "x_account": {"id": account["id"], "label": account["label"]},
                    "proposal": proposal,
                }
                if target is not None:
                    payload["target_tweet"] = target
                approval = api.approvals.request(
                    action_id=action,
                    summary=_post_summary(proposal, account["label"], target),
                    payload=payload,
                )
                return ActionPendingApproval(approval.approval_id, approval.summary)
            return ActionFailed("Unsupported X action.")
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except IntegrationReconnectRequired as exc:
            return ActionFailed(str(exc), reconnect_required=True)
        except Exception as exc:
            return ActionFailed(str(exc) or "X tool request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        try:
            # The host hands a loaded record: approved, and this tool's own.
            if approval.action_id != "post_tweet":
                return ActionFailed("X approval action is invalid.")
            payload = approval.payload
            proposal = payload.get("proposal")
            if not isinstance(proposal, dict):
                return ActionFailed("X approval payload is invalid.")
            proposal_object = cast(JSONObject, proposal)
            access_token = X_CREDENTIALS.access_token(api)
            current_account = X_CREDENTIALS.refresh_identity(api, access_token)
            approved_account = payload.get("x_account")
            if not isinstance(approved_account, dict):
                return ActionFailed("X approval payload is invalid.")
            if approved_account.get("id") != current_account["id"]:
                return ActionFailed("X account changed after approval. Please queue a new approval.")
            approved_target = payload.get("target_tweet")
            if isinstance(approved_target, dict):
                proposal_target = proposal_object.get("in_reply_to_tweet_id") or proposal_object.get("quote_tweet_id")
                if approved_target.get("id") != proposal_target:
                    return ActionFailed("X approval target is invalid. Please queue a new approval.")
                # Rule 8: the referenced post must still exist (posts cannot be
                # edited into something else on the API tier, but they can be
                # deleted or hidden between proposal and approval).
                current_target = _target_tweet_preview(access_token, str(approved_target.get("id") or ""))
                if current_target.get("id") != approved_target.get("id"):
                    return ActionFailed("The referenced X post changed after approval. Please queue a new approval.")
            body: JSONObject = {"text": proposal_object.get("text")}
            reply_id = proposal_object.get("in_reply_to_tweet_id")
            if isinstance(reply_id, str):
                body["reply"] = {"in_reply_to_tweet_id": reply_id}
            quote_id = proposal_object.get("quote_tweet_id")
            if isinstance(quote_id, str):
                body["quote_tweet_id"] = quote_id
            try:
                response = json_request(
                    "POST",
                    f"{X_API_BASE_URL}/tweets",
                    headers={"authorization": f"Bearer {access_token}"},
                    body=body,
                    failure_message="X post request failed.",
                    invalid_response_message="X post returned an invalid response.",
                )
            except WebRequestError as exc:
                raise _mapped_web_error(exc, "post") from exc
            data = response.get("data")
            posted_id = data.get("id") if isinstance(data, dict) else None
            if not isinstance(posted_id, str) or not posted_id:
                return ActionFailed("X did not confirm the new post.")
            return ApprovalExecuted(f"Posted to X as {current_account['label']} (post id {posted_id}).")
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except IntegrationReconnectRequired as exc:
            return ActionFailed(str(exc), reconnect_required=True)
        except Exception as exc:
            return ActionFailed(str(exc) or "X post failed after approval.")


# The instance the host discovers (see host.runtime.tools.tools_host).
BUNDLED_TOOL = XTool()
