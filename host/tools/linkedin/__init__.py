"""LinkedIn tool package."""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from typing import cast

from host.tools.json_types import JSONObject
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
from host.tools.host_api import ApprovalRecord, ConnectionAccount, HostAPI
from host.tools.shared.google import clip_text
from host.tools.shared.oauth2 import (
    IntegrationReconnectRequired,
    access_token_is_fresh,
    clear_if_still_loaded,
    now,
    signed_state,
    verify_state,
)
from host.tools.shared.web import WebRequestError, json_request, json_request_with_headers

LINKEDIN_AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
LINKEDIN_POSTS_URL = "https://api.linkedin.com/rest/posts"
# /rest/* calls require a pinned monthly LinkedIn-Version; each version is
# supported for at least a year, so bump this periodically.
LINKEDIN_VERSION = "202606"
LINKEDIN_OAUTH_SCOPES = ("openid", "profile", "email", "w_member_social")
REQUIRED_LINKEDIN_SCOPES = frozenset(LINKEDIN_OAUTH_SCOPES)
LINKEDIN_RECONNECT_MESSAGE = (
    "LinkedIn is no longer connected (LinkedIn issues 60-day tokens without refresh). "
    "Please reconnect the tool."
)
DEFAULT_TOKEN_LIFETIME_SECONDS = 60 * 24 * 3600
MAX_POST_CHARS = 3_000
# "Little text" reserved characters; unescaped they are parsed as annotations.
LITTLE_TEXT_RESERVED = "\\|{}@[]()<>#*_~"
# Host approval summaries are capped at 500 UTF-8 bytes (tools_host SUMMARY_MAX_BYTES).
SUMMARY_MAX_BYTES = 500


class ToolInputValidationError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


LINKEDIN_OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}},
    "additionalProperties": True,
}


