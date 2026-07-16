"""Gmail tool package."""

from __future__ import annotations

import base64
import html
import re
import urllib.error
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, NoReturn, cast

from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import (
    ActionSpec,
    ConfigRequirement,
    DataSummary,
    DataSummaryCard,
    DataSummaryLink,
    DataSummaryPoint,
    SetupStep,
    ToolManifest,
)
from host.tools.results import (
    ActionExecuted,
    ActionFailed,
    ActionPendingApproval,
    ActionResult,
    ApprovalExecuted,
    ApprovalResult,
)
from host.tools.tool import CredentialFlow
from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.shared.google import GoogleCredentialStore, IntegrationReconnectRequired, clip_text

from .api import (
    GMAIL_DRAFT_ATTACHMENT_UNSUPPORTED_MESSAGE,
    ToolInputValidationError,
    execute_gmail_api_request,
    gmail_draft_preview,
    gmail_label_list_summary,
    gmail_label_preview,
    gmail_label_summaries,
    gmail_message_action_summaries,
    gmail_message_index_entry,
    gmail_operation_request,
    gmail_readable_message,
    gmail_search_messages,
    json_object,
    string_value,
)

GOOGLE_OAUTH_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
)
REQUIRED_GMAIL_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.labels",
        "https://www.googleapis.com/auth/gmail.modify",
    }
)
GMAIL_RECONNECT_MESSAGE = (
    "Gmail needs to be re-enabled because its saved connection is missing newly required permissions. "
    "Please reconnect Gmail from Integrations."
)
# The host caps approval summaries at 500 UTF-8 bytes (tools_host
# SUMMARY_MAX_BYTES). Summaries that can carry many disclosures at once budget
# the flexible body preview against this so a valid action is never blocked.
GMAIL_SUMMARY_MAX_BYTES = 500
GMAIL_THREAD_MESSAGE_INDEX_MAX_RESULTS = 100
DEFAULT_DRAFT_PAGE_LIMIT = 10
MAX_GMAIL_MESSAGE_ACTION_IDS = 20
MAX_GMAIL_MESSAGE_ACTION_LABEL_IDS = 50
GMAIL_SEND_ACTION_TYPE = "gmail_propose_send"
GMAIL_MESSAGE_ACTION_TYPES_BY_TOOL_ACTION = {
    "archive": "gmail_propose_archive_messages",
    "mark_read": "gmail_propose_mark_messages_read",
    "mark_unread": "gmail_propose_mark_messages_unread",
    "star": "gmail_propose_star_messages",
    "unstar": "gmail_propose_unstar_messages",
    "add_labels": "gmail_propose_add_message_labels",
    "remove_labels": "gmail_propose_remove_message_labels",
    "trash": "gmail_propose_trash_message",
    "untrash": "gmail_propose_untrash_message",
}
GMAIL_LABEL_ACTION_TYPES_BY_TOOL_ACTION = {
    "create": "gmail_propose_create_label",
    "update": "gmail_propose_update_label",
    "delete": "gmail_propose_delete_label",
}
GMAIL_DRAFT_ACTION_TYPES_BY_TOOL_ACTION = {
    "create": "gmail_propose_create_draft",
    "update": "gmail_propose_update_draft",
    "send": "gmail_propose_send_draft",
    "delete": "gmail_propose_delete_draft",
}
GMAIL_DRAFT_ACTION_TYPES = frozenset(GMAIL_DRAFT_ACTION_TYPES_BY_TOOL_ACTION.values())
GMAIL_LABEL_ACTION_TYPES = frozenset(GMAIL_LABEL_ACTION_TYPES_BY_TOOL_ACTION.values())
GMAIL_MESSAGE_ACTION_TYPES = frozenset(GMAIL_MESSAGE_ACTION_TYPES_BY_TOOL_ACTION.values())
GMAIL_LABEL_BACKGROUND_COLORS = {
    "red": "#cc3a21",
    "orange": "#eaa041",
    "yellow": "#f2c960",
    "green": "#149e60",
    "teal": "#2a9c68",
    "blue": "#3c78d8",
    "purple": "#8e63ce",
    "pink": "#e07798",
    "gray": "#666666",
}
GMAIL_LABEL_TEXT_COLORS = {"black": "#000000", "white": "#ffffff"}


def _schema(properties: JSONObject, required: list[str] | None = None) -> JSONObject:
    schema: JSONObject = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = cast(list[JSONValue], required)
    return schema


GMAIL_OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}},
    "additionalProperties": True,
}
GMAIL_DIRECT_ACTIONS = frozenset({"search_messages", "read_message", "read_thread", "list_labels", "list_drafts"})


BODY_BLOCK_SCHEMA: JSONObject = {
    "type": "array",
    "description": "Ordered email body blocks: paragraphs contain text; line groups contain one or more lines.",
    "items": {
        "oneOf": [
            {
                "type": "object",
                "properties": {"type": {"type": "string", "enum": ["paragraph"]}, "text": {"type": "string"}},
                "required": ["type", "text"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["line_group"]},
                    "lines": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["type", "lines"],
                "additionalProperties": False,
            },
        ]
    },
}


# Per-action data policies (exposed through the admin API; the combined
# operator-facing data story lives in the manifest data_summary).
GMAIL_SEARCH_POLICY = (
    "Searches your mailbox with a Gmail query string; only the query and any start_time/end_time filters go to "
    "Google. Runs directly with no approval."
)
GMAIL_READ_MESSAGE_POLICY = "Reads one message from your mailbox; only its message id goes to Google. Runs directly with no approval."
GMAIL_READ_THREAD_POLICY = "Reads one conversation thread from your mailbox; only its thread id goes to Google. Runs directly with no approval."
GMAIL_LIST_LABELS_POLICY = "Lists your labels; no search text goes to Google. Runs directly with no approval."
GMAIL_LIST_DRAFTS_POLICY = (
    "Lists your drafts with their content; only an optional query and paging values go to Google. Runs directly with no approval."
)
GMAIL_SEND_POLICY = (
    "Sends an email from your account to the recipients you approved. The drafted recipients, subject, and body "
    "reach Google only after your approval."
)
GMAIL_MESSAGE_ACTION_POLICY = (
    "Archives, relabels, trashes/untrashes, marks read/unread, stars, or unstars the listed messages. "
    "Nothing reaches Google before your approval of the exact operation."
)
GMAIL_LABEL_ACTION_POLICY = "Creates, updates, or deletes one label. Nothing reaches Google before your approval."
GMAIL_DRAFT_ACTION_POLICY = "Creates, updates, sends, or deletes one draft. Nothing reaches Google before your approval."


