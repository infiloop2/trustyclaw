from __future__ import annotations

import base64
import json
import re
import urllib.parse
from typing import Any, NamedTuple, cast

from host.tools.json_types import JSONObject, JSONValue
from host.tools.shared.google import google_json_request

GMAIL_API_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_READ_MAX_RESULTS = 5
GMAIL_READABLE_BODY_MAX_CHARS = 4_000
GMAIL_LABEL_LIST_MAX_RESULTS = 100
GMAIL_LABEL_ID_MAX_RESULTS = 20
GMAIL_ATTACHMENT_MAX_RESULTS = 10
GMAIL_SNIPPET_BODY_NOTE = (
    "Only Gmail's snippet is available because this message has no plaintext body. "
    "TrustyClaw does not render the HTML body here; double-check the message in Gmail before approving."
)
GMAIL_SNIPPET_NO_BODY_NOTE = "Only Gmail's snippet is available because this message has no plaintext or HTML body."
GMAIL_NO_BODY_NOTE = "No plaintext body, HTML body, or Gmail snippet was available for this message."
GMAIL_HTML_ONLY_NO_SNIPPET_NOTE = (
    "This message has HTML body content that TrustyClaw does not render here, and Gmail returned no "
    "snippet, so no preview is available. It will be sent as-is; double-check the message in Gmail before approving."
)
GMAIL_HTML_ALTERNATIVE_BODY_NOTE = (
    "This message also contains an HTML alternative that is not rendered in TrustyClaw approval previews. "
    "Double-check the message in Gmail before approving."
)
GMAIL_DRAFT_ATTACHMENT_UNSUPPORTED_MESSAGE = (
    "Gmail draft has attachments that cannot be preserved safely. Please recreate the approval."
)


class ToolInputValidationError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class GmailApiOperation(NamedTuple):
    method: str
    path_template: str
    title: str
    description: str
    requires_body: bool = False


GMAIL_OPERATIONS: dict[str, GmailApiOperation] = {
    "users.drafts.create": GmailApiOperation("POST", "/drafts", "Create Gmail draft", "Create a Gmail draft.", True),
    "users.drafts.delete": GmailApiOperation("DELETE", "/drafts/{id}", "Delete Gmail draft", "Delete a Gmail draft."),
    "users.drafts.get": GmailApiOperation("GET", "/drafts/{id}", "Get Gmail draft", "Read one Gmail draft."),
    "users.drafts.list": GmailApiOperation("GET", "/drafts", "List Gmail drafts", "Read Gmail draft metadata."),
    "users.drafts.send": GmailApiOperation("POST", "/drafts/send", "Send Gmail draft", "Send a Gmail draft.", True),
    "users.drafts.update": GmailApiOperation("PUT", "/drafts/{id}", "Update Gmail draft", "Update a Gmail draft.", True),
    "users.labels.create": GmailApiOperation("POST", "/labels", "Create Gmail label", "Create a Gmail label.", True),
    "users.labels.delete": GmailApiOperation("DELETE", "/labels/{id}", "Delete Gmail label", "Delete a Gmail label."),
    "users.labels.get": GmailApiOperation("GET", "/labels/{id}", "Get Gmail label", "Read one Gmail label."),
    "users.labels.list": GmailApiOperation("GET", "/labels", "List Gmail labels", "Read Gmail labels."),
    "users.labels.patch": GmailApiOperation("PATCH", "/labels/{id}", "Patch Gmail label", "Patch a Gmail label.", True),
    "users.messages.batchModify": GmailApiOperation("POST", "/messages/batchModify", "Batch modify Gmail messages", "Modify labels on multiple Gmail messages.", True),
    "users.messages.get": GmailApiOperation("GET", "/messages/{id}", "Get Gmail message", "Read one Gmail message."),
    "users.messages.list": GmailApiOperation("GET", "/messages", "List Gmail messages", "Read Gmail message ids."),
    "users.messages.send": GmailApiOperation("POST", "/messages/send", "Send Gmail message", "Send a Gmail message.", True),
    "users.messages.trash": GmailApiOperation("POST", "/messages/{id}/trash", "Trash Gmail message", "Move one Gmail message to trash."),
    "users.messages.untrash": GmailApiOperation("POST", "/messages/{id}/untrash", "Untrash Gmail message", "Remove one Gmail message from trash."),
    "users.threads.get": GmailApiOperation("GET", "/threads/{id}", "Get Gmail thread", "Read one Gmail thread."),
}