MANIFEST = ToolManifest(
    tool_id="linkedin",
    display_name="LinkedIn",
    description="Connect your personal LinkedIn profile and let your agent read its profile details and publish posts with your approval. LinkedIn's self-serve API cannot read posts or comments, so use LinkedIn Discovery to search public posts.",
    connection="oauth",
    actions=(
        ActionSpec(id="get_profile",
            description="Read the connected personal LinkedIn profile's name, email, picture, and LinkedIn id. This cannot read posts, the feed, other profiles, or search LinkedIn content.",
            data_policy=(
                "Read-only. Fetches the connected personal profile from LinkedIn "
                "and returns it to the host and active model context. Runs directly with no approval."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema=LINKEDIN_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="create_post",
            description="Queue approval to publish a text-only LinkedIn post from the connected personal profile, visible publicly or to connections. This creates a post but cannot read it back through LinkedIn's self-serve API.",
            data_policy=(
                "Publishes a post from the connected LinkedIn account, visible per the "
                "chosen visibility. Queued for explicit approval; nothing reaches "
                "LinkedIn until you approve. The proposal and sanitized created-post id "
                "are available to the active model."
            ),
            input_schema={
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string", "description": "Post text (up to 3000 chars)."},
                    "visibility": {"type": "string", "enum": ["PUBLIC", "CONNECTIONS"], "description": "Default PUBLIC."},
                },
                "additionalProperties": False,
            },
            output_schema=LINKEDIN_OUTPUT_SCHEMA,
            approval="operator",
        ),
    ),
    config=(
        ConfigRequirement(key="LINKEDIN_OAUTH_CLIENT_ID", description="LinkedIn developer app client id."),
        ConfigRequirement(key="LINKEDIN_OAUTH_CLIENT_SECRET", description="LinkedIn developer app client secret."),
    ),
    protections=(
        "Your LinkedIn OAuth client secret and connected-account tokens stay in the host credential store and are never returned to or read by the agent.",
        "OAuth connects one personal LinkedIn profile. Only someone with access to this host's protected Connect flow can choose or replace that profile.",
        "LinkedIn's self-serve API cannot read the connected profile's feed or public posts; indexed discovery is a separate no-login tool.",
        "Publishing happens only after your approval.",
    ),
    setup_steps=(
        SetupStep(
            title="Understand why you need a LinkedIn Page",
            description="LinkedIn requires every developer app, including a personal self-serve app, to name a LinkedIn Page as its publisher. The Page is public and identifies the organization responsible for the app; it does not become the account your agent uses. TrustyClaw connects your personal profile and never reads from or posts to the Page. Your profile must be a Page super admin only so you can approve the app association. Other Page admins can see your admin role, but the role does not add the Page to your profile's Experience section. If you want a public link between them, add the organization to your profile separately, and only when that relationship is accurate.",
            link_url="https://www.linkedin.com/help/linkedin/answer/a548360/associate-an-app-with-a-linkedin-page",
            link_label="See why LinkedIn requires a Page",
        ),
        SetupStep(
            title="Use or create a minimal LinkedIn Page",
            description="Use an existing LinkedIn Page where your personal profile is a super admin. If you have none, on LinkedIn desktop open For Business > Create a Company Page > Company. Enter a truthful project or solo-business name, an available public URL, your public TrustyClaw or GitHub URL as Website, the closest Industry, Myself Only as Company size, and the closest accurate Company type; skip optional profile details and create the Page. The Page needs no followers or posts. LinkedIn records it only as the developer app's publisher; TrustyClaw never reads from or posts to it.",
            link_url="https://www.linkedin.com/help/linkedin/answer/a543852",
            link_label="View LinkedIn's Page creation steps",
        ),
        SetupStep(
            title="Create and verify the developer app",
            description="Open the LinkedIn Developer Portal > My apps > Create app. Enter an app name and select your Page. LinkedIn requires a public HTTPS privacy-policy URL and a square app image. For the smallest personal setup, use an existing policy or a public GitHub Gist containing one sentence that this personal app uses LinkedIn OAuth plus your contact email, and upload any plain temporary square image. Accept the terms and create the app. Then open Settings and, if the Page association is not verified, choose Verify > Generate URL, open that URL as the Page super admin, approve it, and confirm Settings now shows the association as verified.",
            link_url="https://www.linkedin.com/help/lms/answer/a1667239",
            link_label="View LinkedIn's app creation fields",
        ),
        SetupStep(
            title="Add the two self-service products",
            description="Open the app's Products tab. Find Sign In with LinkedIn using OpenID Connect, choose Request access, accept any displayed terms, and wait until it shows Added or Approved. Do the same for Share on LinkedIn. Then open the Auth tab and confirm OAuth 2.0 scopes includes openid, profile, email, and w_member_social. The first three identify the connected personal profile; w_member_social allows approved posts from it. These self-serve products do not allow reading feeds, posts, comments, or other profiles, and they do not grant LinkedIn's separately vetted comment-writing permission.",
            link_url="https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin",
            link_label="View Share on LinkedIn setup",
        ),
        SetupStep(
            title="Register the callback URI and copy the credentials",
            show_callback=True,
            description="Stay on the app's Auth tab. Under OAuth 2.0 settings, find Authorized redirect URLs for your app, choose the edit pencil, paste the exact Callback URI for this host displayed in the Connection section below, choose Update, and confirm the URI appears in the saved list. Copy the Client ID. Reveal and copy the Primary Client Secret; store it only in the write-only TrustyClaw field, never in an agent prompt. The callback's scheme, hostname, port, path, and trailing slash must match exactly or LinkedIn rejects Connect.",
        ),
        SetupStep(
            title="Configure and connect TrustyClaw",
            show_config=True,
            description="Expand LinkedIn in Internet Access and Tools. Save the Client ID as LINKEDIN_OAUTH_CLIENT_ID and Primary Client Secret as LINKEDIN_OAUTH_CLIENT_SECRET, enable the tool, choose Connect, and approve the four displayed scopes while signed in to the personal profile you want the agent to use. TrustyClaw replaces no account silently; confirm the row shows that profile's expected name and email before giving the agent access.",
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                points=(
                    DataSummaryPoint(label="Reads", text="Profile reads send only the access token; no other host data accompanies them."),
                    DataSummaryPoint(label="Posts", text="A post reaches LinkedIn only after your approval, and sends exactly the approved text and visibility."),
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(label="LinkedIn", text="The OAuth connection and reads stay within LinkedIn's services under the connected personal profile."),
                    DataSummaryPoint(label="Your network and beyond", text="An approved post is published on LinkedIn under the visibility you approved and stays there until removed."),
                ),
            ),
            DataSummaryCard(
                title="What LinkedIn can do with it",
                description=(
                    "LinkedIn processes member identity, API request metadata, and published content under its Privacy Policy: "
                    "service personalization, analytics, safety, sharing, and legal uses, with the member controls your account "
                    "already has."
                ),
                links=(
                    DataSummaryLink(label="LinkedIn Privacy Policy", url="https://www.linkedin.com/legal/privacy-policy"),
                    DataSummaryLink(label="LinkedIn API Terms of Use", url="https://www.linkedin.com/legal/l/api-terms-of-use"),
                ),
            ),
            DataSummaryCard(
                title="How long LinkedIn retains it",
                description=(
                    "Published content stays on LinkedIn until the member or LinkedIn removes it, and LinkedIn keeps account and "
                    "security records under its policy. The access token expires on its own; disconnect clears only the local "
                    "credential."
                ),
                links=(
                    DataSummaryLink(label="LinkedIn Privacy Policy", url="https://www.linkedin.com/legal/privacy-policy"),
                ),
            ),
        ),
    ),
)