MANIFEST = ToolManifest(
    tool_id="gmail",
    display_name="Gmail",
    description="Connect your Google account and let your agent read and search email, and send messages or organize your mailbox with your approval.",
    connection="oauth",
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                points=(
                    DataSummaryPoint(label="Reads", text="Read queries and their parameters go to Google directly: the Gmail search query, time filters, and message, thread, draft, or label identifiers."),
                    DataSummaryPoint(label="Modifications", text="A change reaches Google only after your approval, and sends exactly what you approved: recipients, subject, and body for a send or draft, or the label and message changes otherwise."),
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                description=(
                    "Everything goes to your own Gmail account and stays there. Nothing in your mailbox changes or leaves it "
                    "without your approval; the one way out is an approved send, which delivers that email to its recipients."
                ),
            ),
            DataSummaryCard(
                title="What Google can do with it",
                description=(
                    "Google handles it under the Privacy Policy that already covers your Gmail, the same as mail you read or send "
                    "in a browser."
                ),
                links=(
                    DataSummaryLink(label="Google Privacy Policy", url="https://policies.google.com/privacy"),
                ),
            ),
            DataSummaryCard(
                title="How long Google retains it",
                description="Gmail content remains in your account until you delete it. Google says deletion generally completes within about 2 months, plus up to 6 months in encrypted backups.",
                links=(
                    DataSummaryLink(label="Google data retention policy", url="https://policies.google.com/technologies/retention"),
                ),
            ),
        ),
    ),
    actions=(
        ActionSpec(id="search_messages",
            description="Search messages with a Gmail query string.",
            data_policy=GMAIL_SEARCH_POLICY,
            input_schema=_schema({"query": {"type": "string", "description": "Optional Gmail search syntax, e.g. from:alice@example.com is:unread."}, "start_time": {"type": "string", "description": "Optional inclusive lower bound as ISO 8601 or YYYY-MM-DD; naive values are UTC."}, "end_time": {"type": "string", "description": "Optional exclusive upper bound as ISO 8601 or YYYY-MM-DD; must be after start_time."}}),
            output_schema=GMAIL_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="read_message",
            description="Read one message by id.",
            data_policy=GMAIL_READ_MESSAGE_POLICY,
            input_schema=_schema({"message_id": {"type": "string", "description": "Gmail message id returned by search_messages, read_thread, or list_drafts."}}, ["message_id"]),
            output_schema=GMAIL_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="read_thread",
            description="Read a conversation thread by id.",
            data_policy=GMAIL_READ_THREAD_POLICY,
            input_schema=_schema({"thread_id": {"type": "string", "description": "Gmail thread id returned by message or search results."}}, ["thread_id"]),
            output_schema=GMAIL_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="list_labels", description="List the account's labels.", data_policy=GMAIL_LIST_LABELS_POLICY, input_schema=_schema({}), output_schema=GMAIL_OUTPUT_SCHEMA),
        ActionSpec(id="list_drafts",
            description="List current drafts, each with its headers and body content.",
            data_policy=GMAIL_LIST_DRAFTS_POLICY,
            input_schema=_schema({"query": {"type": "string", "description": "Optional Gmail query used to filter drafts."}, "page_token": {"type": "string", "description": "Opaque nextPageToken from a prior list_drafts result."}, "include_spam_trash": {"type": "boolean", "description": "Include drafts associated with spam or trash (default false)."}}),
            output_schema=GMAIL_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="send_email",
            description="Queue approval to send an email (to, subject, body blocks).",
            data_policy=GMAIL_SEND_POLICY,
            input_schema=_schema({"to": {"type": "string", "description": "Recipient address or comma-separated recipient addresses."}, "subject": {"type": "string", "description": "Email subject."}, "blocks": BODY_BLOCK_SCHEMA}, ["to", "subject", "blocks"]),
            output_schema=GMAIL_OUTPUT_SCHEMA,
            approval="operator",
        ),
        ActionSpec(id="message_action",
            description="Queue approval to apply a message operation: archive, mark_read, mark_unread, star, unstar, add_labels, remove_labels, trash, untrash.",
            data_policy=GMAIL_MESSAGE_ACTION_POLICY,
            input_schema=_schema(
                {
                    "action": {"type": "string", "enum": list(GMAIL_MESSAGE_ACTION_TYPES_BY_TOOL_ACTION), "description": "One operation applied to every listed message."},
                    "message_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_GMAIL_MESSAGE_ACTION_IDS, "description": "Gmail message ids, not thread ids."},
                    "label_ids": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_GMAIL_MESSAGE_ACTION_LABEL_IDS, "description": "Required only for add_labels/remove_labels; obtain ids from list_labels."},
                },
                ["action", "message_ids"],
            ),
            output_schema=GMAIL_OUTPUT_SCHEMA,
            approval="operator",
        ),
        ActionSpec(id="label_action",
            description="Queue approval to create, update, or delete a label.",
            data_policy=GMAIL_LABEL_ACTION_POLICY,
            input_schema=_schema(
                {
                    "action": {"type": "string", "enum": ["create", "update", "delete"], "description": "Label operation to queue for approval."},
                    "label_id": {"type": "string", "description": "Existing user-label id from list_labels; required for update/delete."},
                    "name": {"type": "string", "description": "New label name for create, or replacement name for update."},
                    "background_color": {"type": "string", "enum": list(GMAIL_LABEL_BACKGROUND_COLORS), "description": "Named background color; set together with text_color."},
                    "text_color": {"type": "string", "enum": list(GMAIL_LABEL_TEXT_COLORS), "description": "Named text color; set together with background_color."},
                },
                ["action"],
            ),
            output_schema=GMAIL_OUTPUT_SCHEMA,
            approval="operator",
        ),
        ActionSpec(id="draft_action",
            description="Queue approval to create, update, send, or delete a draft.",
            data_policy=GMAIL_DRAFT_ACTION_POLICY,
            input_schema=_schema(
                {
                    "action": {"type": "string", "enum": ["create", "update", "send", "delete"], "description": "Draft operation to queue for approval."},
                    "draft_id": {"type": "string", "description": "Existing draft id from list_drafts; required for update/send/delete."},
                    "to": {"type": "string", "description": "Recipient address or comma-separated addresses for create/update."},
                    "subject": {"type": "string", "description": "Draft subject for create/update."},
                    "blocks": BODY_BLOCK_SCHEMA,
                },
                ["action"],
            ),
            output_schema=GMAIL_OUTPUT_SCHEMA,
            approval="operator",
        ),
    ),
    config=(
        ConfigRequirement(key="GOOGLE_OAUTH_CLIENT_ID", description="Google OAuth client id for the hosting deployment."),
        ConfigRequirement(key="GOOGLE_OAUTH_CLIENT_SECRET", description="Google OAuth client secret for the hosting deployment."),
    ),
    protections=(
        "OAuth tokens stay in the host credential store and are never exposed to the agent. The connection is bound to the Google account you approve.",
        "Reads run directly. Sending mail or changing messages, labels, and drafts waits for explicit operator approval.",
    ),
    setup_steps=(
        SetupStep(
            title="Create or select a Google Cloud project",
            description="Open Google Cloud Console, choose the project picker, and create a dedicated project if you do not already have one for TrustyClaw.",
            link_url="https://console.cloud.google.com/projectcreate",
            link_label="Open Google Cloud project creation",
        ),
        SetupStep(
            title="Enable the Gmail API",
            description="Open APIs and Services > Library, search for Gmail API, open it, and choose Enable.",
            link_url="https://console.cloud.google.com/apis/library/gmail.googleapis.com",
            link_label="Open the Gmail API library page",
        ),
        SetupStep(
            title="Configure the OAuth consent screen",
            description="Open Google Auth Platform > Branding and choose Get Started. Enter an app name such as TrustyClaw, a support email, External audience unless you use a Workspace-internal app, and your contact email. Then publish the app to Production; an app left in Testing needs your Google account under Audience > Test users and must be reconnected every week.",
            link_url="https://developers.google.com/workspace/guides/configure-oauth-consent",
            link_label="View Google's consent-screen guide",
            image_path="/guide-assets/google-auth-app-information.png",
            image_alt="Google Auth Platform app information form with App name and User support email fields.",
        ),
        SetupStep(
            title="Declare Gmail permissions",
            description="Under Google Auth Platform > Data Access, add openid, email, gmail.readonly, gmail.send, gmail.compose, gmail.labels, and gmail.modify. Google can show an unverified-app warning when you connect; that is expected for a personal app you created yourself. The screenshot locates the control; use this exact scope list rather than the example selection pictured.",
            link_url="https://developers.google.com/workspace/gmail/api/auth/scopes",
            link_label="Review the Gmail scope classifications",
            image_path="/guide-assets/google-auth-data-access.png",
            image_alt="Google Auth Platform Data Access screen for adding OAuth scopes manually.",
        ),
        SetupStep(
            title="Create a Web application OAuth client",
            description="Open Google Auth Platform > Clients, choose Create Client, and select Web application. Give the client a recognizable name. Leave Authorized JavaScript origins empty. Under Authorized redirect URIs, choose Add URI and enter this host's callback URI shown below. Then create the client and copy the client ID and client secret for the final step. The screenshot shows where the two URI sections appear.",
            link_url="https://developers.google.com/workspace/guides/create-credentials#web-application",
            link_label="View Google's web-client instructions",
            image_path="/guide-assets/google-auth-web-client.png",
            image_alt="Google Auth Platform Web application client form with Authorized JavaScript origins and Authorized redirect URIs sections.",
            show_callback=True,
        ),
        SetupStep(
            title="Configure TrustyClaw and connect",
            description="Expand Gmail in Internet Access and Tools and save the client ID and client secret you copied from the Web application client in the previous step under the two configuration keys below. Enable Gmail, then choose Connect and approve the requested Google permissions. Confirm that the row shows the expected connected email. A read can run immediately; a send should appear under Approvals before Google receives it. The same client can also serve Google Calendar.",
            show_config=True,
        ),
    ),
)