def string_value(record: JSONObject, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def json_object(value: JSONValue | None) -> JSONObject:
    return cast(JSONObject, value) if isinstance(value, dict) else {}


def _base64url_decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(f"{encoded}{padding}".encode("ascii"))


def _message_header(message: MappingObject, name: str) -> str:
    return _payload_header(message.get("payload"), name)


MappingObject = dict[str, Any] | JSONObject


def _payload_header(payload: object, name: str) -> str:
    headers = payload.get("headers") if isinstance(payload, dict) else None
    if not isinstance(headers, list):
        return ""
    normalized_name = name.lower()
    for header in headers:
        if not isinstance(header, dict):
            continue
        header_name = header.get("name")
        if isinstance(header_name, str) and header_name.lower() == normalized_name and isinstance(header.get("value"), str):
            return cast(str, header["value"])
    return ""


def _query_parameter_items(parameters: JSONObject) -> list[tuple[str, str]]:
    normalized: list[tuple[str, str]] = []
    for key, value in parameters.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str):
                normalized.append((key, item.strip()))
            elif isinstance(item, bool):
                normalized.append((key, "true" if item else "false"))
            elif isinstance(item, int):
                normalized.append((key, str(item)))
            else:
                raise ToolInputValidationError("Gmail query parameters only support strings, integers, booleans, or repeated strings.")
    return normalized


def _format_gmail_api_path(path_template: str, parameters: JSONObject) -> tuple[str, JSONObject]:
    consumed_keys: set[str] = set()
    path = path_template
    for key in re.findall(r"{([^}]+)}", path_template):
        value = parameters.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ToolInputValidationError(f"Gmail API operation requires parameters.{key}.")
        path = path.replace(f"{{{key}}}", urllib.parse.quote(value.strip(), safe=""))
        consumed_keys.add(key)
    query_parameters = {key: value for key, value in parameters.items() if key not in consumed_keys}
    return path, cast(JSONObject, query_parameters)


def gmail_api_url(path: str, query_parameters: JSONObject) -> str:
    query = urllib.parse.urlencode(_query_parameter_items(query_parameters))
    return f"{GMAIL_API_BASE_URL}{path}{f'?{query}' if query else ''}"


def gmail_operation_request(
    operation_name: str,
    parameters: JSONObject,
    body: JSONObject | None = None,
) -> JSONObject:
    operation = GMAIL_OPERATIONS.get(operation_name)
    if operation is None:
        raise ToolInputValidationError("Unsupported Gmail API operation.")
    if operation.requires_body and body is None:
        raise ToolInputValidationError("Gmail API operation requires tool_input.body.")
    path, query_parameters = _format_gmail_api_path(operation.path_template, parameters)
    request: JSONObject = {
        "operation": operation_name,
        "method": operation.method,
        "path": path,
        "parameters": query_parameters,
        "title": operation.title,
        "description": operation.description,
    }
    if body is not None:
        request["body"] = body
    return request


def execute_gmail_api_request(access_token: str, request: JSONObject) -> JSONObject:
    method = string_value(request, ("method",))
    path = string_value(request, ("path",))
    parameters = json_object(request.get("parameters"))
    body = json_object(request.get("body")) if "body" in request else None
    return google_json_request(
        method,
        gmail_api_url(path, parameters),
        access_token,
        body=body,
        failure_message="Gmail API request failed.",
        invalid_response_message="Gmail API returned an invalid response.",
    )


def _base64url_text(value: object, charset: str = "utf-8") -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        raw = _base64url_decode(value)
    except ValueError:
        return ""
    try:
        return raw.decode(charset or "utf-8", errors="replace").strip()
    except LookupError:
        return raw.decode("utf-8", errors="replace").strip()


