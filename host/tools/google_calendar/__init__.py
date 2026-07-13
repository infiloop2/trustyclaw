"""Google Calendar tool package."""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import ActionSpec, ConfigRequirement, ToolManifest
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
from host.tools.shared.google import GoogleCredentialStore, IntegrationReconnectRequired, clip_text, google_json_request

CALENDAR_API_BASE_URL = "https://www.googleapis.com/calendar/v3"
# Host approval summaries are capped at 500 UTF-8 bytes (tools_host SUMMARY_MAX_BYTES).
CALENDAR_SUMMARY_MAX_BYTES = 500
GOOGLE_OAUTH_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/calendar.events",
)
REQUIRED_CALENDAR_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/calendar.events.readonly",
        "https://www.googleapis.com/auth/calendar.events",
    }
)
CALENDAR_RECONNECT_MESSAGE = "Google Calendar is no longer connected. Please reconnect Calendar."
CALENDAR_READ_TOOL_FIELDS = frozenset({"start_time", "end_time"})
CALENDAR_CHANGE_OPERATIONS = frozenset({"create", "update", "delete"})
CALENDAR_CHANGE_TOOL_FIELDS = frozenset(
    {"operation", "event_id", "summary", "description", "location", "start_time", "end_time", "time_zone"}
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


CALENDAR_OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}},
    "additionalProperties": True,
}


MANIFEST = ToolManifest(
    tool_id="google_calendar",
    display_name="Google Calendar",
    description="Read events on the connected Google Calendar account and propose event changes. Sensitive actions are approval-gated.",
    connection="oauth",
    actions=(
        ActionSpec(id="read_events",
            description="Read events in a time range.",
            data_policy=(
                "Read-only. Sends the calendar id and time range to Google and returns "
                "event ids, times, titles, locations, and descriptions. Runs directly "
                "with no approval."
            ),
            input_schema=_schema({"start_time": {"type": "string"}, "end_time": {"type": "string"}}),
            output_schema=CALENDAR_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="event_change",
            description="Queue approval to create, update, or delete an event.",
            data_policy=(
                "Creates, updates, or deletes an event on the connected calendar. Queued "
                "for explicit approval; the change reaches Google only after you approve."
            ),
            input_schema=_schema(
                {
                    "operation": {"type": "string", "enum": ["create", "update", "delete"]},
                    "event_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                    "time_zone": {"type": "string"},
                },
                ["operation"],
            ),
            output_schema=CALENDAR_OUTPUT_SCHEMA,
        ),
    ),
    config=(
        ConfigRequirement(key="GOOGLE_OAUTH_CLIENT_ID", description="Google OAuth client id for the hosting deployment."),
        ConfigRequirement(key="GOOGLE_OAUTH_CLIENT_SECRET", description="Google OAuth client secret for the hosting deployment."),
    ),
    setup_guide=(
        "In Google Cloud Console, create an OAuth 2.0 Client ID of type \"Web application\" "
        "(https://console.cloud.google.com/apis/credentials) under a Google account you "
        "control (the same client can serve Gmail and Calendar), and add the redirect URI "
        "shown by Connect (the loopback admin origin, e.g. http://localhost:7443/oauth/callback) "
        "to its Authorized redirect URIs. Enable the Google Calendar API for the project. "
        "On the OAuth consent screen, publish to Production (or add the account as a test "
        "user) — Calendar scopes are sensitive, so test-mode refresh tokens expire after 7 "
        "days. Then set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET here, enable "
        "the tool, and Connect with that account."
    ),
)


CALENDAR_CREDENTIALS = GoogleCredentialStore(
    tool_id="google_calendar",
    scopes=GOOGLE_OAUTH_SCOPES,
    required_scopes=REQUIRED_CALENDAR_SCOPES,
    reconnect_message=CALENDAR_RECONNECT_MESSAGE,
)