def _escape_little_text(text: str) -> str:
    """Escape LinkedIn 'little text' reserved characters so post text
    is published literally instead of being parsed as annotations."""
    output: list[str] = []
    for char in text:
        if char in LITTLE_TEXT_RESERVED:
            output.append(f"\\{char}")
        else:
            output.append(char)
    return "".join(output)


def _fetch_userinfo(access_token: str) -> JSONObject:
    return json_request(
        "GET",
        LINKEDIN_USERINFO_URL,
        headers={"authorization": f"Bearer {access_token}"},
        failure_message="LinkedIn profile lookup failed.",
        invalid_response_message="LinkedIn profile lookup returned an invalid response.",
    )


def _account_from_userinfo(userinfo: JSONObject, scopes: list[str]) -> ConnectionAccount:
    sub = userinfo.get("sub")
    if (
        not isinstance(sub, str)
        or not sub
        or len(sub) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in sub)
    ):
        raise RuntimeError("LinkedIn did not return a stable member id.")
    email = userinfo.get("email")
    name = userinfo.get("name")
    label = email if isinstance(email, str) and email else (name if isinstance(name, str) and name else sub)
    return {"id": sub, "label": label, "scopes": scopes}


class LinkedInCredentialStore:
    """OAuth 2.0 authorization code against LinkedIn. Self-serve apps get
    60-day access tokens and no refresh tokens: expiry surfaces as
    reconnect_required rather than a silent refresh."""

    def start_connect(self, params: OAuthStartConnectParams, api: HostAPI) -> OAuthStartConnectResult:
        state = signed_state(secret=api.config["LINKEDIN_OAUTH_CLIENT_SECRET"], tool_id=MANIFEST.tool_id)
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": api.config["LINKEDIN_OAUTH_CLIENT_ID"],
                "redirect_uri": params["redirect_uri"],
                "scope": " ".join(LINKEDIN_OAUTH_SCOPES),
                "state": state,
            }
        )
        return {"authorization_url": f"{LINKEDIN_AUTHORIZE_URL}?{query}", "state": state}

    def complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult:
        verify_state(params["state"], secret=api.config["LINKEDIN_OAUTH_CLIENT_SECRET"], tool_id=MANIFEST.tool_id)
        token_response = json_request(
            "POST",
            LINKEDIN_TOKEN_URL,
            form={
                "grant_type": "authorization_code",
                "code": params["code"],
                "client_id": api.config["LINKEDIN_OAUTH_CLIENT_ID"],
                "client_secret": api.config["LINKEDIN_OAUTH_CLIENT_SECRET"],
                "redirect_uri": params["redirect_uri"],
            },
            failure_message="LinkedIn OAuth token exchange failed.",
            invalid_response_message="LinkedIn OAuth token exchange returned an invalid response.",
        )
        access_token = token_response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("LinkedIn OAuth token exchange returned no access token.")
        scope_field = token_response.get("scope")
        granted_scopes = scope_field.split(",") if isinstance(scope_field, str) and "," in str(scope_field) else str(
            scope_field or ""
        ).split()
        granted_scopes = [scope.strip() for scope in granted_scopes if scope.strip()]
        if REQUIRED_LINKEDIN_SCOPES - set(granted_scopes):
            raise RuntimeError("LinkedIn connection is missing required permissions.")
        expires_in = token_response.get("expires_in")
        userinfo = _fetch_userinfo(access_token)
        account = _account_from_userinfo(userinfo, granted_scopes)
        existing = api.credentials.load()
        created_at = existing["metadata"].get("created_at") if existing is not None else None
        current_time = now()
        api.credentials.save(
            {
                "account": account,
                "secret": {
                    "access_token": access_token,
                    "expires_at": current_time
                    + (expires_in if isinstance(expires_in, int) else DEFAULT_TOKEN_LIFETIME_SECONDS),
                },
                "metadata": {
                    "created_at": created_at if isinstance(created_at, int) else current_time,
                    "updated_at": current_time,
                },
            }
        )
        return {"account": account}

    def disconnect(self, api: HostAPI) -> None:
        # LinkedIn has no self-serve token revocation endpoint; the 60-day
        # token simply ages out after the credential is cleared.
        api.credentials.clear()

    def connection_status(self, api: HostAPI) -> ConnectionStatus:
        existing = api.credentials.load()
        if existing is None:
            return {"connected": False}
        return {"connected": True, "account": existing["account"]}

    def access_token(self, api: HostAPI) -> str:
        existing = api.credentials.load()
        if existing is None:
            raise IntegrationReconnectRequired(LINKEDIN_RECONNECT_MESSAGE)
        payload = cast(Mapping[str, object], existing["secret"])
        if not access_token_is_fresh(payload, now()):
            clear_if_still_loaded(api, existing)
            raise IntegrationReconnectRequired(LINKEDIN_RECONNECT_MESSAGE)
        return str(payload.get("access_token") or "")

    def refresh_identity(self, api: HostAPI, access_token: str) -> ConnectionAccount:
        existing = api.credentials.load()
        if existing is None:
            raise IntegrationReconnectRequired(LINKEDIN_RECONNECT_MESSAGE)
        account = _account_from_userinfo(_fetch_userinfo(access_token), existing["account"]["scopes"])
        if existing["account"]["id"] != account["id"]:
            raise IntegrationReconnectRequired(LINKEDIN_RECONNECT_MESSAGE)
        return account