def _payload_charset(payload: object) -> str:
    content_type = _payload_header(payload, "Content-Type")
    match = re.search(r"(?i)(?:^|;)\s*charset\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^;\s]+))", content_type)
    if match is None:
        return "utf-8"
    return (match.group(1) or match.group(2) or match.group(3) or "utf-8").strip()


class GmailBody(NamedTuple):
    text: str
    source: str
    note: str = ""


def _payload_is_attachment(payload: JSONObject) -> bool:
    if string_value(payload, ("filename",)):
        return True
    body = payload.get("body")
    if isinstance(body, dict) and string_value(cast(JSONObject, body), ("attachmentId",)):
        return True
    # Only a "Content-Disposition: attachment" part is an attachment. An inline
    # part with no filename or attachmentId is inline body text (or an inline
    # container); treating it as an attachment would hide that plaintext from the
    # operator's send/delete approval while the stored draft still sends it.
    return _payload_header(payload, "Content-Disposition").lower().startswith("attachment")


def gmail_message_body(message: JSONObject) -> GmailBody:
    def payload_body(payload: object) -> tuple[str, bool]:
        if not isinstance(payload, dict):
            return "", False
        typed_payload = cast(JSONObject, payload)
        mime_type = payload.get("mimeType")
        body = payload.get("body")
        has_html = mime_type == "text/html" and isinstance(body, dict)
        if _payload_is_attachment(typed_payload):
            return "", has_html
        plain_text = ""
        if mime_type == "text/plain" and isinstance(body, dict):
            plain_text = _base64url_text(body.get("data"), _payload_charset(payload))
        parts = payload.get("parts")
        if isinstance(parts, list):
            for part in parts:
                text, child_has_html = payload_body(part)
                has_html = has_html or child_has_html
                # Concatenate every plaintext part so a multipart body cannot
                # hide later text/plain segments from the operator; the caller
                # caps and discloses truncation of the combined body.
                if text:
                    plain_text = f"{plain_text}\n\n{text}" if plain_text else text
        return plain_text, has_html

    plain_text, has_html = payload_body(message.get("payload"))
    if plain_text:
        if has_html:
            return GmailBody(plain_text, "text/plain_with_html_alternative", GMAIL_HTML_ALTERNATIVE_BODY_NOTE)
        return GmailBody(plain_text, "text/plain")
    snippet = message.get("snippet")
    if isinstance(snippet, str) and snippet.strip():
        if not has_html:
            return GmailBody(snippet.strip(), "gmail_snippet", GMAIL_SNIPPET_NO_BODY_NOTE)
        return GmailBody(snippet.strip(), "gmail_snippet", GMAIL_SNIPPET_BODY_NOTE)
    # An HTML-only draft with no snippet still has content that will be sent, so
    # warn about the hidden HTML rather than claiming no body is available.
    if has_html:
        return GmailBody("", "unavailable", GMAIL_HTML_ONLY_NO_SNIPPET_NOTE)
    return GmailBody("", "unavailable", GMAIL_NO_BODY_NOTE)


def _body_fields(body: str) -> JSONObject:
    if len(body) <= GMAIL_READABLE_BODY_MAX_CHARS:
        return {"body": body}
    return {
        "body": body[:GMAIL_READABLE_BODY_MAX_CHARS].rstrip(),
        "bodyLimit": GMAIL_READABLE_BODY_MAX_CHARS,
        "bodyOriginalLength": len(body),
        "bodyTruncated": True,
    }


def gmail_message_attachments(message: JSONObject) -> tuple[list[JSONObject], int]:
    attachments: list[JSONObject] = []
    attachment_count = 0
    payloads: list[object] = [message.get("payload")]
    while payloads:
        payload = payloads.pop()
        if not isinstance(payload, dict):
            continue
        typed_payload = cast(JSONObject, payload)
        body = payload.get("body")
        filename = string_value(typed_payload, ("filename",))
        attachment_id = string_value(cast(JSONObject, body), ("attachmentId",)) if isinstance(body, dict) else ""
        if isinstance(body, dict) and _payload_is_attachment(typed_payload):
            size = body.get("size")
            attachment: JSONObject = {
                "filename": filename or "(unnamed attachment)",
                "mimeType": string_value(typed_payload, ("mimeType",)),
            }
            if attachment_id:
                attachment["attachmentId"] = attachment_id
            if not filename:
                attachment["unnamed"] = True
            if isinstance(size, int) and not isinstance(size, bool):
                attachment["size"] = size
            attachment_count += 1
            if len(attachments) < GMAIL_ATTACHMENT_MAX_RESULTS:
                attachments.append(attachment)
        parts = payload.get("parts")
        if isinstance(parts, list):
            payloads.extend(reversed(parts))
    return attachments, attachment_count