def _string_value(record: JSONObject, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _calendar_response_time(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    date_time = value.get("dateTime")
    if isinstance(date_time, str):
        return date_time
    date = value.get("date")
    return date if isinstance(date, str) else ""


def _calendar_read_input(tool_input: JSONObject) -> JSONObject:
    extra_fields = set(tool_input) - CALENDAR_READ_TOOL_FIELDS
    if extra_fields:
        raise ToolInputValidationError("Calendar read tool input only supports start_time and end_time.")
    output: JSONObject = {}
    for key in ("start_time", "end_time"):
        value = tool_input.get(key)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ToolInputValidationError(f"Calendar read tool_input.{key} must be a string.")
            output[key] = value.strip()
    return output


def _calendar_events(access_token: str, tool_input: JSONObject) -> list[JSONObject]:
    current_time = datetime.now(timezone.utc)
    time_min = tool_input.get("start_time") if isinstance(tool_input.get("start_time"), str) else current_time.isoformat()
    time_max = tool_input.get("end_time") if isinstance(tool_input.get("end_time"), str) else (current_time + timedelta(days=7)).isoformat()
    params = urllib.parse.urlencode(
        {
            "maxResults": "10",
            "orderBy": "startTime",
            "singleEvents": "true",
            "timeMax": time_max,
            "timeMin": time_min,
        }
    )
    response = google_json_request(
        "GET",
        f"{CALENDAR_API_BASE_URL}/calendars/primary/events?{params}",
        access_token,
        failure_message="Calendar API request failed.",
        invalid_response_message="Calendar API returned an invalid response.",
    )
    events: list[JSONObject] = []
    for event in cast(list[Any], response.get("items", [])):
        if not isinstance(event, dict):
            continue
        event_object = cast(JSONObject, event)
        events.append(
            {
                "description": _string_value(event_object, ("description",)),
                "end_time": _calendar_response_time(event_object.get("end")),
                "html_link": _string_value(event_object, ("htmlLink",)),
                "id": _string_value(event_object, ("id",)),
                "location": _string_value(event_object, ("location",)),
                "start_time": _calendar_response_time(event_object.get("start")),
                "summary": _string_value(event_object, ("summary",)),
            }
        )
    return events


def _calendar_change_proposal(tool_input: JSONObject) -> JSONObject:
    extra_fields = set(tool_input) - CALENDAR_CHANGE_TOOL_FIELDS
    if extra_fields:
        raise ToolInputValidationError(
            "Calendar change tool input only supports operation, event_id, summary, description, location, start_time, end_time, and time_zone."
        )
    operation = _string_value(tool_input, ("operation",)).lower()
    if operation not in CALENDAR_CHANGE_OPERATIONS:
        raise ToolInputValidationError("Calendar change operation must be create, update, or delete.")
    proposal: JSONObject = {"operation": operation}
    # description and location may be set to "" on an update to clear them; every
    # other field must be a non-empty string.
    clearable_on_update = {"description", "location"}
    for key in ("event_id", "summary", "description", "location", "start_time", "end_time", "time_zone"):
        value = tool_input.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ToolInputValidationError(f"Calendar change tool_input.{key} must be a string.")
        stripped = value.strip()
        if not stripped and not (operation == "update" and key in clearable_on_update):
            raise ToolInputValidationError(f"Calendar change tool_input.{key} must be a non-empty string.")
        proposal[key] = stripped
    has_event_id = "event_id" in proposal
    changed_fields = ("summary", "description", "location", "start_time", "end_time")
    if operation == "create":
        if has_event_id:
            raise ToolInputValidationError("Calendar create must not include tool_input.event_id.")
        if not all(key in proposal for key in ("summary", "start_time", "end_time")):
            raise ToolInputValidationError("Calendar create requires tool_input.summary, tool_input.start_time, and tool_input.end_time.")
    if operation == "update":
        if not has_event_id:
            raise ToolInputValidationError("Calendar update requires tool_input.event_id.")
        if not any(key in proposal for key in changed_fields):
            raise ToolInputValidationError(
                "Calendar update requires at least one of tool_input.summary, tool_input.description, tool_input.location, tool_input.start_time, or tool_input.end_time."
            )
    if operation == "delete":
        if not has_event_id:
            raise ToolInputValidationError("Calendar delete requires tool_input.event_id.")
        if set(proposal) - {"operation", "event_id"}:
            raise ToolInputValidationError("Calendar delete only supports tool_input.operation and tool_input.event_id.")
    if "time_zone" in proposal and not any(key in proposal for key in ("start_time", "end_time")):
        raise ToolInputValidationError("Calendar time_zone requires tool_input.start_time or tool_input.end_time.")
    return proposal


def _calendar_event_preview(access_token: str, event_id: str) -> JSONObject:
    """Current state of one event, captured at proposal time so the approval
    can show what is being changed and detect changes before executing."""
    encoded_event_id = urllib.parse.quote(event_id, safe="")
    event = google_json_request(
        "GET",
        f"{CALENDAR_API_BASE_URL}/calendars/primary/events/{encoded_event_id}",
        access_token,
        failure_message="Calendar event lookup failed.",
        invalid_response_message="Calendar API returned an invalid response.",
    )
    organizer = event.get("organizer")
    # Only a definite non-self organizer means "guest"; an absent organizer is
    # unknown, not a guest.
    is_guest = isinstance(organizer, dict) and organizer.get("self") is False
    attendees = event.get("attendees")
    return {
        "attendee_count": len(attendees) if isinstance(attendees, list) else 0,
        "end_time": _calendar_response_time(event.get("end")),
        "event_id": _string_value(event, ("id",)) or event_id,
        "is_guest": is_guest,
        "location": _string_value(event, ("location",)),
        # An instance of a series carries recurringEventId; the series master
        # carries recurrence. Either means changing/deleting affects a series.
        "recurring": bool(event.get("recurringEventId")) or isinstance(event.get("recurrence"), list),
        "start_time": _calendar_response_time(event.get("start")),
        "status": _string_value(event, ("status",)),
        "summary": _string_value(event, ("summary",)) or "(no title)",
        "updated": _string_value(event, ("updated",)),
    }


def _verify_event_matches_approval(access_token: str, payload: JSONObject) -> None:
    """Rule 8: the event captured at proposal time must be unchanged when the
    approved payload executes, so the user never approves one meeting and
    changes or deletes another (or a since-edited version of it)."""
    approved_event = payload.get("current_event")
    if not isinstance(approved_event, dict):
        return
    current = _calendar_event_preview(access_token, _string_value(cast(JSONObject, approved_event), ("event_id",)))
    if current.get("updated") != approved_event.get("updated") or current.get("status") == "cancelled":
        raise RuntimeError("Calendar event changed after approval. Please queue a new approval.")


def _calendar_time_value(record: JSONObject, keys: tuple[str, ...]) -> JSONObject:
    for key in keys:
        value = record.get(key)
        if isinstance(value, dict):
            return cast(JSONObject, value)
        if isinstance(value, str) and value.strip():
            output: JSONObject = {"dateTime": value.strip()}
            time_zone = record.get("time_zone")
            if isinstance(time_zone, str) and time_zone.strip():
                output["timeZone"] = time_zone.strip()
            return output
    return {}


def _calendar_write_event(access_token: str, proposal: JSONObject) -> JSONObject:
    record = _calendar_change_proposal(proposal)
    operation = _string_value(record, ("operation",))
    event_id = _string_value(record, ("event_id",))
    if operation == "delete":
        encoded_event_id = urllib.parse.quote(event_id, safe="")
        google_json_request(
            "DELETE",
            f"{CALENDAR_API_BASE_URL}/calendars/primary/events/{encoded_event_id}",
            access_token,
            failure_message="Calendar API request failed.",
            invalid_response_message="Calendar API returned an invalid response.",
        )
        return {"event_id": event_id, "html_link": "", "status": "deleted"}
    body: JSONObject = {}
    summary = record.get("summary")
    if isinstance(summary, str) and summary.strip():
        body["summary"] = summary.strip()
    # description and location are emitted whenever provided, including as "" so an
    # update PATCHes the empty value and clears the field (validation only lets
    # them be empty on an update).
    for key in ("description", "location"):
        value = record.get(key)
        if isinstance(value, str):
            body[key] = value.strip()
    start = _calendar_time_value(record, ("start_time",))
    end = _calendar_time_value(record, ("end_time",))
    if start:
        body["start"] = start
    if end:
        body["end"] = end
    if not body:
        raise RuntimeError("Calendar approval is missing event changes.")
    if operation == "update":
        encoded_event_id = urllib.parse.quote(event_id, safe="")
        updated = google_json_request(
            "PATCH",
            f"{CALENDAR_API_BASE_URL}/calendars/primary/events/{encoded_event_id}",
            access_token,
            body=body,
            failure_message="Calendar API request failed.",
            invalid_response_message="Calendar API returned an invalid response.",
        )
        status = "updated"
    else:
        updated = google_json_request(
            "POST",
            f"{CALENDAR_API_BASE_URL}/calendars/primary/events",
            access_token,
            body=body,
            failure_message="Calendar API request failed.",
            invalid_response_message="Calendar API returned an invalid response.",
        )
        status = "created"
    return {"event_id": _string_value(updated, ("id",)), "html_link": _string_value(updated, ("htmlLink",)), "status": status}


def _calendar_summary(proposal: JSONObject, current_event: JSONObject | None) -> str:
    """Approval summary with enough event detail to verify the decision: the
    proposed fields for create, and the current event's title and start time
    (from the proposal-time preview) for update and delete."""
    operation = _string_value(proposal, ("operation",))
    if operation == "create":
        title = clip_text(_string_value(proposal, ("summary",)), 60)
        summary = (
            f"Create Google Calendar event \"{title}\" from "
            f"{clip_text(_string_value(proposal, ('start_time',)), 40)} to "
            f"{clip_text(_string_value(proposal, ('end_time',)), 40)}"
        )
        # Every accepted field must be visible: the write sends description,
        # location, and time_zone too when the proposal carries them.
        extras = ", ".join(
            f"{key} {clip_text(str(proposal[key]), 40)}"
            for key in ("location", "description", "time_zone")
            if key in proposal
        )
        return f"{summary} ({extras})." if extras else f"{summary}."
    event = current_event or {}
    # The identity and safety context (id, recurrence, guest, guest count) is
    # mandatory; location and the change-field previews are progressively clipped
    # so even a worst-case update stays within the host API's 500-byte limit.
    for change_field_clip, include_location in ((40, True), (24, False), (16, False)):
        summary = _calendar_change_summary(
            operation, proposal, event, change_field_clip=change_field_clip, include_location=include_location
        )
        if len(summary.encode("utf-8")) <= CALENDAR_SUMMARY_MAX_BYTES:
            return summary
    return summary


def _calendar_change_summary(
    operation: str, proposal: JSONObject, event: JSONObject, *, change_field_clip: int, include_location: bool
) -> str:
    title = clip_text(_string_value(event, ("summary",)) or "(no title)", 48)
    start = clip_text(_string_value(event, ("start_time",)) or "unknown", 40)
    described = f"Google Calendar event \"{title}\" (starts {start})"
    # Context that distinguishes a lookalike or high-stakes event so the operator
    # is not deciding on title + start alone: a stable id, whether it is a series
    # instance, whether the account is a guest rather than the organizer, guest
    # count, and location.
    context: list[str] = [f"id {clip_text(_string_value(event, ('event_id',)) or 'unknown', 60)}"]
    if event.get("recurring"):
        context.append("recurring event")
    if event.get("is_guest"):
        context.append("you are a guest, not the organizer")
    attendee_count = event.get("attendee_count")
    if isinstance(attendee_count, int) and attendee_count > 0:
        context.append(f"{attendee_count} guest{'s' if attendee_count != 1 else ''}")
    location = _string_value(event, ("location",))
    if include_location and location:
        context.append(f"location {clip_text(location, 40)}")
    described = f"{described} [{'; '.join(context)}]"
    if operation == "delete":
        return f"Delete {described}."
    changes = ", ".join(
        f"{key} → {clip_text(str(proposal[key]), change_field_clip)}"
        for key in ("summary", "description", "location", "start_time", "end_time", "time_zone")
        if key in proposal
    )
    return f"Update {described}: {changes}."


class GoogleCalendarTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> CredentialFlow:
        # GoogleCredentialStore implements the CredentialFlow protocol directly.
        return CALENDAR_CREDENTIALS

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            if action == "read_events":
                read_input = _calendar_read_input(tool_input)
                events = _calendar_events(CALENDAR_CREDENTIALS.access_token(api), read_input)
                result: JSONObject = {"status": "success_executed", "message": "Calendar events loaded.", "events": cast(list[JSONValue], events)}
                return ActionExecuted(result)
            if action == "event_change":
                proposal = _calendar_change_proposal(tool_input)
                access_token = CALENDAR_CREDENTIALS.access_token(api)
                account = CALENDAR_CREDENTIALS.refresh_identity(api, access_token)
                # Update and delete reference an existing event: capture its
                # current state so the approval shows which meeting is
                # affected and execute_approved can detect later changes.
                current_event: JSONObject | None = None
                operation = _string_value(proposal, ("operation",))
                if operation in {"update", "delete"}:
                    current_event = _calendar_event_preview(access_token, _string_value(proposal, ("event_id",)))
                payload: JSONObject = {
                    "action": action,
                    "calendar_account": {"email": account["label"], "sub": account["id"]},
                    "proposal": proposal,
                    "tool_id": MANIFEST.tool_id,
                }
                if current_event is not None:
                    payload["current_event"] = current_event
                approval = api.approvals.request(
                    action_id=action, summary=_calendar_summary(proposal, current_event), payload=payload
                )
                return ActionPendingApproval(approval.approval_id, approval.summary)
            return ActionFailed("Unsupported Calendar action.")
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except IntegrationReconnectRequired as exc:
            return ActionFailed(str(exc), reconnect_required=True)
        except Exception as exc:
            return ActionFailed(str(exc) or "Calendar tool request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        try:
            # The host hands a loaded record: approved, and this tool's own.
            payload = approval.payload
            proposal = payload.get("proposal")
            if not isinstance(proposal, dict):
                return ActionFailed("Calendar approval payload is invalid.")
            access_token = CALENDAR_CREDENTIALS.access_token(api)
            current_account = CALENDAR_CREDENTIALS.refresh_identity(api, access_token)
            approved_account = payload.get("calendar_account")
            if not isinstance(approved_account, dict):
                return ActionFailed("Calendar approval payload is invalid.")
            if approved_account.get("sub") != current_account["id"]:
                return ActionFailed("Google Calendar account changed after approval. Please queue a new approval.")
            _verify_event_matches_approval(access_token, payload)
            write_result = _calendar_write_event(access_token, cast(JSONObject, proposal))
            operation = _string_value(cast(JSONObject, proposal), ("operation",))
            event_id = _string_value(write_result, ("event_id",))
            # Surface the affected event id in the user-visible message so the
            # agent can reference the object it just created/changed.
            messages = {
                "create": f"Created Google Calendar event {event_id}.",
                "update": f"Updated Google Calendar event {event_id}.",
                "delete": f"Deleted Google Calendar event {event_id}.",
            }
            return ApprovalExecuted(messages.get(operation, f"Updated Google Calendar event {event_id}."))
        except IntegrationReconnectRequired as exc:
            return ActionFailed(str(exc), reconnect_required=True)
        except Exception as exc:
            return ActionFailed(str(exc) or "Calendar write failed after approval.")


# The instance the host discovers (see host.runtime.tools_host).
BUNDLED_TOOL = GoogleCalendarTool()