LINKEDIN_CREDENTIALS = LinkedInCredentialStore()


class LinkedInCredentialFlow(CredentialFlow):
    def start_connect(self, params: OAuthStartConnectParams, api: HostAPI) -> OAuthStartConnectResult:
        return LINKEDIN_CREDENTIALS.start_connect(params, api)

    def complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult:
        return LINKEDIN_CREDENTIALS.complete_connect(params, api)

    def disconnect(self, api: HostAPI) -> None:
        LINKEDIN_CREDENTIALS.disconnect(api)

    def connection_status(self, api: HostAPI) -> ConnectionStatus:
        return LINKEDIN_CREDENTIALS.connection_status(api)


def _rest_headers(access_token: str) -> dict[str, str]:
    return {
        "authorization": f"Bearer {access_token}",
        "linkedin-version": LINKEDIN_VERSION,
        "x-restli-protocol-version": "2.0.0",
    }


def _mapped_web_error(exc: WebRequestError, what: str) -> Exception:
    if exc.status == 401:
        return IntegrationReconnectRequired(LINKEDIN_RECONNECT_MESSAGE)
    if exc.status == 403:
        return RuntimeError(
            f"LinkedIn declined the {what} request (forbidden). The app may be missing the required product."
        )
    if exc.status == 429:
        return RuntimeError("LinkedIn API rate limit was reached.")
    if exc.status == 422:
        return RuntimeError(f"LinkedIn rejected the {what} content (for example a duplicate post).")
    if exc.status:
        return RuntimeError(f"LinkedIn API returned HTTP {exc.status}.")
    return RuntimeError(f"LinkedIn {what} request failed.")


def _post_proposal(tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"text", "visibility"}
    if extra:
        raise ToolInputValidationError("LinkedIn post tool input only supports text and visibility.")
    text = tool_input.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ToolInputValidationError("LinkedIn tool_input.text is required.")
    visibility = tool_input.get("visibility")
    if visibility is None:
        visibility = "PUBLIC"
    if visibility not in {"PUBLIC", "CONNECTIONS"}:
        raise ToolInputValidationError("LinkedIn tool_input.visibility must be PUBLIC or CONNECTIONS.")
    text = text.strip()
    # LinkedIn's commentary limit applies to the escaped "little text", so check
    # the escaped length; reject rather than truncate so nothing publishes that
    # the agent did not intend or that would 422 only after approval.
    if len(_escape_little_text(text)) > MAX_POST_CHARS:
        raise ToolInputValidationError(f"LinkedIn post text must be at most {MAX_POST_CHARS} characters after escaping.")
    return {"text": text, "visibility": visibility}


def _summary(proposal: JSONObject, account_label: str) -> str:
    text = str(proposal.get("text") or "")
    # Clip the provider-supplied account label too, so a long display name can't
    # push the summary past the host's byte cap.
    label = clip_text(account_label, 80)
    count = f"{len(text)}-char"
    for text_clip in (300, 200, 120):
        summary = (
            f"Post on LinkedIn as {label} ({proposal.get('visibility')}), {count}: "
            f"\"{clip_text(text, text_clip)}\""
        )
        if len(summary.encode("utf-8")) <= SUMMARY_MAX_BYTES:
            return summary
    return summary