def add_attachment_fields(target: JSONObject, message: JSONObject) -> None:
    attachments, attachment_count = gmail_message_attachments(message)
    if attachments:
        target["attachments"] = cast(list[JSONValue], attachments)
    if attachment_count > len(attachments):
        target["attachmentsLimit"] = GMAIL_ATTACHMENT_MAX_RESULTS
        target["attachmentsOriginalCount"] = attachment_count
        target["attachmentsOmittedCount"] = attachment_count - len(attachments)
        target["attachmentsTruncated"] = True


def add_label_id_fields(target: JSONObject, value: object) -> None:
    if not isinstance(value, list):
        return
    label_ids = [label_id for label_id in value if isinstance(label_id, str)]
    if not label_ids:
        return
    target["labelIds"] = cast(list[JSONValue], label_ids[:GMAIL_LABEL_ID_MAX_RESULTS])
    if len(label_ids) > GMAIL_LABEL_ID_MAX_RESULTS:
        target["labelIdsLimit"] = GMAIL_LABEL_ID_MAX_RESULTS
        target["labelIdsOriginalCount"] = len(label_ids)
        target["labelIdsOmittedCount"] = len(label_ids) - GMAIL_LABEL_ID_MAX_RESULTS
        target["labelIdsTruncated"] = True


def gmail_message_index_entry(message: JSONObject) -> JSONObject:
    index_entry: JSONObject = {}
    for field in ("id", "threadId", "snippet", "historyId", "internalDate", "sizeEstimate"):
        value = message.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            index_entry[field] = value
    add_label_id_fields(index_entry, message.get("labelIds"))
    for header_name, output_key in (
        ("From", "from"),
        ("To", "to"),
        ("Cc", "cc"),
        ("Bcc", "bcc"),
        ("Subject", "subject"),
        ("Date", "date"),
    ):
        header_value = _message_header(message, header_name)
        if header_value:
            index_entry[output_key] = header_value
    return index_entry


def gmail_readable_message(message: JSONObject) -> JSONObject:
    enriched_message = gmail_message_index_entry(message)
    body = gmail_message_body(message)
    if body.text:
        enriched_message.update(_body_fields(body.text))
    if body.source:
        enriched_message["bodySource"] = body.source
    if body.note:
        enriched_message["bodyNote"] = body.note
    add_attachment_fields(enriched_message, message)
    return enriched_message


def gmail_draft_preview(access_token: str, draft_id: str) -> JSONObject:
    response = execute_gmail_api_request(
        access_token,
        gmail_operation_request("users.drafts.get", {"id": draft_id, "format": "full"}),
    )
    raw_message = response.get("message")
    message = cast(JSONObject, raw_message) if isinstance(raw_message, dict) else {}
    body = gmail_message_body(message)
    preview: JSONObject = {
        "bcc": _message_header(message, "Bcc"),
        "cc": _message_header(message, "Cc"),
        "date": _message_header(message, "Date"),
        "draftId": draft_id,
        "from": _message_header(message, "From"),
        "inReplyTo": _message_header(message, "In-Reply-To"),
        "messageId": string_value(message, ("id",)),
        "references": _message_header(message, "References"),
        "replyTo": _message_header(message, "Reply-To"),
        "snippet": string_value(message, ("snippet",)),
        "subject": _message_header(message, "Subject") or "(no subject)",
        "threadId": string_value(message, ("threadId",)),
        "to": _message_header(message, "To"),
    }
    if body.text:
        preview.update(_body_fields(body.text))
    if body.source:
        preview["bodySource"] = body.source
    if body.note:
        preview["bodyNote"] = body.note
    add_attachment_fields(preview, message)
    return preview