GMAIL_CREDENTIALS = GoogleCredentialStore(
    tool_id="gmail",
    scopes=GOOGLE_OAUTH_SCOPES,
    required_scopes=REQUIRED_GMAIL_SCOPES,
    reconnect_message=GMAIL_RECONNECT_MESSAGE,
)


def _single_line_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _optional_single_line_text(tool_input: JSONObject, key: str) -> str:
    value = tool_input.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ToolInputValidationError(f"Gmail search messages field {key} must be a string.")
    return _single_line_text(value)


def _gmail_search_time_epoch(tool_input: JSONObject, key: str) -> int:
    value = _optional_single_line_text(tool_input, key)
    if not value:
        return 0
    normalized_value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized_value)
    except ValueError as exc:
        raise ToolInputValidationError(f"Gmail search messages field {key} must be an ISO 8601 timestamp or YYYY-MM-DD date.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp())


def _gmail_search_query(tool_input: JSONObject) -> str:
    extra_fields = set(tool_input) - {"query", "start_time", "end_time"}
    if extra_fields:
        raise ToolInputValidationError("Gmail search messages only supports query, start_time, and end_time.")
    query_text = _optional_single_line_text(tool_input, "query")
    start_epoch = _gmail_search_time_epoch(tool_input, "start_time")
    end_epoch = _gmail_search_time_epoch(tool_input, "end_time")
    if start_epoch and end_epoch and start_epoch >= end_epoch:
        raise ToolInputValidationError("Gmail search messages start_time must be before end_time.")
    query_parts: list[str] = []
    if query_text:
        query_parts.append(query_text)
    if start_epoch:
        query_parts.append(f"after:{start_epoch}")
    if end_epoch:
        query_parts.append(f"before:{end_epoch}")
    if not query_parts:
        raise ToolInputValidationError("Gmail search messages requires query, start_time, or end_time.")
    return " ".join(query_parts)


def _draft_list_parameters(tool_input: JSONObject) -> JSONObject:
    extra_fields = set(tool_input) - {"query", "page_token", "include_spam_trash"}
    if extra_fields:
        raise ToolInputValidationError("Gmail draft list only supports query, page_token, and include_spam_trash.")
    parameters: JSONObject = {"maxResults": DEFAULT_DRAFT_PAGE_LIMIT}
    query = _single_line_text(tool_input.get("query"))
    if query:
        parameters["q"] = query
    page_token = _single_line_text(tool_input.get("page_token"))
    if page_token:
        parameters["pageToken"] = page_token
    include_spam_trash = tool_input.get("include_spam_trash")
    if isinstance(include_spam_trash, bool):
        parameters["includeSpamTrash"] = include_spam_trash
    elif include_spam_trash is not None:
        raise ToolInputValidationError("Gmail tool input include_spam_trash must be a boolean.")
    return parameters


def _single_line_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [line for line in (_single_line_text(item) for item in value) if line]


def _required_string_list(tool_input: JSONObject, field_name: str, *, maximum: int | None = None) -> list[str]:
    values = _single_line_text_list(tool_input.get(field_name))
    if not values:
        raise ToolInputValidationError(f"Gmail tool input requires {field_name}.")
    if maximum is not None and len(values) > maximum:
        raise ToolInputValidationError(f"Gmail tool input {field_name} must contain at most {maximum} items.")
    return values


def _optional_string_list(tool_input: JSONObject, field_name: str, *, maximum: int | None = None) -> list[str]:
    values = _single_line_text_list(tool_input.get(field_name))
    if maximum is not None and len(values) > maximum:
        raise ToolInputValidationError(f"Gmail tool input {field_name} must contain at most {maximum} items.")
    return values


def _structured_email_body_from_blocks(value: JSONValue | None) -> str:
    if not isinstance(value, list):
        return ""
    rendered_blocks: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            return ""
        block_type = item.get("type")
        if block_type == "paragraph":
            if set(item) != {"type", "text"}:
                return ""
            text = _single_line_text(item.get("text"))
            if text:
                rendered_blocks.append(text)
            continue
        if block_type != "line_group" or set(item) != {"type", "lines"}:
            return ""
        lines = _single_line_text_list(item.get("lines"))
        if lines:
            rendered_blocks.append("\n".join(lines))
    return "\n\n".join(rendered_blocks)


def _body_disclosure(body: str, max_bytes: int = 90) -> str:
    """A clipped preview plus the total length of an agent-composed body, so
    the visible approval text shows what the message says — not only who it
    goes to."""
    normalized = " ".join(str(body).split())
    return f"body ({len(normalized)} chars): \"{clip_text(normalized, max_bytes)}\""


def _gmail_send_proposal(tool_input: JSONObject) -> JSONObject:
    extra_fields = set(tool_input) - {"to", "subject", "blocks"}
    if extra_fields:
        raise ToolInputValidationError("Gmail send tool input only supports to, subject, and blocks.")
    draft: JSONObject = {
        "to": string_value(tool_input, ("to",)),
        "subject": string_value(tool_input, ("subject",)),
        "body": _structured_email_body_from_blocks(tool_input.get("blocks")),
    }
    if not draft["to"] or not draft["subject"] or not draft["body"]:
        raise ToolInputValidationError("Gmail send requires tool_input.to, tool_input.subject, and tool_input.blocks.")
    # Clip every agent-supplied field so nothing can push the summary past
    # the host API's 500-byte limit, and disclose the composed body — the
    # operator approves content, not just an address line.
    summary = (
        f"Send Gmail message to {clip_text(str(draft['to']), 90)}"
        f" with subject \"{clip_text(str(draft['subject']), 60)}\"; {_body_disclosure(str(draft['body']))}."
    )
    return {"summary": summary, "draft": draft}


def _message_action_proposal(tool_input: JSONObject) -> JSONObject:
    extra_fields = set(tool_input) - {"action", "message_ids", "label_ids"}
    if extra_fields:
        raise ToolInputValidationError("Gmail message action only supports action, message_ids, and label_ids.")
    action = string_value(tool_input, ("action",))
    message_ids = _required_string_list(tool_input, "message_ids", maximum=MAX_GMAIL_MESSAGE_ACTION_IDS)
    label_ids = _optional_string_list(tool_input, "label_ids", maximum=MAX_GMAIL_MESSAGE_ACTION_LABEL_IDS)
    if label_ids and action not in {"add_labels", "remove_labels"}:
        raise ToolInputValidationError("Gmail label_ids are only supported for add_labels and remove_labels.")
    add_labels: list[str] = []
    remove_labels: list[str] = []
    request: JSONObject | None = None
    if action == "archive":
        remove_labels = ["INBOX"]
    elif action == "mark_read":
        remove_labels = ["UNREAD"]
    elif action == "mark_unread":
        add_labels = ["UNREAD"]
    elif action == "star":
        add_labels = ["STARRED"]
    elif action == "unstar":
        remove_labels = ["STARRED"]
    elif action == "add_labels":
        if not label_ids:
            raise ToolInputValidationError("Gmail add_labels requires label_ids.")
        add_labels = label_ids
    elif action == "remove_labels":
        if not label_ids:
            raise ToolInputValidationError("Gmail remove_labels requires label_ids.")
        remove_labels = label_ids
    elif action in {"trash", "untrash"}:
        if len(message_ids) != 1:
            raise ToolInputValidationError("Gmail trash and untrash actions support one message per approval.")
        operation = "users.messages.trash" if action == "trash" else "users.messages.untrash"
        request = gmail_operation_request(operation, {"id": message_ids[0]})
    else:
        raise ToolInputValidationError("Unsupported Gmail message action.")
    if request is None:
        body: JSONObject = {"ids": cast(list[JSONValue], message_ids)}
        if add_labels:
            body["addLabelIds"] = cast(list[JSONValue], add_labels)
        if remove_labels:
            body["removeLabelIds"] = cast(list[JSONValue], remove_labels)
        request = gmail_operation_request("users.messages.batchModify", {}, body)
    action_label = action.replace("_", " ")
    count_label = f"{len(message_ids)} message{'s' if len(message_ids) != 1 else ''}"
    proposal: JSONObject = {
        "summary": f"{action_label.title()} {count_label}.",
        "messageIds": cast(list[JSONValue], message_ids),
        "request": request,
    }
    if label_ids:
        proposal["labelIds"] = cast(list[JSONValue], label_ids)
    return proposal


def _label_color(tool_input: JSONObject) -> JSONObject:
    background_color = string_value(tool_input, ("background_color",))
    text_color = string_value(tool_input, ("text_color",))
    if not background_color and not text_color:
        return {}
    if not background_color or not text_color:
        raise ToolInputValidationError("Gmail label color requires both background_color and text_color.")
    background_hex = GMAIL_LABEL_BACKGROUND_COLORS.get(background_color)
    if background_hex is None:
        raise ToolInputValidationError("Unsupported Gmail label background_color.")
    text_hex = GMAIL_LABEL_TEXT_COLORS.get(text_color)
    if text_hex is None:
        raise ToolInputValidationError("Unsupported Gmail label text_color.")
    return {"backgroundColor": background_hex, "textColor": text_hex}


def _label_action_proposal(tool_input: JSONObject) -> JSONObject:
    extra_fields = set(tool_input) - {"action", "label_id", "name", "background_color", "text_color"}
    if extra_fields:
        raise ToolInputValidationError("Gmail label action only supports action, label_id, name, background_color, and text_color.")
    action = string_value(tool_input, ("action",))
    label_id = string_value(tool_input, ("label_id",))
    name = string_value(tool_input, ("name",))
    color = _label_color(tool_input)
    body: JSONObject = {}
    if name:
        body["name"] = name
    if color:
        body["color"] = color
    if action == "create":
        if label_id:
            raise ToolInputValidationError("Gmail label create does not support label_id.")
        if not name:
            raise ToolInputValidationError("Gmail label create requires name.")
        request = gmail_operation_request("users.labels.create", {}, body)
        summary = f"Create Gmail label \"{clip_text(name, 100)}\"."
    elif action == "update":
        if not label_id:
            raise ToolInputValidationError("Gmail label update requires label_id.")
        if not body:
            raise ToolInputValidationError("Gmail label update requires at least one changed field.")
        request = gmail_operation_request("users.labels.patch", {"id": label_id}, body)
        summary = f"Update Gmail label {label_id}."
    elif action == "delete":
        if not label_id:
            raise ToolInputValidationError("Gmail label delete requires label_id.")
        request = gmail_operation_request("users.labels.delete", {"id": label_id})
        summary = f"Delete Gmail label {label_id}."
    else:
        raise ToolInputValidationError("Unsupported Gmail label action.")
    proposal: JSONObject = {"summary": summary, "request": request}
    if label_id:
        proposal["labelId"] = label_id
    if name and action != "delete":
        proposal["name"] = name
    if color and action != "delete":
        proposal["color"] = color
    return proposal


def _validate_draft_action_shape(tool_input: JSONObject, action: str, draft_id: str) -> None:
    if action == "create" and draft_id:
        raise ToolInputValidationError("Gmail draft create does not support draft_id.")
    if action in {"send", "delete"} and any(field_name in tool_input for field_name in ("to", "subject", "blocks")):
        raise ToolInputValidationError("Gmail draft send and delete only support action and draft_id.")


def _draft_has_attachments(draft: JSONObject) -> bool:
    return _draft_attachment_count(draft) > 0


def _draft_attachment_count(draft: JSONObject) -> int:
    attachment_count = draft.get("attachmentsOriginalCount")
    if isinstance(attachment_count, int) and not isinstance(attachment_count, bool) and attachment_count > 0:
        return attachment_count
    attachments = draft.get("attachments")
    return len(attachments) if isinstance(attachments, list) else 0


def _draft_attachment_names(draft: JSONObject, *, max_field_bytes: int, max_names: int = 3) -> list[str]:
    """Clipped filename (and size) for the draft's captured attachments, so a
    send/delete approval names which files ride along instead of only a count."""
    attachments = draft.get("attachments")
    if not isinstance(attachments, list):
        return []
    names: list[str] = []
    for item in attachments[:max_names]:
        if not isinstance(item, dict):
            continue
        typed = cast(JSONObject, item)
        label = clip_text(string_value(typed, ("filename",)) or "(unnamed attachment)", max_field_bytes)
        size = typed.get("size")
        if isinstance(size, int) and not isinstance(size, bool):
            label += f", {size} bytes"
        names.append(label)
    return names


def _draft_attachment_disclosure(draft: JSONObject, *, include_names: bool = True, max_field_bytes: int = 40) -> str:
    """The attachment part of an approval summary: the count plus (best-effort)
    up to three named files (with a +N more marker when the count exceeds what is
    named). The count is mandatory; names are dropped by the caller when they
    would push the summary past the host API's 500-byte limit."""
    count = _draft_attachment_count(draft)
    if not count:
        return ""
    disclosure = f"{count} attachment{'s' if count != 1 else ''}"
    names = _draft_attachment_names(draft, max_field_bytes=max_field_bytes) if include_names else []
    if names:
        extra = count - len(names)
        listed = ", ".join(names) + (f", +{extra} more" if extra > 0 else "")
        disclosure += f" ({listed})"
    return disclosure


def _draft_recipient_disclosures(draft: JSONObject, *, max_field_bytes: int) -> list[str]:
    disclosures = [f"to {clip_text(string_value(draft, ('to',)) or 'recipient', max_field_bytes)}"]
    for field in ("cc", "bcc"):
        value = string_value(draft, (field,))
        if value:
            disclosures.append(f"{field} {clip_text(value, max_field_bytes)}")
    return disclosures


def _draft_routing_disclosures(draft: JSONObject, *, max_field_bytes: int) -> list[str]:
    disclosures: list[str] = []
    for field, label in (
        ("from", "from"),
        ("replyTo", "reply-to"),
        ("inReplyTo", "in-reply-to"),
        ("references", "refs"),
        ("threadId", "thread"),
    ):
        value = string_value(draft, (field,))
        if value:
            disclosures.append(f"{label} {clip_text(value, max_field_bytes)}")
    return disclosures


def _draft_delete_summary(draft: JSONObject) -> str:
    """Approval summary for deleting an existing draft. A draft is identified by
    more than To/Subject (two drafts can share both), so this discloses the
    attachment count and a body preview to make the delete target
    distinguishable. Attachment names are best-effort and dropped if the summary
    would exceed the 500-byte cap."""
    for include_names in (True, False):
        to_address = clip_text(string_value(draft, ("to",)) or "recipient", 90)
        subject = clip_text(string_value(draft, ("subject",)) or "(no subject)", 60)
        summary = f"Delete Gmail draft to {to_address} with subject \"{subject}\""
        attachments = _draft_attachment_disclosure(draft, include_names=include_names)
        if attachments:
            summary += f" and {attachments}"
        body = string_value(draft, ("body",))
        if body:
            summary += f"; {_body_disclosure(body, 48)}"
            # A previewed body can understate what the stored draft holds; flag a
            # truncated preview so two same-recipient drafts stay distinguishable.
            if draft.get("bodyTruncated"):
                original = draft.get("bodyOriginalLength")
                summary += f" (full body {original} chars)" if isinstance(original, int) else " (full body)"
        else:
            # No renderable body preview (for example HTML-only or no snippet):
            # disclose the note that explains what content is not shown.
            body_note = string_value(draft, ("bodyNote",))
            summary += f"; body preview unavailable: {clip_text(body_note, 60)}" if body_note else "; no body preview"
        summary += "."
        if len(summary.encode("utf-8")) <= GMAIL_SUMMARY_MAX_BYTES:
            return summary
    return summary


def _draft_send_summary(draft: JSONObject) -> str:
    """Approval summary for sending an existing draft. The mandatory disclosures
    (recipients, attachment count, routing, body caveats/length) are clipped
    per field so they always fit; attachment names are best-effort, so if naming
    them would exceed the 500-byte cap the summary falls back to the count-only
    form, which per-field clipping keeps in budget."""
    summary = _draft_send_summary_impl(draft, include_attachment_names=True)
    if len(summary.encode("utf-8")) <= GMAIL_SUMMARY_MAX_BYTES:
        return summary
    return _draft_send_summary_impl(draft, include_attachment_names=False)


def _draft_send_summary_impl(draft: JSONObject, *, include_attachment_names: bool) -> str:
    recipients = _draft_recipient_disclosures(draft, max_field_bytes=52)
    subject = clip_text(string_value(draft, ("subject",)) or "(no subject)", 48)
    summary = f"Send Gmail draft {', '.join(recipients)} with subject \"{subject}\""
    attachments = _draft_attachment_disclosure(draft, include_names=include_attachment_names)
    if attachments:
        summary += f" and {attachments}"
    routing = _draft_routing_disclosures(draft, max_field_bytes=12)
    if routing:
        summary += f"; routing {', '.join(routing)}"
    body = string_value(draft, ("body",))
    body_note = string_value(draft, ("bodyNote",))
    if not body:
        if body_note:
            note_prefix = "; body preview unavailable"
            fixed = summary + note_prefix + ": " + "."
            remaining = GMAIL_SUMMARY_MAX_BYTES - len(fixed.encode("utf-8"))
            if remaining >= 8:
                summary += f"{note_prefix}: {clip_text(body_note, remaining)}"
            else:
                summary += note_prefix
        return summary + "."
    # The draft is sent as-is, so a body preview can understate what is sent:
    # it may be truncated to the first bodyLimit chars of a longer body, and a
    # note can flag an unrendered HTML alternative. These caveats are mandatory
    # (an operator must not approve a benign-looking preview while the full body
    # or HTML part goes out) and short, so they always fit. The preview text
    # itself is the one flexible part: it takes whatever budget remains under
    # the host summary cap after every recipient, routing, attachment, and
    # caveat disclosure, so a draft is never blocked from sending by the very
    # fields being disclosed.
    caveats = ""
    if draft.get("bodyTruncated"):
        original = draft.get("bodyOriginalLength")
        caveats += f" (full body {original} chars sent)" if isinstance(original, int) else " (full body sent)"
    if body_note:
        caveats += f"; {clip_text(body_note, 40)}"
    normalized = " ".join(str(body).split())
    length_note = f"; body ({len(normalized)} chars)"
    fixed = summary + length_note + ': ""' + caveats + "."
    remaining = GMAIL_SUMMARY_MAX_BYTES - len(fixed.encode("utf-8"))
    if remaining >= 8:
        return summary + length_note + f': "{clip_text(normalized, remaining)}"' + caveats + "."
    return summary + length_note + caveats + "."


def _draft_action_proposal(tool_input: JSONObject, existing_draft: JSONObject | None = None) -> JSONObject:
    extra_fields = set(tool_input) - {"action", "draft_id", "to", "subject", "blocks"}
    if extra_fields:
        raise ToolInputValidationError("Gmail draft action only supports action, draft_id, to, subject, and blocks.")
    action = string_value(tool_input, ("action",))
    draft_id = string_value(tool_input, ("draft_id",))
    _validate_draft_action_shape(tool_input, action, draft_id)
    proposal: JSONObject = {}
    if action in {"create", "update"}:
        send_proposal = _gmail_send_proposal({"to": tool_input.get("to"), "subject": tool_input.get("subject"), "blocks": tool_input.get("blocks")})
        draft = json_object(send_proposal.get("draft"))
        if action == "update" and not draft_id:
            raise ToolInputValidationError("Gmail draft update requires draft_id.")
        proposal.update({
            "draft": draft,
            "summary": (
                f"{action.title()} Gmail draft to {clip_text(str(draft['to']), 90)}"
                f" with subject \"{clip_text(str(draft['subject']), 60)}\"; {_body_disclosure(str(draft['body']))}."
            ),
        })
        if action == "update":
            if existing_draft is None:
                raise ToolInputValidationError("Gmail draft update requires current draft contents.")
            if _draft_has_attachments(existing_draft):
                raise ToolInputValidationError(GMAIL_DRAFT_ATTACHMENT_UNSUPPORTED_MESSAGE)
            current_subject = clip_text(string_value(existing_draft, ("subject",)) or "(no subject)", 24)
            current_to = clip_text(string_value(existing_draft, ("to",)) or "recipient", 24)
            proposal["currentDraft"] = existing_draft
            # Every field is clipped — including the agent-supplied draft id —
            # so the worst case (all fields maxed, both hidden recipients, a
            # long body) stays under the host API's 500-byte summary limit
            # while keeping every mandatory disclosure visible.
            summary = (
                f"Update Gmail draft {clip_text(draft_id, 24)} from {current_to} / \"{current_subject}\""
                f" to {clip_text(str(draft['to']), 24)} / \"{clip_text(str(draft['subject']), 24)}\""
            )
            # The rewrite preserves the draft's existing Cc/Bcc and routing
            # headers, so an update approval must disclose them too.
            preserved = [
                f"{field} {clip_text(string_value(existing_draft, (field,)), 24)}"
                for field in ("cc", "bcc")
                if string_value(existing_draft, (field,))
            ]
            preserved.extend(_draft_routing_disclosures(existing_draft, max_field_bytes=24))
            if preserved:
                summary += f" (keeps {', '.join(preserved)})"
            proposal["summary"] = f"{summary}; new {_body_disclosure(str(draft['body']), 48)}."
    elif action in {"send", "delete"}:
        if not draft_id:
            raise ToolInputValidationError(f"Gmail draft {action} requires draft_id.")
        if existing_draft is None:
            raise ToolInputValidationError(f"Gmail draft {action} requires current draft contents.")
        if action == "send":
            summary = _draft_send_summary(existing_draft)
        else:
            summary = _draft_delete_summary(existing_draft)
        proposal.update({"currentDraft": existing_draft, "summary": summary})
    else:
        raise ToolInputValidationError("Unsupported Gmail draft action.")
    if draft_id:
        proposal["draftId"] = draft_id
    return proposal


def _message_action_summary(action: str, proposal: JSONObject) -> str:
    """Approval summary naming the target messages (subject and sender from
    the proposal-time previews) and any label names, so the visible decision
    text identifies exactly what changes without expanding the raw payload.
    Per-field clipping keeps it within the host API's 500-byte limit."""
    messages = [message for message in _json_object_list(proposal.get("messages"))]
    described = [
        f"\"{clip_text(string_value(message, ('subject',)) or '(no subject)', 35)}\""
        f" from {clip_text(string_value(message, ('from',)) or 'unknown sender', 35)}"
        for message in messages[:3]
    ]
    if len(messages) > 3:
        described.append(f"and {len(messages) - 3} more")
    action_label = action.replace("_", " ").title()
    count_label = f"{len(messages)} message{'s' if len(messages) != 1 else ''}"
    summary = f"{action_label} {count_label}"
    if described:
        summary += f": {'; '.join(described)}"
    labels = _json_object_list(proposal.get("labels"))
    if labels:
        names = ", ".join(
            clip_text(string_value(label, ("name",)) or string_value(label, ("id",)), 30)
            for label in labels[:4]
        )
        if len(labels) > 4:
            names += f", and {len(labels) - 4} more"
        summary += f" (labels: {names})"
    return summary + "."


def _label_action_summary(tool_action: str, proposal: JSONObject) -> str:
    """Approval summary naming the label being updated or deleted (create
    already names the new label)."""
    label = json_object(proposal.get("label"))
    label_id = string_value(proposal, ("labelId",))
    name = clip_text(string_value(label, ("name",)) or label_id, 60)
    if tool_action == "delete":
        return f"Delete Gmail label \"{name}\" ({label_id})."
    changes = []
    new_name = string_value(proposal, ("name",))
    if new_name:
        changes.append(f"name → {clip_text(new_name, 60)}")
    color = json_object(proposal.get("color"))
    if color:
        changes.append(f"color → {string_value(color, ('backgroundColor',))}/{string_value(color, ('textColor',))}")
    return f"Update Gmail label \"{name}\" ({label_id}): {', '.join(changes)}."


def _json_object_list(value: JSONValue | None) -> list[JSONObject]:
    if not isinstance(value, list):
        return []
    return [cast(JSONObject, item) for item in value if isinstance(item, dict)]


def _is_http_status(exc: Exception, status_code: int) -> bool:
    cause = exc.__cause__
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == status_code
    return isinstance(cause, urllib.error.HTTPError) and cause.code == status_code


def _raise_stale_approval_on_not_found(exc: Exception, message: str) -> NoReturn:
    """A draft or label deleted between approval and execution surfaces as a 404
    on the verification fetch; report it as the approved object having changed
    (queue a new approval) instead of a generic Gmail API failure."""
    if _is_http_status(exc, 404):
        raise RuntimeError(message) from exc
    raise exc


def _verify_message_action_labels(access_token: str, proposal: JSONObject) -> None:
    """The label names shown at approval time must still be accurate when the
    approved action executes: a label renamed (or deleted) in between means the
    operator approved applying a different label than the ids now point to. A
    single labels.list call covers every approved label — the schema allows up to
    50 — so verification stays well within the operator approval timeout instead
    of making one blocking labels.get per label."""
    approved_labels = [label for label in _json_object_list(proposal.get("labels")) if string_value(label, ("id",))]
    if not approved_labels:
        return
    current_labels = gmail_label_summaries(access_token, [string_value(label, ("id",)) for label in approved_labels])
    for approved, current in zip(approved_labels, current_labels):
        if string_value(current, ("name",)) != string_value(approved, ("name",)):
            raise RuntimeError("Gmail label changed after approval. Please queue a new approval.")


def _normalize_email_body_for_mime(body: str) -> str:
    return body.replace("\r\n", "\n").replace("\r", "\n").strip()


def _gmail_html_body_from_text(body: str) -> str:
    html_blocks: list[str] = []
    for block in re.split(r"\n\s*\n", body):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        escaped_lines = [html.escape(line, quote=True) for line in lines]
        html_blocks.append("<br>".join(escaped_lines))
    return f'<div dir="ltr">{"<br><br>".join(html_blocks)}<br></div>' if html_blocks else '<div dir="ltr"><br></div>'


def _gmail_raw_message_resource(google_email: str, proposal: JSONObject, *, include_html_alternative: bool = True, thread_id: str = "") -> JSONObject:
    recipient = string_value(proposal, ("to",))
    subject = string_value(proposal, ("subject",))
    body = string_value(proposal, ("body",))
    if not recipient or not subject or not body:
        raise RuntimeError("Gmail send approval is missing to, subject, or body.")
    message = EmailMessage()
    message["From"] = string_value(proposal, ("from",)) or google_email
    message["To"] = recipient
    for field, header in (("cc", "Cc"), ("bcc", "Bcc"), ("inReplyTo", "In-Reply-To"), ("references", "References"), ("replyTo", "Reply-To")):
        value = string_value(proposal, (field,))
        if value:
            message[header] = value
    message["Subject"] = subject
    plain_body = _normalize_email_body_for_mime(body)
    message.set_content(plain_body, cte="base64")
    if include_html_alternative:
        message.add_alternative(_gmail_html_body_from_text(plain_body), subtype="html", cte="base64")
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
    resource: JSONObject = {"raw": raw}
    if thread_id:
        resource["threadId"] = thread_id
    return resource


def _execute_send_approval(access_token: str, google_email: str, proposal: JSONObject) -> JSONObject:
    draft = json_object(proposal.get("draft"))
    if not draft:
        raise RuntimeError("Gmail approval payload is invalid.")
    request = gmail_operation_request("users.messages.send", {}, _gmail_raw_message_resource(google_email, draft))
    return {"status": "success_executed", "response": execute_gmail_api_request(access_token, request), "message": "Gmail message sent after approval."}


def _verify_draft_matches_approval(access_token: str, draft_id: str, approved_draft: JSONObject) -> None:
    approved_message_id = string_value(approved_draft, ("messageId",))
    if not approved_message_id:
        return
    try:
        current_draft = gmail_draft_preview(access_token, draft_id)
    except Exception as exc:
        _raise_stale_approval_on_not_found(exc, "Gmail draft changed after approval. Please queue a new approval.")
    if string_value(current_draft, ("messageId",)) != approved_message_id:
        raise RuntimeError("Gmail draft changed after approval. Please queue a new approval.")


def _execute_draft_approval(action_type: str, access_token: str, google_email: str, proposal: JSONObject) -> JSONObject:
    draft_id = string_value(proposal, ("draftId",))
    if action_type in {GMAIL_DRAFT_ACTION_TYPES_BY_TOOL_ACTION["create"], GMAIL_DRAFT_ACTION_TYPES_BY_TOOL_ACTION["update"]}:
        draft = json_object(proposal.get("draft"))
        current_draft = json_object(proposal.get("currentDraft"))
        if action_type == GMAIL_DRAFT_ACTION_TYPES_BY_TOOL_ACTION["update"] and current_draft:
            if _draft_has_attachments(current_draft):
                raise RuntimeError(GMAIL_DRAFT_ATTACHMENT_UNSUPPORTED_MESSAGE)
            draft = {**draft, "bcc": string_value(current_draft, ("bcc",)), "cc": string_value(current_draft, ("cc",)), "from": string_value(current_draft, ("from",)), "inReplyTo": string_value(current_draft, ("inReplyTo",)), "references": string_value(current_draft, ("references",)), "replyTo": string_value(current_draft, ("replyTo",))}
        body: JSONObject = {"message": _gmail_raw_message_resource(google_email, draft, thread_id=string_value(current_draft, ("threadId",)))}
        operation = "users.drafts.create"
        parameters: JSONObject = {}
        verb = "Created"
        if action_type == GMAIL_DRAFT_ACTION_TYPES_BY_TOOL_ACTION["update"]:
            _verify_draft_matches_approval(access_token, draft_id, current_draft)
            operation = "users.drafts.update"
            parameters = {"id": draft_id}
            body["id"] = draft_id
            verb = "Updated"
        response = execute_gmail_api_request(access_token, gmail_operation_request(operation, parameters, body))
        # Surface the resulting draft id in the user-visible message so the agent
        # can reference the object it just created/updated.
        new_draft_id = string_value(response, ("id",)) or draft_id
        return {"status": "success_executed", "response": response, "message": f"{verb} Gmail draft {new_draft_id}."}
    if action_type == GMAIL_DRAFT_ACTION_TYPES_BY_TOOL_ACTION["send"]:
        _verify_draft_matches_approval(access_token, draft_id, json_object(proposal.get("currentDraft")))
        response = execute_gmail_api_request(access_token, gmail_operation_request("users.drafts.send", {}, {"id": draft_id}))
        sent_id = string_value(response, ("id",))
        return {"status": "success_executed", "response": response, "message": f"Sent Gmail message {sent_id}." if sent_id else "Sent the Gmail draft."}
    if action_type == GMAIL_DRAFT_ACTION_TYPES_BY_TOOL_ACTION["delete"]:
        _verify_draft_matches_approval(access_token, draft_id, json_object(proposal.get("currentDraft")))
        return {"status": "success_executed", "response": execute_gmail_api_request(access_token, gmail_operation_request("users.drafts.delete", {"id": draft_id})), "message": f"Deleted Gmail draft {draft_id}."}
    raise RuntimeError("Gmail draft approval payload is invalid.")


def _verify_label_matches_approval(access_token: str, proposal: JSONObject) -> None:
    approved_label = json_object(proposal.get("label"))
    label_id = string_value(proposal, ("labelId",))
    if not approved_label or not label_id:
        return
    try:
        current_label = gmail_label_preview(access_token, label_id)
    except Exception as exc:
        _raise_stale_approval_on_not_found(exc, "Gmail label changed after approval. Please queue a new approval.")
    for field in ("name", "type"):
        if string_value(current_label, (field,)) != string_value(approved_label, (field,)):
            raise RuntimeError("Gmail label changed after approval. Please queue a new approval.")
    if json_object(approved_label.get("color")) != json_object(current_label.get("color")):
        raise RuntimeError("Gmail label changed after approval. Please queue a new approval.")


def _execute_request_approval(access_token: str, proposal: JSONObject) -> JSONObject:
    request = proposal.get("request")
    if not isinstance(request, dict):
        raise RuntimeError("Gmail approval payload is invalid.")
    return {"status": "success_executed", "response": execute_gmail_api_request(access_token, cast(JSONObject, request)), "message": "Gmail action completed after approval."}


class GmailTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> CredentialFlow:
        # GoogleCredentialStore implements the CredentialFlow protocol directly.
        return GMAIL_CREDENTIALS

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            spec = MANIFEST.action(action)
            if spec is None:
                return ActionFailed("Unsupported Gmail action.")
            access_token = GMAIL_CREDENTIALS.access_token(api)
            if action in GMAIL_DIRECT_ACTIONS:
                result = self._execute_read(action, tool_input, access_token)
                return ActionExecuted(result)
            proposal, action_type = self._proposal(action, tool_input, api, access_token)
            payload = self._approval_payload(action, action_type, proposal, api, access_token)
            approval = api.approvals.request(action_id=action, summary=string_value(proposal, ("summary",)) or f"Review Gmail {action}.", payload=payload)
            return ActionPendingApproval(approval.approval_id, approval.summary)
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except IntegrationReconnectRequired as exc:
            return ActionFailed(str(exc), reconnect_required=True)
        except Exception as exc:
            return ActionFailed(str(exc) or "Gmail tool request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        try:
            # The host hands a loaded record: approved, and this tool's own.
            payload = approval.payload
            action_type = string_value(payload, ("action_type",))
            proposal = json_object(payload.get("proposal"))
            access_token = GMAIL_CREDENTIALS.access_token(api)
            current_account = GMAIL_CREDENTIALS.refresh_identity(api, access_token)
            approved_account = json_object(payload.get("gmail_account"))
            # Fail closed on a missing binding, exactly like a mismatched one.
            if approved_account.get("sub") != current_account["id"]:
                return ActionFailed("Gmail account changed after approval. Please queue a new approval.")
            result = self._execute_approval(action_type, access_token, current_account["label"], proposal)
            return ApprovalExecuted(string_value(result, ("message",)) or "Gmail action completed after approval.")
        except IntegrationReconnectRequired as exc:
            return ActionFailed(str(exc), reconnect_required=True)
        except Exception as exc:
            return ActionFailed(str(exc) or "Gmail action failed after approval.")

    def _execute_read(self, action: str, tool_input: JSONObject, access_token: str) -> JSONObject:
        if action == "search_messages":
            query_text = _gmail_search_query(tool_input)
            return {"status": "success_executed", "message": "Gmail messages searched.", "query": query_text, "messages": cast(list[JSONValue], gmail_search_messages(access_token, query_text))}
        if action == "read_message":
            if set(tool_input) - {"message_id"}:
                raise ToolInputValidationError("Gmail read message only supports message_id.")
            message_id = string_value(tool_input, ("message_id",))
            if not message_id:
                raise ToolInputValidationError("Gmail read message requires message_id.")
            response = execute_gmail_api_request(access_token, gmail_operation_request("users.messages.get", {"id": message_id, "format": "full"}))
            return {"status": "success_executed", "message": "Gmail message loaded.", "gmailMessage": gmail_readable_message(response)}
        if action == "read_thread":
            thread_id = string_value(tool_input, ("thread_id",))
            if not thread_id:
                raise ToolInputValidationError("Gmail read thread requires thread_id.")
            response = execute_gmail_api_request(access_token, gmail_operation_request("users.threads.get", {"id": thread_id, "format": "metadata", "metadataHeaders": ["From", "To", "Cc", "Bcc", "Subject", "Date"]}))
            raw_messages = response.get("messages")
            messages = raw_messages if isinstance(raw_messages, list) else []
            thread: JSONObject = {
                "id": string_value(response, ("id",))[:500],
                "historyId": string_value(response, ("historyId",))[:500],
                "messages": cast(list[JSONValue], [
                    gmail_message_index_entry(cast(JSONObject, message))
                    for message in messages[:GMAIL_THREAD_MESSAGE_INDEX_MAX_RESULTS]
                    if isinstance(message, dict)
                ]),
            }
            if len(messages) > GMAIL_THREAD_MESSAGE_INDEX_MAX_RESULTS:
                thread["messageIndexTruncated"] = True
                thread["messageIndexLimit"] = GMAIL_THREAD_MESSAGE_INDEX_MAX_RESULTS
                thread["messageIndexOriginalCount"] = len(messages)
                thread["messageIndexOmittedCount"] = len(messages) - GMAIL_THREAD_MESSAGE_INDEX_MAX_RESULTS
            return {"status": "success_executed", "message": "Gmail thread message list loaded.", "thread": thread}
        if action == "list_labels":
            response = execute_gmail_api_request(access_token, gmail_operation_request("users.labels.list", {}))
            return {"status": "success_executed", "message": "Gmail labels loaded.", "labels": gmail_label_list_summary(response)}
        if action == "list_drafts":
            parameters = _draft_list_parameters(tool_input)
            response = execute_gmail_api_request(access_token, gmail_operation_request("users.drafts.list", parameters))
            raw_drafts = response.get("drafts")
            drafts = raw_drafts if isinstance(raw_drafts, list) else []
            normalized: JSONObject = {
                "drafts": cast(list[JSONValue], [
                    gmail_draft_preview(access_token, string_value(cast(JSONObject, draft), ("id",)))
                    for draft in drafts[:DEFAULT_DRAFT_PAGE_LIMIT]
                    if isinstance(draft, dict) and string_value(cast(JSONObject, draft), ("id",))
                ])
            }
            next_page_token = response.get("nextPageToken")
            if isinstance(next_page_token, str):
                normalized["nextPageToken"] = next_page_token[:2_048]
            return {"status": "success_executed", "message": "Gmail drafts loaded.", "drafts": normalized}
        raise ToolInputValidationError("Unsupported Gmail read action.")

    def _proposal(self, action: str, tool_input: JSONObject, api: HostAPI, access_token: str) -> tuple[JSONObject, str]:
        del api
        if action == "send_email":
            return _gmail_send_proposal(tool_input), GMAIL_SEND_ACTION_TYPE
        if action == "message_action":
            proposal = _message_action_proposal(tool_input)
            tool_action = string_value(tool_input, ("action",))
            action_type = GMAIL_MESSAGE_ACTION_TYPES_BY_TOOL_ACTION.get(tool_action)
            if action_type is None:
                raise ToolInputValidationError("Unsupported Gmail message action.")
            message_ids = proposal.get("messageIds")
            if isinstance(message_ids, list):
                ids = [message_id for message_id in message_ids if isinstance(message_id, str)]
                proposal["messages"] = cast(list[JSONValue], gmail_message_action_summaries(access_token, ids))
            label_ids = proposal.get("labelIds")
            if isinstance(label_ids, list):
                ids = [label_id for label_id in label_ids if isinstance(label_id, str)]
                proposal["labels"] = cast(list[JSONValue], gmail_label_summaries(access_token, ids))
            proposal["summary"] = _message_action_summary(tool_action, proposal)
            return proposal, action_type
        if action == "label_action":
            proposal = _label_action_proposal(tool_input)
            tool_action = string_value(tool_input, ("action",))
            action_type = GMAIL_LABEL_ACTION_TYPES_BY_TOOL_ACTION.get(tool_action)
            if action_type is None:
                raise ToolInputValidationError("Unsupported Gmail label action.")
            label_id = proposal.get("labelId")
            if isinstance(label_id, str) and label_id:
                proposal["label"] = gmail_label_preview(access_token, label_id)
                proposal["summary"] = _label_action_summary(tool_action, proposal)
            return proposal, action_type
        if action == "draft_action":
            tool_action = string_value(tool_input, ("action",))
            draft_id = string_value(tool_input, ("draft_id",))
            existing_draft: JSONObject | None = None
            _validate_draft_action_shape(tool_input, tool_action, draft_id)
            if tool_action in {"update", "send", "delete"}:
                if not draft_id:
                    raise ToolInputValidationError(f"Gmail draft {tool_action} requires draft_id.")
                existing_draft = gmail_draft_preview(access_token, draft_id)
            proposal = _draft_action_proposal(tool_input, existing_draft)
            action_type = GMAIL_DRAFT_ACTION_TYPES_BY_TOOL_ACTION.get(tool_action)
            if action_type is None:
                raise ToolInputValidationError("Unsupported Gmail draft action.")
            return proposal, action_type
        raise ToolInputValidationError("Unsupported Gmail write action.")

    def _approval_payload(self, action: str, action_type: str, proposal: JSONObject, api: HostAPI, access_token: str) -> JSONObject:
        account = GMAIL_CREDENTIALS.refresh_identity(api, access_token)
        return {
            "action": action,
            "action_type": action_type,
            "gmail_account": {"email": account["label"], "sub": account["id"]},
            "proposal": proposal,
            "tool_id": MANIFEST.tool_id,
        }

    def _execute_approval(self, action_type: str, access_token: str, google_email: str, proposal: JSONObject) -> JSONObject:
        if action_type == GMAIL_SEND_ACTION_TYPE:
            return _execute_send_approval(access_token, google_email, proposal)
        if action_type in GMAIL_DRAFT_ACTION_TYPES:
            return _execute_draft_approval(action_type, access_token, google_email, proposal)
        if action_type in GMAIL_LABEL_ACTION_TYPES:
            _verify_label_matches_approval(access_token, proposal)
            return _execute_request_approval(access_token, proposal)
        if action_type in GMAIL_MESSAGE_ACTION_TYPES:
            _verify_message_action_labels(access_token, proposal)
            return _execute_request_approval(access_token, proposal)
        raise RuntimeError("Gmail approval payload is invalid.")


# The instance the host discovers (see host.runtime.tools_host).
BUNDLED_TOOL = GmailTool()