def _create_post(access_token: str, member_id: str, proposal: JSONObject) -> str:
    body: JSONObject = {
        "author": f"urn:li:person:{member_id}",
        "commentary": _escape_little_text(str(proposal.get("text") or "")),
        "visibility": proposal.get("visibility"),
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    try:
        _, headers = json_request_with_headers(
            "POST",
            LINKEDIN_POSTS_URL,
            headers=_rest_headers(access_token),
            body=body,
            failure_message="LinkedIn post request failed.",
            invalid_response_message="LinkedIn post returned an invalid response.",
        )
    except WebRequestError as exc:
        raise _mapped_web_error(exc, "post") from exc
    return headers.get("x-restli-id", "")


class LinkedInTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> CredentialFlow:
        return LinkedInCredentialFlow()

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            if action == "get_profile":
                if tool_input:
                    raise ToolInputValidationError("LinkedIn get_profile takes no input.")
                access_token = LINKEDIN_CREDENTIALS.access_token(api)
                userinfo = _fetch_userinfo(access_token)
                result: JSONObject = {
                    "status": "success_executed",
                    "message": "LinkedIn profile loaded.",
                    "member_id": str(userinfo.get("sub") or ""),
                    "name": str(userinfo.get("name") or ""),
                    "email": str(userinfo.get("email") or ""),
                    "picture": str(userinfo.get("picture") or ""),
                }
                return ActionExecuted(result)
            if action == "create_post":
                proposal = _post_proposal(tool_input)
                access_token = LINKEDIN_CREDENTIALS.access_token(api)
                account = LINKEDIN_CREDENTIALS.refresh_identity(api, access_token)
                payload: JSONObject = {
                    "action": action,
                    "tool_id": MANIFEST.tool_id,
                    "linkedin_account": {"id": account["id"], "label": account["label"]},
                    "proposal": proposal,
                }
                approval = api.approvals.request(
                    action_id=action, summary=_summary(proposal, account["label"]), payload=payload
                )
                return ActionPendingApproval(approval.approval_id, approval.summary)
            return ActionFailed("Unsupported LinkedIn action.")
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except IntegrationReconnectRequired as exc:
            return ActionFailed(str(exc), reconnect_required=True)
        except WebRequestError as exc:
            mapped = _mapped_web_error(exc, "profile")
            if isinstance(mapped, IntegrationReconnectRequired):
                return ActionFailed(str(mapped), reconnect_required=True)
            return ActionFailed(str(mapped))
        except Exception as exc:
            return ActionFailed(str(exc) or "LinkedIn tool request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        try:
            # The host hands a loaded record: approved, and this tool's own.
            payload = approval.payload
            proposal = payload.get("proposal")
            if not isinstance(proposal, dict):
                return ActionFailed("LinkedIn approval payload is invalid.")
            proposal_object = cast(JSONObject, proposal)
            access_token = LINKEDIN_CREDENTIALS.access_token(api)
            current_account = LINKEDIN_CREDENTIALS.refresh_identity(api, access_token)
            approved_account = payload.get("linkedin_account")
            if not isinstance(approved_account, dict):
                return ActionFailed("LinkedIn approval payload is invalid.")
            if approved_account.get("id") != current_account["id"]:
                return ActionFailed("LinkedIn account changed after approval. Please queue a new approval.")
            if approval.action_id == "create_post":
                post_id = _create_post(access_token, current_account["id"], proposal_object)
                suffix = f" (post {post_id})" if post_id else ""
                return ApprovalExecuted(f"Posted to LinkedIn as {current_account['label']}{suffix}.")
            return ActionFailed("LinkedIn approval payload is invalid.")
        except IntegrationReconnectRequired as exc:
            return ActionFailed(str(exc), reconnect_required=True)
        except WebRequestError as exc:
            mapped = _mapped_web_error(exc, "profile")
            if isinstance(mapped, IntegrationReconnectRequired):
                return ActionFailed(str(mapped), reconnect_required=True)
            return ActionFailed(str(mapped))
        except Exception as exc:
            return ActionFailed(str(exc) or "LinkedIn write failed after approval.")


# The instance the host discovers (see host.runtime.tools.tools_host).
BUNDLED_TOOL = LinkedInTool()