def gmail_message_action_summaries(access_token: str, message_ids: list[str]) -> list[JSONObject]:
    summaries: list[JSONObject] = []
    for message_id in message_ids:
        response = execute_gmail_api_request(
            access_token,
            gmail_operation_request(
                "users.messages.get",
                {
                    "id": message_id,
                    "format": "metadata",
                    "metadataHeaders": ["From", "Subject", "Date"],
                },
            ),
        )
        summaries.append({
            "date": _message_header(response, "Date"),
            "from": _message_header(response, "From"),
            "id": message_id,
            "snippet": string_value(response, ("snippet",)),
            "subject": _message_header(response, "Subject"),
            "threadId": string_value(response, ("threadId",)),
        })
    return summaries


def gmail_label_summary(label: JSONObject, fallback_id: str = "") -> JSONObject:
    label_id = string_value(label, ("id",)) or fallback_id
    summary: JSONObject = {
        "id": label_id,
        "name": string_value(label, ("name",)) or label_id,
        "type": string_value(label, ("type",)),
    }
    color = json_object(label.get("color"))
    if color:
        summary["color"] = color
    return summary


def gmail_label_preview(access_token: str, label_id: str) -> JSONObject:
    response = execute_gmail_api_request(
        access_token,
        gmail_operation_request("users.labels.get", {"id": label_id}),
    )
    return gmail_label_summary(response, label_id)


def gmail_label_summaries(access_token: str, label_ids: list[str]) -> list[JSONObject]:
    if not label_ids:
        return []
    response = execute_gmail_api_request(access_token, gmail_operation_request("users.labels.list", {}))
    raw_labels = response.get("labels")
    labels = raw_labels if isinstance(raw_labels, list) else []
    labels_by_id = {
        string_value(cast(JSONObject, label), ("id",)): cast(JSONObject, label)
        for label in labels
        if isinstance(label, dict) and string_value(cast(JSONObject, label), ("id",))
    }
    return [gmail_label_summary(labels_by_id.get(label_id, {}), label_id) for label_id in label_ids]


def gmail_label_list_summary(response: JSONObject) -> JSONObject:
    raw_labels = response.get("labels")
    labels = raw_labels if isinstance(raw_labels, list) else []
    label_summaries = [
        gmail_label_summary(cast(JSONObject, label))
        for label in labels[:GMAIL_LABEL_LIST_MAX_RESULTS]
        if isinstance(label, dict)
    ]
    summary: JSONObject = {
        "labels": cast(list[JSONValue], label_summaries),
        "labelCount": len(labels),
    }
    if isinstance(response.get("nextPageToken"), str):
        summary["nextPageToken"] = cast(str, response["nextPageToken"])
    if len(labels) > GMAIL_LABEL_LIST_MAX_RESULTS:
        summary["labelsTruncated"] = True
        summary["labelLimit"] = GMAIL_LABEL_LIST_MAX_RESULTS
        summary["labelOmittedCount"] = len(labels) - GMAIL_LABEL_LIST_MAX_RESULTS
    return summary


def gmail_search_messages(access_token: str, query: str) -> list[JSONObject]:
    listing_parameters: JSONObject = {"maxResults": GMAIL_READ_MAX_RESULTS}
    if query:
        listing_parameters["q"] = query
    listing = execute_gmail_api_request(
        access_token,
        gmail_operation_request("users.messages.list", listing_parameters),
    )
    summaries: list[JSONObject] = []
    for entry in cast(list[Any], listing.get("messages", [])):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        message_id = cast(str, entry["id"])
        detail = execute_gmail_api_request(
            access_token,
            gmail_operation_request(
                "users.messages.get",
                {
                    "id": message_id,
                    "format": "metadata",
                    "metadataHeaders": ["From", "Subject", "Date"],
                },
            ),
        )
        summaries.append(
            {
                "date": _message_header(detail, "Date"),
                "from": _message_header(detail, "From"),
                "id": message_id,
                "snippet": string_value(detail, ("snippet",)),
                "subject": _message_header(detail, "Subject"),
                "threadId": string_value(detail, ("threadId",)),
            }
        )
    return summaries
