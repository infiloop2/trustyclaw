from __future__ import annotations

import base64
import copy
from contextlib import contextmanager
import hashlib
import io
import unittest
import urllib.error
from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import patch

from host.tools.host_api import ApprovalRecord, AssetMetadata
from host.tools.json_types import JSONObject
from host.tools.manifest import (
    ActionSpec,
    DataSummary,
    DataSummaryCard,
    DataSummaryLink,
    SetupStep,
    ToolManifest,
)
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools import brave_search
from host.tools.brave_search import BraveSearchTool
from host.tools.gmail import GmailTool
from host.tools import gmail
from host.tools.gmail import api as gmail_api
from host.tools.google_calendar import GoogleCalendarTool
from host.tools import google_calendar
from host.tools.shared import google as google_shared
from host.tools.shared.web import WebRequestError


FRESH_EXPIRES_AT = 2_000_000_000
EXAMPLE_DATA_SUMMARY = DataSummary(
    cards=(
        DataSummaryCard(title="What leaves this host", description="Request data."),
        DataSummaryCard(title="Where it can go", description="Only to the provider."),
        DataSummaryCard(title="What the provider can do with it", description="Provider policy applies."),
        DataSummaryCard(title="How long it is retained", description="Provider retention applies."),
    ),
)


class MemoryCredentials:
    def __init__(self) -> None:
        self.record: JSONObject | None = None

    def load(self) -> JSONObject | None:
        return copy.deepcopy(self.record) if self.record is not None else None

    def save(self, credential: JSONObject) -> None:
        self.record = copy.deepcopy(credential)

    def clear(self) -> None:
        self.record = None

class _ConfigView(dict[str, str]):
    """Mirror the production config view (tools_host._ToolConfigView): reading an
    unset key raises a RuntimeError with the operator-actionable message tools
    surface verbatim, not a bare KeyError, so tests exercise the real path."""

    def __missing__(self, key: str) -> str:
        raise RuntimeError(
            f"Tool config {key} is not set. The operator must set it in the admin UI's Tools tab."
        )


def default_config() -> "_ConfigView":
    return _ConfigView(
        {
            "BRAVE_SEARCH_API_KEY": "brave-key",
            "GOOGLE_OAUTH_CLIENT_ID": "google-client",
            "GOOGLE_OAUTH_CLIENT_SECRET": "google-secret",
        }
    )


class MemoryApprovals:
    def __init__(self) -> None:
        self.records: dict[str, ApprovalRecord] = {}
        self.counter = 0

    def request(self, *, action_id: str, summary: str, payload: JSONObject) -> ApprovalRecord:
        self.counter += 1
        record = ApprovalRecord(
            approval_id=f"approval-{self.counter}",
            action_id=action_id,
            status="pending",
            payload=copy.deepcopy(payload),
            summary=summary,
            created_at=self.counter,
        )
        self.records[record.approval_id] = record
        return record

    def get(self, approval_id: str) -> ApprovalRecord | None:
        return self.records.get(approval_id)

    def approve(self, approval_id: str) -> ApprovalRecord:
        existing = self.records[approval_id]
        approved = ApprovalRecord(
            approval_id=existing.approval_id,
            action_id=existing.action_id,
            status="approved",
            payload=existing.payload,
            summary=existing.summary,
            created_at=existing.created_at,
            decided_at=existing.created_at + 1,
        )
        self.records[approval_id] = approved
        return approved


class MemoryAssets:
    def __init__(self) -> None:
        self.records: dict[str, tuple[AssetMetadata, bytes]] = {}

    def add(
        self,
        asset_id: str = "asset_abcdefghijklmnopqrstuvwxyz123456",
        *,
        filename: str = "video.mp4",
        media_type: str = "video/mp4",
        data: bytes = b"video bytes",
    ) -> str:
        self.records[asset_id] = (
            AssetMetadata(
                asset_id=asset_id,
                filename=filename,
                media_type=media_type,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                expires_at=2_000_000_000,
            ),
            data,
        )
        return asset_id

    def describe(self, asset_id: str) -> AssetMetadata:
        return self.records[asset_id][0]

    @contextmanager
    def open(self, asset_id: str):
        yield io.BytesIO(self.records[asset_id][1])

    def delete(self, asset_id: str) -> None:
        self.records.pop(asset_id, None)

@dataclass(frozen=True)
class FakeHostAPI:
    credentials: MemoryCredentials = field(default_factory=MemoryCredentials)
    config: dict[str, str] = field(default_factory=default_config)
    approvals: MemoryApprovals = field(default_factory=MemoryApprovals)
    assets: MemoryAssets = field(default_factory=MemoryAssets)


def connected_google_api(tool_id: str, scopes: frozenset[str]) -> FakeHostAPI:
    api = FakeHostAPI()
    api.credentials.save(
        {
            "account": {"id": "google-sub-1", "label": "user@example.com", "scopes": sorted(scopes)},
            "secret": {
                "access_token": f"{tool_id}-access-token",
                "expires_at": FRESH_EXPIRES_AT,
                "refresh_token": f"{tool_id}-refresh-token",
                "scope": " ".join(sorted(scopes)),
                "token_type": "Bearer",
            },
            "metadata": {
                "created_at": 1,
                "email_verified": True,
                "identity_checked_at": 1,
                "updated_at": 1,
            },
        }
    )
    return api


def google_userinfo(sub: str = "google-sub-1", email: str = "user@example.com") -> dict[str, object]:
    return {"email": email, "email_verified": True, "sub": sub}


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.urlopen_guard = patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("tests must mock every third-party network call"),
        )
        self.urlopen_guard.start()

    def tearDown(self) -> None:
        self.urlopen_guard.stop()

    def test_tool_manifest_rejects_invalid_tool_ids(self) -> None:
        def manifest(tool_id: str) -> ToolManifest:
            return ToolManifest(
                tool_id=tool_id,
                display_name="Example",
                description="Example tool.",
                connection="enable_only",
                data_summary=EXAMPLE_DATA_SUMMARY,
                actions=(),
            )

        self.assertEqual(manifest("a").tool_id, "a")
        self.assertEqual(manifest("example_tool_1").tool_id, "example_tool_1")
        for tool_id in ("", "Gmail", "gmail-tool", "1gmail", "_gmail", "gmail.tool", "g" * 65):
            with self.subTest(tool_id=tool_id):
                with self.assertRaisesRegex(ValueError, "ToolManifest.tool_id"):
                    manifest(tool_id)

    def test_tool_manifest_validates_connection_guide_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "protections"):
            ToolManifest(
                tool_id="bad_protection",
                display_name="Bad",
                description="Bad protection.",
                connection="enable_only",
                data_summary=EXAMPLE_DATA_SUMMARY,
                actions=(),
                protections=("",),
            )
        with self.assertRaisesRegex(ValueError, "technical_details"):
            ToolManifest(
                tool_id="bad_technical_detail",
                display_name="Bad",
                description="Bad technical detail.",
                connection="enable_only",
                data_summary=EXAMPLE_DATA_SUMMARY,
                actions=(),
                technical_details=("",),
            )
        with self.assertRaisesRegex(ValueError, "link_url and link_label"):
            ToolManifest(
                tool_id="bad_step",
                display_name="Bad",
                description="Bad setup step.",
                connection="enable_only",
                data_summary=EXAMPLE_DATA_SUMMARY,
                actions=(),
                setup_steps=(SetupStep("Create", "Create it.", link_url="https://example.com"),),
            )
        with self.assertRaisesRegex(ValueError, "approval must be direct or operator"):
            ToolManifest(
                tool_id="bad_approval",
                display_name="Bad",
                description="Bad action control.",
                connection="enable_only",
                data_summary=EXAMPLE_DATA_SUMMARY,
                actions=(ActionSpec("write", "Write.", "Writes data.", {}, approval="invalid"),),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "local PNG guide asset"):
            ToolManifest(
                tool_id="remote_image",
                display_name="Bad",
                description="Remote guide image.",
                connection="enable_only",
                data_summary=EXAMPLE_DATA_SUMMARY,
                actions=(),
                setup_steps=(SetupStep("Create", "Create it.", image_path="https://example.com/step.png", image_alt="Step."),),
            )
        with self.assertRaisesRegex(ValueError, "exactly four cards"):
            ToolManifest(
                tool_id="bad_summary",
                display_name="Bad",
                description="Bad data summary.",
                connection="enable_only",
                actions=(),
                data_summary=DataSummary(cards=EXAMPLE_DATA_SUMMARY.cards[:2]),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "HTTPS URL"):
            ToolManifest(
                tool_id="bad_policy",
                display_name="Bad",
                description="Bad policy link.",
                connection="enable_only",
                actions=(),
                data_summary=DataSummary(
                    cards=(
                        EXAMPLE_DATA_SUMMARY.cards[0],
                        EXAMPLE_DATA_SUMMARY.cards[1],
                        DataSummaryCard(
                            title="What the provider can do with it",
                            description="Provider policy applies.",
                            links=(DataSummaryLink("Privacy", "http://example.com/privacy"),),
                        ),
                        EXAMPLE_DATA_SUMMARY.cards[3],
                    ),
                ),
            )

    def test_bundled_manifests_declare_sensitive_action_controls(self) -> None:
        brave_search_action = brave_search.MANIFEST.action("search_web")
        gmail_read_action = gmail.MANIFEST.action("read_message")
        gmail_send_action = gmail.MANIFEST.action("send_email")
        calendar_change_action = google_calendar.MANIFEST.action("event_change")
        self.assertIsNotNone(brave_search_action)
        self.assertIsNotNone(gmail_read_action)
        self.assertIsNotNone(gmail_send_action)
        self.assertIsNotNone(calendar_change_action)
        assert brave_search_action is not None
        assert gmail_read_action is not None
        assert gmail_send_action is not None
        assert calendar_change_action is not None
        self.assertEqual(brave_search_action.approval, "direct")
        self.assertEqual(gmail_read_action.approval, "direct")
        self.assertEqual(gmail_send_action.approval, "operator")
        self.assertEqual(calendar_change_action.approval, "operator")
        for manifest in (gmail.MANIFEST, google_calendar.MANIFEST):
            protection_copy = " ".join(manifest.protections)
            self.assertIn("waits for explicit operator approval", protection_copy)
            self.assertNotIn("rechecks the account", protection_copy)
        for manifest in (brave_search.MANIFEST, gmail.MANIFEST, google_calendar.MANIFEST):
            self.assertTrue(manifest.protections)
            self.assertTrue(manifest.setup_steps)
            self.assertEqual(len(manifest.data_summary.cards), 4)
            self.assertEqual(manifest.data_summary.cards[0].title, "What leaves this host")
            self.assertEqual(manifest.data_summary.cards[1].title, "Where it can go")
            self.assertTrue(all(card.title for card in manifest.data_summary.cards))
        self.assertIn("search query", brave_search.MANIFEST.data_summary.cards[0].description)
        gmail_search = gmail.MANIFEST.action("search_messages")
        calendar_read = google_calendar.MANIFEST.action("read_events")
        self.assertIsNotNone(gmail_search)
        self.assertIsNotNone(calendar_read)
        assert gmail_search is not None
        assert calendar_read is not None
        self.assertIn("start_time/end_time", gmail_search.data_policy)
        self.assertIn("requested time range", calendar_read.data_policy)

    def test_brave_search_uses_host_config_and_mocked_third_party(self) -> None:
        api = FakeHostAPI()
        captured_payloads: list[JSONObject] = []

        def fake_post(api_key: str, payload: JSONObject) -> dict[str, Any]:
            self.assertEqual(api_key, "brave-key")
            captured_payloads.append(payload)
            return {
                "grounding": {
                    "generic": [
                        {
                            "title": "Example result",
                            "url": "https://example.com",
                            "snippets": ["One", "Two"],
                        }
                    ]
                }
            }

        with patch.object(brave_search, "_post_brave_context", side_effect=fake_post):
            result = BraveSearchTool().execute("search_web", {"query": "latest docs"}, api)

        self.assertIsInstance(result, ActionExecuted)
        executed = result
        self.assertEqual(captured_payloads[0]["q"], "latest docs")
        self.assertEqual(executed.result["message"], "Brave Search returned 1 grounding result(s).")

    def test_brave_search_rejects_unsupported_action(self) -> None:
        result = BraveSearchTool().execute("write_web", {"query": "x"}, FakeHostAPI())
        self.assertIsInstance(result, ActionFailed)
        self.assertEqual(result.error, "Unsupported Brave Search action.")

    def test_brave_request_uses_hardened_json_boundary(self) -> None:
        captured: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            captured.update(method=method, url=url, **kwargs)
            return {"grounding": {"generic": []}}

        with patch.object(brave_search, "json_request", side_effect=fake_json_request):
            response = brave_search._post_brave_context("secret", {"q": "safe"})

        self.assertEqual(response, {"grounding": {"generic": []}})
        self.assertEqual(captured["url"], brave_search.BRAVE_LLM_CONTEXT_ENDPOINT)
        self.assertEqual(captured["headers"], {"x-subscription-token": "secret"})
        self.assertEqual(captured["body"], {"q": "safe"})

    def test_brave_request_maps_hardened_http_errors(self) -> None:
        with patch.object(
            brave_search,
            "json_request",
            side_effect=WebRequestError("failed", status=401),
        ):
            with self.assertRaisesRegex(RuntimeError, "rejected the configured API key"):
                brave_search._post_brave_context("secret", {"q": "safe"})

    def test_brave_results_drop_active_and_credentialed_urls(self) -> None:
        results = brave_search._grounding_results(
            {
                "grounding": {
                    "generic": [
                        {"title": "x" * 1_000, "url": "javascript:alert(1)"},
                        {"title": "credentialed", "url": "https://user:pass@example.com/private"},
                        {"title": "safe", "url": "https://example.com/result"},
                    ]
                }
            }
        )
        self.assertEqual([result["url"] for result in results], ["", "", "https://example.com/result"])
        self.assertEqual(len(str(results[0]["title"])), 1_000)

    def test_gmail_credential_flow_stores_tokens_and_connection_status(self) -> None:
        api = FakeHostAPI()
        flow = GmailTool().credentials
        start = flow.start_connect({"redirect_uri": "https://host.example/oauth/callback"}, api)

        with (
            patch.object(
                google_shared,
                "exchange_google_oauth_code",
                return_value={
                    "access_token": "new-access-token",
                    "expires_in": 3600,
                    "refresh_token": "new-refresh-token",
                    "scope": " ".join(gmail.GOOGLE_OAUTH_SCOPES),
                    "token_type": "Bearer",
                },
            ),
            patch.object(google_shared, "get_google_userinfo", return_value=google_userinfo()),
        ):
            result = flow.complete_connect(
                {
                    "code": "oauth-code",
                    "redirect_uri": "https://host.example/oauth/callback",
                    "state": str(start["state"]),
                },
                api,
            )

        self.assertEqual(result["account"]["id"], "google-sub-1")
        self.assertEqual(result["account"]["label"], "user@example.com")
        self.assertEqual(flow.connection_status(api), {"connected": True, "account": result["account"]})
        stored = api.credentials.load()
        self.assertIsNotNone(stored)
        self.assertEqual(stored["account"]["id"], "google-sub-1")
        self.assertEqual(stored["secret"]["refresh_token"], "new-refresh-token")

    def test_invalid_grant_during_refresh_clears_the_credential(self) -> None:
        # A revoked/expired refresh token fails closed: the credential is
        # cleared and the caller gets the reconnect flow.
        api = connected_google_api(gmail.MANIFEST.tool_id, gmail.REQUIRED_GMAIL_SCOPES)
        stored = api.credentials.load()
        assert stored is not None
        api.credentials.save({**stored, "secret": {**stored["secret"], "expires_at": 1}})
        with patch.object(
            google_shared,
            "refresh_google_oauth_token",
            side_effect=google_shared.GoogleOAuthInvalidGrantError("invalid_grant"),
        ):
            with self.assertRaises(google_shared.IntegrationReconnectRequired):
                gmail.GMAIL_CREDENTIALS.access_token(api)
        self.assertIsNone(api.credentials.load())

    def test_failed_reconnect_keeps_the_existing_credential(self) -> None:
        # An already-connected account survives a failed attempt to connect
        # another account: an insufficient new grant was never saved, so
        # nothing may be cleared.
        api = connected_google_api(gmail.MANIFEST.tool_id, gmail.REQUIRED_GMAIL_SCOPES)
        flow = GmailTool().credentials
        start = flow.start_connect({"redirect_uri": "https://host.example/oauth/callback"}, api)
        with patch.object(
            google_shared,
            "exchange_google_oauth_code",
            return_value={
                "access_token": "narrow-token",
                "expires_in": 3600,
                "scope": "openid email",  # missing the Gmail scopes
                "token_type": "Bearer",
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "missing required permissions"):
                flow.complete_connect(
                    {
                        "code": "oauth-code",
                        "redirect_uri": "https://host.example/oauth/callback",
                        "state": str(start["state"]),
                    },
                    api,
                )
        current = api.credentials.load()
        assert current is not None
        self.assertEqual(current["account"]["id"], "google-sub-1")

    def test_gmail_search_messages_reads_without_approval(self) -> None:
        api = connected_google_api(gmail.MANIFEST.tool_id, gmail.REQUIRED_GMAIL_SCOPES)
        calls: list[JSONObject] = []

        def fake_gmail_request(access_token: str, request: JSONObject) -> JSONObject:
            self.assertEqual(access_token, "gmail-access-token")
            calls.append(request)
            operation = request["operation"]
            if operation == "users.messages.list":
                self.assertEqual(request["parameters"]["q"], "from:alice@example.com")
                return {"messages": [{"id": "msg-1"}]}
            if operation == "users.messages.get":
                message_id = str(request["path"]).rsplit("/", 1)[-1]
                return {
                    "id": message_id,
                    "threadId": "thread-1",
                    "snippet": "Hello snippet",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "Alice <alice@example.com>"},
                            {"name": "Subject", "value": "Hello"},
                            {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
                        ]
                    },
                }
            raise AssertionError(f"unexpected Gmail operation: {operation}")

        with patch.object(gmail_api, "execute_gmail_api_request", side_effect=fake_gmail_request):
            result = GmailTool().execute(
                "search_messages",
                {"query": "from:alice@example.com"},
                api,
            )

        self.assertIsInstance(result, ActionExecuted)
        executed = result
        self.assertEqual([call["operation"] for call in calls], ["users.messages.list", "users.messages.get"])
        self.assertEqual(executed.result["messages"][0]["subject"], "Hello")

    def test_gmail_write_queues_exact_payload_then_executes_after_approval(self) -> None:
        api = connected_google_api(gmail.MANIFEST.tool_id, gmail.REQUIRED_GMAIL_SCOPES)
        tool = GmailTool()
        executed_requests: list[JSONObject] = []

        with patch.object(google_shared, "get_google_userinfo", return_value=google_userinfo()):
            pending = tool.execute(
                "send_email",
                {
                    "to": "recipient@example.com",
                    "subject": "Approval subject",
                    "blocks": [{"type": "paragraph", "text": "Approval body"}],
                },
                api,
            )

        self.assertIsInstance(pending, ActionPendingApproval)
        approval = api.approvals.get(pending.approval_id)
        self.assertIsNotNone(approval)
        self.assertEqual(approval.status, "pending")
        self.assertEqual(approval.payload["action_type"], gmail.GMAIL_SEND_ACTION_TYPE)
        self.assertEqual(approval.payload["proposal"]["draft"]["to"], "recipient@example.com")

        def fake_execute_gmail_request(access_token: str, request: JSONObject) -> JSONObject:
            self.assertEqual(access_token, "gmail-access-token")
            executed_requests.append(request)
            return {"id": "sent-message-1"}

        approved_record = api.approvals.approve(pending.approval_id)
        with (
            patch.object(google_shared, "get_google_userinfo", return_value=google_userinfo()),
            patch.object(gmail, "execute_gmail_api_request", side_effect=fake_execute_gmail_request),
        ):
            executed = tool.execute_approved(approved_record, api)

        self.assertIsInstance(executed, ApprovalExecuted)
        self.assertEqual([request["operation"] for request in executed_requests], ["users.messages.send"])
        self.assertEqual(executed.message, "Gmail message sent after approval.")

    def test_google_calendar_read_events_uses_mocked_third_party(self) -> None:
        api = connected_google_api(google_calendar.MANIFEST.tool_id, google_calendar.REQUIRED_CALENDAR_SCOPES)
        calls: list[tuple[str, str, JSONObject | None]] = []

        def fake_google_request(
            method: str,
            url: str,
            access_token: str,
            *,
            body: JSONObject | None = None,
            failure_message: str,
            invalid_response_message: str,
        ) -> JSONObject:
            del failure_message, invalid_response_message
            self.assertEqual(access_token, "google_calendar-access-token")
            calls.append((method, url, body))
            return {
                "items": [
                    {
                        "description": "Roadmap sync",
                        "end": {"dateTime": "2024-01-01T10:30:00Z"},
                        "htmlLink": "https://calendar.example/event",
                        "id": "event-1",
                        "location": "Room 4",
                        "start": {"dateTime": "2024-01-01T10:00:00Z"},
                        "summary": "Planning",
                    }
                ]
            }

        with patch.object(google_calendar, "google_json_request", side_effect=fake_google_request):
            result = GoogleCalendarTool().execute(
                "read_events",
                {"start_time": "2024-01-01T00:00:00Z", "end_time": "2024-01-02T00:00:00Z"},
                api,
            )

        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(calls[0][0], "GET")
        self.assertIn("/calendars/primary/events?", calls[0][1])
        event = result.result["events"][0]
        self.assertEqual(event["summary"], "Planning")
        # The read action promises locations and descriptions, so surface them.
        self.assertEqual(event["location"], "Room 4")
        self.assertEqual(event["description"], "Roadmap sync")

    def test_google_calendar_bounds_untrusted_provider_event_count(self) -> None:
        api = connected_google_api(google_calendar.MANIFEST.tool_id, google_calendar.REQUIRED_CALENDAR_SCOPES)
        provider_events = [
            {"id": str(index), "summary": "x" * 2_000, "description": "y" * 8_000}
            for index in range(20)
        ]
        with patch.object(google_calendar, "google_json_request", return_value={"items": provider_events}):
            result = GoogleCalendarTool().execute("read_events", {}, api)

        assert isinstance(result, ActionExecuted)
        events = result.result["events"]
        assert isinstance(events, list)
        self.assertEqual(len(events), google_calendar.CALENDAR_READ_MAX_EVENTS)
        self.assertEqual(len(str(events[0]["summary"])), 2_000)
        self.assertEqual(len(str(events[0]["description"])), 8_000)

    def test_google_calendar_write_queues_then_executes_after_approval(self) -> None:
        api = connected_google_api(google_calendar.MANIFEST.tool_id, google_calendar.REQUIRED_CALENDAR_SCOPES)
        tool = GoogleCalendarTool()
        with patch.object(google_shared, "get_google_userinfo", return_value=google_userinfo()):
            pending = tool.execute(
                "event_change",
                {
                    "operation": "create",
                    "summary": "Review design",
                    "start_time": "2024-01-01T12:00:00Z",
                    "end_time": "2024-01-01T12:30:00Z",
                },
                api,
            )

        self.assertIsInstance(pending, ActionPendingApproval)
        approval = api.approvals.get(pending.approval_id)
        self.assertIsNotNone(approval)
        self.assertEqual(approval.payload["calendar_account"], {"email": "user@example.com", "sub": "google-sub-1"})
        self.assertEqual(approval.payload["proposal"]["summary"], "Review design")
        calls: list[tuple[str, str, JSONObject | None]] = []

        def fake_google_request(
            method: str,
            url: str,
            access_token: str,
            *,
            body: JSONObject | None = None,
            failure_message: str,
            invalid_response_message: str,
        ) -> JSONObject:
            del failure_message, invalid_response_message
            self.assertEqual(access_token, "google_calendar-access-token")
            calls.append((method, url, body))
            return {"htmlLink": "https://calendar.example/new", "id": "event-new"}

        approved_record = api.approvals.approve(pending.approval_id)
        with (
            patch.object(google_shared, "get_google_userinfo", return_value=google_userinfo()),
            patch.object(google_calendar, "google_json_request", side_effect=fake_google_request),
        ):
            executed = tool.execute_approved(approved_record, api)

        self.assertIsInstance(executed, ApprovalExecuted)
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][2]["summary"], "Review design")
        self.assertEqual(executed.message, "Created Google Calendar event event-new.")

    def test_google_calendar_update_captures_event_preview_and_verifies_before_executing(self) -> None:
        api = connected_google_api(google_calendar.MANIFEST.tool_id, google_calendar.REQUIRED_CALENDAR_SCOPES)
        tool = GoogleCalendarTool()
        event_state = {
            "id": "event-1",
            "start": {"dateTime": "2024-01-01T10:00:00Z"},
            "end": {"dateTime": "2024-01-01T10:30:00Z"},
            "status": "confirmed",
            "summary": "Planning",
            "updated": "2024-01-01T00:00:00.000Z",
        }
        calls: list[tuple[str, str, JSONObject | None]] = []

        def fake_google_request(method: str, url: str, access_token: str, *, body: JSONObject | None = None, failure_message: str, invalid_response_message: str) -> JSONObject:
            del failure_message, invalid_response_message, access_token
            calls.append((method, url, body))
            if method == "GET":
                return dict(event_state)
            return {"htmlLink": "https://calendar.example/event-1", "id": "event-1"}

        with (
            patch.object(google_shared, "get_google_userinfo", return_value=google_userinfo()),
            patch.object(google_calendar, "google_json_request", side_effect=fake_google_request),
        ):
            pending = tool.execute(
                "event_change",
                {"operation": "update", "event_id": "event-1", "summary": "Planning v2"},
                api,
            )

        self.assertIsInstance(pending, ActionPendingApproval)
        # The approval shows which meeting changes and what changes, and the
        # payload captures the event state for re-verification.
        self.assertEqual(
            pending.summary,
            'Update Google Calendar event "Planning" (starts 2024-01-01T10:00:00Z) [id event-1]: summary → Planning v2.',
        )
        approval = api.approvals.get(pending.approval_id)
        self.assertEqual(approval.payload["current_event"]["updated"], "2024-01-01T00:00:00.000Z")

        approved_record = api.approvals.approve(pending.approval_id)
        with (
            patch.object(google_shared, "get_google_userinfo", return_value=google_userinfo()),
            patch.object(google_calendar, "google_json_request", side_effect=fake_google_request),
        ):
            executed = tool.execute_approved(approved_record, api)
        self.assertIsInstance(executed, ApprovalExecuted)
        self.assertEqual(executed.message, "Updated Google Calendar event event-1.")
        self.assertEqual(calls[-1][0], "PATCH")

    def test_google_calendar_rejects_event_changed_after_approval(self) -> None:
        api = connected_google_api(google_calendar.MANIFEST.tool_id, google_calendar.REQUIRED_CALENDAR_SCOPES)
        tool = GoogleCalendarTool()
        event_state = {
            "id": "event-1",
            "start": {"dateTime": "2024-01-01T10:00:00Z"},
            "status": "confirmed",
            "summary": "Planning",
            "updated": "2024-01-01T00:00:00.000Z",
        }

        def fake_google_request(method: str, url: str, access_token: str, *, body: JSONObject | None = None, failure_message: str, invalid_response_message: str) -> JSONObject:
            del failure_message, invalid_response_message, access_token, body
            if method == "GET":
                return dict(event_state)
            raise AssertionError("a stale approval must not execute the write")

        with (
            patch.object(google_shared, "get_google_userinfo", return_value=google_userinfo()),
            patch.object(google_calendar, "google_json_request", side_effect=fake_google_request),
        ):
            pending = tool.execute("event_change", {"operation": "delete", "event_id": "event-1"}, api)
        self.assertIsInstance(pending, ActionPendingApproval)
        self.assertEqual(pending.summary, 'Delete Google Calendar event "Planning" (starts 2024-01-01T10:00:00Z) [id event-1].')

        event_state["updated"] = "2024-01-02T00:00:00.000Z"  # edited after approval was queued
        approved_record = api.approvals.approve(pending.approval_id)
        with (
            patch.object(google_shared, "get_google_userinfo", return_value=google_userinfo()),
            patch.object(google_calendar, "google_json_request", side_effect=fake_google_request),
        ):
            failed = tool.execute_approved(approved_record, api)
        self.assertIsInstance(failed, ActionFailed)
        self.assertEqual(failed.error, "Calendar event changed after approval. Please queue a new approval.")

    def test_google_calendar_write_rejects_changed_account_after_approval(self) -> None:
        api = connected_google_api(google_calendar.MANIFEST.tool_id, google_calendar.REQUIRED_CALENDAR_SCOPES)
        tool = GoogleCalendarTool()

        event_preview = {
            "id": "event-1",
            "start": {"dateTime": "2024-01-01T10:00:00Z"},
            "status": "confirmed",
            "summary": "Planning",
            "updated": "2024-01-01T00:00:00.000Z",
        }
        with (
            patch.object(google_shared, "get_google_userinfo", return_value=google_userinfo()),
            patch.object(google_calendar, "google_json_request", return_value=event_preview),
        ):
            pending = tool.execute(
                "event_change",
                {
                    "operation": "delete",
                    "event_id": "event-1",
                },
                api,
            )

        self.assertIsInstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)
        record = api.credentials.load()
        self.assertIsNotNone(record)
        record["account"]["label"] = "other@example.com"
        record["account"]["id"] = "google-sub-2"
        record["secret"]["access_token"] = "other-access-token"
        api.credentials.save(record)

        with (
            patch.object(google_shared, "get_google_userinfo", return_value=google_userinfo("google-sub-2", "other@example.com")),
            patch.object(google_calendar, "google_json_request", side_effect=AssertionError("approval must not execute")),
        ):
            executed = tool.execute_approved(approved_record, api)

        self.assertIsInstance(executed, ActionFailed)
        self.assertEqual(executed.error, "Google Calendar account changed after approval. Please queue a new approval.")

    def test_gmail_draft_send_summary_discloses_all_recipients_and_attachments(self) -> None:
        draft = {
            "to": "visible@example.com",
            "cc": "copied@example.com",
            "bcc": "hidden@example.com",
            "subject": "Quarterly report",
            "attachmentsOriginalCount": 2,
            "from": "me@x.io",
            "replyTo": "reply@x.io",
            "inReplyTo": "msg-1",
            "references": "ref-1",
            "threadId": "thread-1",
        }
        summary = gmail._draft_send_summary(draft)
        self.assertEqual(
            summary,
            'Send Gmail draft to visible@example.com, cc copied@example.com, bcc hidden@example.com '
            'with subject "Quarterly report" and 2 attachments; routing from me@x.io, '
            'reply-to reply@x.io, in-reply-to msg-1, refs ref-1, thread thread-1.',
        )
        self.assertEqual(
            gmail._draft_send_summary({"to": "a@b.c", "subject": "Hi"}),
            'Send Gmail draft to a@b.c with subject "Hi".',
        )
        # Fields are clipped individually, so an oversized To list can never
        # push the Cc/Bcc/attachment disclosures past the 500-byte limit.
        huge = gmail._draft_send_summary(
            {
                "to": ", ".join(f"user{i}@example.com" for i in range(60)),
                "cc": ", ".join(f"copy{i}@example.com" for i in range(40)),
                "bcc": "hidden@example.com",
                "subject": "S" * 300,
                "attachmentsOriginalCount": 3,
            }
        )
        self.assertLessEqual(len(huge.encode("utf-8")), 500)
        self.assertIn(", cc copy0@example.com", huge)
        self.assertIn(", bcc hidden@example.com", huge)
        self.assertIn("3 attachments", huge)
        # When the preview captured attachment names, the send summary names the
        # files (up to three, with a +N more marker) instead of only a count, so
        # the operator knows which files ride along.
        named = gmail._draft_send_summary(
            {
                "to": "a@b.c",
                "subject": "Hi",
                "attachmentsOriginalCount": 4,
                "attachments": [
                    {"filename": "invoice.pdf", "size": 1024},
                    {"filename": "budget.xlsx", "size": 2048},
                    {"filename": "notes.txt"},
                    {"filename": "extra.png"},
                ],
            }
        )
        self.assertLessEqual(len(named.encode("utf-8")), 500)
        self.assertIn("4 attachments (invoice.pdf, 1024 bytes, budget.xlsx, 2048 bytes, notes.txt, +1 more)", named)
        # Attachment names are best-effort: a worst-case draft (long recipients,
        # subject, routing, body, and three named attachments) must still fit the
        # 500-byte cap, falling back to the count so the send is never blocked.
        worst = gmail._draft_send_summary(
            {
                "to": ", ".join(f"user{i}@example.com" for i in range(60)),
                "cc": ", ".join(f"copy{i}@example.com" for i in range(40)),
                "bcc": ", ".join(f"blind{i}@example.com" for i in range(40)),
                "subject": "S" * 300,
                "from": "sender@example.com",
                "replyTo": "reply@example.com",
                "inReplyTo": "m" * 80,
                "references": "r" * 80,
                "threadId": "t" * 80,
                "body": "B" * 4000,
                "bodyTruncated": True,
                "bodyOriginalLength": 4000,
                "attachmentsOriginalCount": 3,
                "attachments": [
                    {"filename": "a" * 80, "size": 111111},
                    {"filename": "b" * 80, "size": 222222},
                    {"filename": "c" * 80, "size": 333333},
                ],
            }
        )
        self.assertLessEqual(len(worst.encode("utf-8")), 500)
        self.assertIn("3 attachments", worst)
        # The stored draft body (what an approved send actually sends) is
        # disclosed too, and stays within budget alongside everything else.
        with_body = gmail._draft_send_summary(
            {"to": "a@b.c", "subject": "Hi", "body": "The actual message body."}
        )
        self.assertIn('body (24 chars): "The actual message body."', with_body)
        noted = gmail._draft_send_summary({"to": "a@b.c", "subject": "Hi", "bodyNote": "Only the snippet is available."})
        self.assertIn("body preview unavailable: Only the snippet is available.", noted)
        # A shown body preview that understates what is sent (truncated to the
        # first bodyLimit chars, and/or an unrendered HTML alternative) must
        # keep both caveats, so the operator never approves a benign preview
        # while the full body or HTML part goes out.
        caveated = gmail._draft_send_summary(
            {
                "to": "a@b.c",
                "subject": "Hi",
                "body": "First part of a long message.",
                "bodyTruncated": True,
                "bodyOriginalLength": 9000,
                "bodyNote": "An HTML alternative part is not shown.",
            }
        )
        self.assertIn("(full body 9000 chars sent)", caveated)
        self.assertIn("An HTML alternative part is not shown.", caveated)
        # Worst case: every recipient field long, long subject, truncated body,
        # and a long note all at once. The mandatory disclosures (recipients,
        # attachments, truncation, note) stay present and the flexible body
        # preview is budgeted, so the summary is guaranteed within the cap.
        everything = gmail._draft_send_summary(
            {
                "to": ", ".join(f"toooo{i}@example.com" for i in range(60)),
                "cc": ", ".join(f"ccccc{i}@example.com" for i in range(60)),
                "bcc": ", ".join(f"bbbbb{i}@example.com" for i in range(60)),
                "subject": "S" * 300,
                "attachmentsOriginalCount": 9,
                "from": "sender@example.com",
                "replyTo": "reply-to@example.com",
                "inReplyTo": "message-1234567890",
                "references": "reference-1234567890",
                "threadId": "thread-1234567890",
                "body": "B" * 9000,
                "bodyTruncated": True,
                "bodyOriginalLength": 40000,
                "bodyNote": "N" * 300,
            }
        )
        self.assertLessEqual(len(everything.encode("utf-8")), 500)
        self.assertIn("bcc bbbbb0@example.com", everything)
        self.assertIn("body (9000 chars)", everything)
        self.assertIn("full body 40000 chars sent", everything)
        self.assertIn("routing from", everything)
        self.assertIn("reply-to", everything)
        self.assertIn("thread", everything)

    def test_gmail_draft_update_summary_worst_case_stays_under_limit(self) -> None:
        proposal = gmail._draft_action_proposal(
            {
                "action": "update",
                "draft_id": "d" * 300,
                "to": ", ".join(f"user{i}@example.com" for i in range(40)),
                "subject": "N" * 300,
                "blocks": [{"type": "paragraph", "text": "B" * 5000}],
            },
            existing_draft={
                "to": ", ".join(f"old{i}@example.com" for i in range(40)),
                "cc": ", ".join(f"copy{i}@example.com" for i in range(40)),
                "bcc": "h" * 300 + "@example.com",
                "subject": "O" * 300,
                "messageId": "message-1",
                "from": "sender@example.com",
                "replyTo": "reply-to@example.com",
                "inReplyTo": "message-1234567890",
                "references": "reference-1234567890",
                "threadId": "thread-1234567890",
            },
        )
        summary = str(proposal["summary"])
        self.assertLessEqual(len(summary.encode("utf-8")), 500)
        self.assertIn("keeps cc copy0@example.com", summary)
        self.assertIn("bcc h", summary)
        self.assertIn("from sender@example.com", summary)
        self.assertIn("reply-to reply-to@example.com", summary)
        self.assertIn("thread thread-1234567890", summary)
        self.assertIn("new body (5000 chars)", summary)

    def test_gmail_send_email_summary_is_clipped_for_long_fields(self) -> None:
        long_to = ", ".join(f"user{i}@example.com" for i in range(40))
        proposal = gmail._gmail_send_proposal(
            {"to": long_to, "subject": "S" * 300, "blocks": [{"type": "paragraph", "text": "Body"}]}
        )
        summary = str(proposal["summary"])
        self.assertLessEqual(len(summary.encode("utf-8")), 500)
        self.assertIn("Send Gmail message to user0@example.com", summary)
        # The composed body is disclosed in the visible summary, and the
        # exact untruncated draft still rides in the payload.
        self.assertIn('body (4 chars): "Body"', summary)
        self.assertEqual(proposal["draft"]["to"], long_to)

    def test_gmail_draft_update_summary_discloses_preserved_hidden_recipients(self) -> None:
        proposal = gmail._draft_action_proposal(
            {
                "action": "update",
                "draft_id": "draft-1",
                "to": "new@example.com",
                "subject": "New subject",
                "blocks": [{"type": "paragraph", "text": "Body"}],
            },
            existing_draft={
                "to": "old@example.com",
                "cc": "copied@example.com",
                "bcc": "hidden@example.com",
                "subject": "Old subject",
                "messageId": "message-1",
                "from": "sender@example.com",
                "replyTo": "reply-to@example.com",
                "threadId": "thread-1",
            },
        )
        self.assertEqual(
            proposal["summary"],
            'Update Gmail draft draft-1 from old@example.com / "Old subject" to new@example.com / '
            '"New subject" (keeps cc copied@example.com, bcc hidden@example.com, '
            'from sender@example.com, reply-to reply-to@example.com, thread thread-1); '
            'new body (4 chars): "Body".',
        )

    def test_gmail_message_action_summary_names_targets_and_labels(self) -> None:
        proposal = {
            "messages": [
                {"id": "m1", "subject": "Invoice #1042", "from": "billing@acme.dev"},
                {"id": "m2", "subject": "", "from": "noreply@acme.dev"},
            ],
            "labels": [{"id": "Label_7", "name": "Receipts"}],
        }
        self.assertEqual(
            gmail._message_action_summary("add_labels", proposal),
            'Add Labels 2 messages: "Invoice #1042" from billing@acme.dev; '
            '"(no subject)" from noreply@acme.dev (labels: Receipts).',
        )
        many = {
            "messages": [
                {"id": f"m{i}", "subject": f"Subject {i}", "from": f"sender{i}@example.com"} for i in range(5)
            ],
        }
        summary = gmail._message_action_summary("archive", many)
        self.assertIn("Archive 5 messages:", summary)
        self.assertIn("and 2 more", summary)
        self.assertLessEqual(len(summary.encode("utf-8")), 500)

    def test_gmail_label_action_summary_names_the_label(self) -> None:
        delete_proposal = {"labelId": "Label_7", "label": {"id": "Label_7", "name": "Receipts"}}
        self.assertEqual(
            gmail._label_action_summary("delete", delete_proposal),
            'Delete Gmail label "Receipts" (Label_7).',
        )
        update_proposal = {
            "labelId": "Label_7",
            "label": {"id": "Label_7", "name": "Receipts"},
            "name": "Archived receipts",
            "color": {"backgroundColor": "#cc3a21", "textColor": "#ffffff"},
        }
        self.assertEqual(
            gmail._label_action_summary("update", update_proposal),
            'Update Gmail label "Receipts" (Label_7): name → Archived receipts, color → #cc3a21/#ffffff.',
        )

    def test_gmail_label_create_summary_is_clipped(self) -> None:
        proposal = gmail._label_action_proposal({"action": "create", "name": "🏷" * 200})
        self.assertLessEqual(len(str(proposal["summary"]).encode("utf-8")), 500)
        self.assertTrue(str(proposal["summary"]).startswith('Create Gmail label "🏷'))

    def test_gmail_message_action_execution_rejects_renamed_labels(self) -> None:
        proposal = {"labels": [{"id": "Label_7", "name": "Receipts"}]}
        with patch.object(gmail, "gmail_label_summaries", return_value=[{"id": "Label_7", "name": "Renamed"}]) as summaries:
            with self.assertRaisesRegex(RuntimeError, "Gmail label changed after approval"):
                gmail._verify_message_action_labels("token", proposal)
        # A single labels.list-backed call covers every approved label, so the
        # verification never fans out to one blocking labels.get per label.
        summaries.assert_called_once_with("token", ["Label_7"])
        with patch.object(gmail, "gmail_label_summaries", return_value=[{"id": "Label_7", "name": "Receipts"}]):
            gmail._verify_message_action_labels("token", proposal)

    def test_gmail_deleted_draft_or_label_reports_stale_approval(self) -> None:
        # A 404 on the pre-execution verification fetch means the approved
        # object was deleted in between: report "changed after approval", not a
        # generic Gmail API failure. Non-404 failures pass through unchanged.
        def raise_gmail_404(*args: object) -> None:
            raise RuntimeError("Gmail API request failed.") from urllib.error.HTTPError("u", 404, "nf", None, None)  # type: ignore[arg-type]

        def raise_gmail_500(*args: object) -> None:
            raise RuntimeError("Gmail API request failed.") from urllib.error.HTTPError("u", 500, "err", None, None)  # type: ignore[arg-type]

        with patch.object(gmail, "gmail_draft_preview", side_effect=raise_gmail_404):
            with self.assertRaisesRegex(RuntimeError, "Gmail draft changed after approval"):
                gmail._verify_draft_matches_approval("token", "draft_1", {"messageId": "m1"})
        with patch.object(gmail, "gmail_draft_preview", side_effect=raise_gmail_500):
            with self.assertRaisesRegex(RuntimeError, "Gmail API request failed"):
                gmail._verify_draft_matches_approval("token", "draft_1", {"messageId": "m1"})
        with patch.object(gmail, "gmail_label_preview", side_effect=raise_gmail_404):
            with self.assertRaisesRegex(RuntimeError, "Gmail label changed after approval"):
                gmail._verify_label_matches_approval("token", {"label": {"id": "L1", "name": "x"}, "labelId": "L1"})

    def test_gmail_thread_truncation_reports_index_counts(self) -> None:
        messages = [{"id": f"m{i}", "threadId": "t1"} for i in range(105)]
        with patch.object(gmail, "execute_gmail_api_request", return_value={"id": "t1", "messages": messages, "attacker": {"nested": "payload"}}):
            result = gmail.GmailTool()._execute_read("read_thread", {"thread_id": "t1"}, "token")
        thread = cast(dict, result["thread"])
        self.assertEqual(len(cast(list, thread["messages"])), 100)
        self.assertTrue(thread["messageIndexTruncated"])
        self.assertEqual(thread["messageIndexLimit"], 100)
        self.assertEqual(thread["messageIndexOriginalCount"], 105)
        self.assertEqual(thread["messageIndexOmittedCount"], 5)
        self.assertNotIn("attacker", thread)

    def test_gmail_provider_lists_cannot_trigger_unbounded_followup_reads(self) -> None:
        message_calls: list[str] = []

        def fake_message_request(access_token: str, request: JSONObject) -> JSONObject:
            del access_token
            operation = cast(str, request["operation"])
            message_calls.append(operation)
            if operation == "users.messages.list":
                return {"messages": [{"id": f"m{index}"} for index in range(100)]}
            return {"id": "message", "threadId": "thread"}

        with patch.object(gmail_api, "execute_gmail_api_request", side_effect=fake_message_request):
            summaries = gmail_api.gmail_search_messages("token", "query")
        self.assertEqual(len(summaries), gmail_api.GMAIL_READ_MAX_RESULTS)
        self.assertEqual(message_calls.count("users.messages.get"), gmail_api.GMAIL_READ_MAX_RESULTS)

        with (
            patch.object(
                gmail,
                "execute_gmail_api_request",
                return_value={"drafts": [{"id": f"d{index}"} for index in range(100)]},
            ),
            patch.object(gmail, "gmail_draft_preview", return_value={"draftId": "d"}) as preview,
        ):
            result = gmail.GmailTool()._execute_read("list_drafts", {}, "token")
        drafts = cast(dict, result["drafts"])
        self.assertEqual(len(cast(list, drafts["drafts"])), gmail.DEFAULT_DRAFT_PAGE_LIMIT)
        self.assertEqual(preview.call_count, gmail.DEFAULT_DRAFT_PAGE_LIMIT)

    def test_calendar_create_summary_includes_all_accepted_fields(self) -> None:
        proposal = {
            "operation": "create",
            "summary": "Design review",
            "start_time": "2024-01-01T12:00:00Z",
            "end_time": "2024-01-01T12:30:00Z",
            "location": "Room 4",
            "description": "Quarterly design review",
            "time_zone": "Europe/Berlin",
        }
        summary = google_calendar._calendar_summary(proposal, None)
        self.assertEqual(
            summary,
            'Create Google Calendar event "Design review" from 2024-01-01T12:00:00Z to '
            "2024-01-01T12:30:00Z (location Room 4, description Quarterly design review, "
            "time_zone Europe/Berlin).",
        )
        plain = google_calendar._calendar_summary(
            {"operation": "create", "summary": "Standup", "start_time": "S", "end_time": "E"}, None
        )
        self.assertEqual(plain, 'Create Google Calendar event "Standup" from S to E.')

    def test_calendar_update_can_clear_description_and_location(self) -> None:
        # An update may set description/location to "" to clear them, but every
        # other field (and clearing on create) still requires a non-empty string.
        proposal = google_calendar._calendar_change_proposal(
            {"operation": "update", "event_id": "evt-1", "location": "", "description": ""}
        )
        self.assertEqual(proposal["location"], "")
        self.assertEqual(proposal["description"], "")
        with self.assertRaises(google_calendar.ToolInputValidationError):
            google_calendar._calendar_change_proposal(
                {"operation": "update", "event_id": "evt-1", "summary": ""}
            )
        with self.assertRaises(google_calendar.ToolInputValidationError):
            google_calendar._calendar_change_proposal(
                {"operation": "create", "summary": "x", "start_time": "S", "end_time": "E", "location": ""}
            )

    def test_calendar_change_summary_discloses_id_recurrence_and_guest_context(self) -> None:
        preview = {
            "summary": "Team sync", "start_time": "2024-01-01T10:00:00Z", "event_id": "evt-123",
            "recurring": True, "is_guest": True, "attendee_count": 5, "location": "Room 4",
        }
        summary = google_calendar._calendar_summary(
            {"operation": "update", "event_id": "evt-123", "summary": "Team sync v2"}, preview
        )
        for fragment in ("id evt-123", "recurring event", "you are a guest", "5 guests", "location Room 4"):
            self.assertIn(fragment, summary)
        # A worst-case update (every field long, full context) still fits the cap,
        # dropping location and clipping change previews as needed.
        worst = google_calendar._calendar_summary(
            {"operation": "update", "event_id": "e", "summary": "S" * 200, "description": "D" * 200,
             "location": "L" * 200, "start_time": "T" * 200, "end_time": "E" * 200, "time_zone": "Z" * 200},
            {"summary": "T" * 200, "start_time": "X" * 200, "event_id": "i" * 200, "recurring": True,
             "is_guest": True, "attendee_count": 9, "location": "Y" * 200},
        )
        self.assertLessEqual(len(worst.encode("utf-8")), 500)
        # The mandatory safety flags survive even in the clipped form.
        self.assertIn("recurring event", worst)
        self.assertIn("you are a guest", worst)

    def test_gmail_draft_delete_summary_is_clipped(self) -> None:
        proposal = gmail._draft_action_proposal(
            {"action": "delete", "draft_id": "draft-1"},
            existing_draft={
                "to": ", ".join(f"user{i}@example.com" for i in range(40)),
                "subject": "S" * 300,
                "messageId": "message-1",
            },
        )
        summary = str(proposal["summary"])
        self.assertLessEqual(len(summary.encode("utf-8")), 500)
        self.assertIn("Delete Gmail draft to user0@example.com", summary)

    def test_gmail_draft_delete_summary_discloses_body_and_attachments(self) -> None:
        # Two drafts with the same To/Subject must be distinguishable in the
        # delete approval, so the summary discloses the body preview and the
        # attachment count.
        proposal = gmail._draft_action_proposal(
            {"action": "delete", "draft_id": "draft-1"},
            existing_draft={
                "to": "user@example.com",
                "subject": "Invoice",
                "body": "Please wire the funds to account 12345 today.",
                "attachmentsOriginalCount": 2,
                "messageId": "message-1",
            },
        )
        summary = str(proposal["summary"])
        self.assertLessEqual(len(summary.encode("utf-8")), 500)
        self.assertIn("2 attachments", summary)
        self.assertIn("wire the funds", summary)

    def test_gmail_draft_delete_summary_discloses_body_caveats(self) -> None:
        # A truncated preview and an unavailable (HTML-only/no-snippet) body are
        # exactly the content that distinguishes two same-recipient drafts, so
        # the delete summary must disclose the caveat, not silently drop it.
        truncated = gmail._draft_action_proposal(
            {"action": "delete", "draft_id": "d1"},
            existing_draft={"to": "u@x.io", "subject": "Report", "body": "Short preview", "bodyTruncated": True, "bodyOriginalLength": 5000, "messageId": "m1"},
        )
        self.assertIn("full body 5000 chars", str(truncated["summary"]))
        html_only = gmail._draft_action_proposal(
            {"action": "delete", "draft_id": "d2"},
            existing_draft={"to": "u@x.io", "subject": "Report", "bodyNote": "HTML body not rendered here.", "messageId": "m2"},
        )
        self.assertIn("body preview unavailable: HTML body not rendered here.", str(html_only["summary"]))

    def test_gmail_html_only_message_without_snippet_warns_instead_of_no_body(self) -> None:
        # An HTML-only draft/message with no snippet still has content that will
        # be sent, so the note must warn about the hidden HTML rather than
        # claiming no body was available.
        html_only = gmail_api.gmail_message_body({"payload": {"mimeType": "text/html", "body": {"data": ""}}})
        self.assertEqual(html_only.text, "")
        self.assertEqual(html_only.note, gmail_api.GMAIL_HTML_ONLY_NO_SNIPPET_NOTE)
        # A message with neither a body nor HTML nor snippet still reports no body.
        empty = gmail_api.gmail_message_body({"payload": {"mimeType": "text/plain", "body": {}}})
        self.assertEqual(empty.note, gmail_api.GMAIL_NO_BODY_NOTE)

    def test_gmail_message_body_concatenates_all_plaintext_parts(self) -> None:
        # A multipart body with more than one text/plain part must surface every
        # part, so a later segment cannot be hidden from the operator.
        def _plain_part(text: str) -> dict[str, Any]:
            data = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
            return {"mimeType": "text/plain", "body": {"data": data}}

        body = gmail_api.gmail_message_body(
            {"payload": {"mimeType": "multipart/mixed", "parts": [_plain_part("First part."), _plain_part("Second part.")]}}
        )
        self.assertEqual(body.source, "text/plain")
        self.assertIn("First part.", body.text)
        self.assertIn("Second part.", body.text)

    def test_gmail_message_body_reads_inline_text_not_as_attachment(self) -> None:
        # An inline text/plain part (Content-Disposition: inline, no filename or
        # attachmentId) is body text, not an attachment, so it must be decoded.
        data = base64.urlsafe_b64encode(b"Inline body text.").decode("ascii")
        body = gmail_api.gmail_message_body({
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Content-Disposition", "value": "inline"}],
                "body": {"data": data},
            }
        })
        self.assertEqual(body.source, "text/plain")
        self.assertIn("Inline body text.", body.text)

    def test_google_api_401_surfaces_the_reconnect_flow(self) -> None:
        api = connected_google_api(gmail.MANIFEST.tool_id, gmail.REQUIRED_GMAIL_SCOPES)
        with patch.object(
            google_shared,
            "json_request",
            side_effect=WebRequestError("unauthorized", status=401),
        ):
            result = GmailTool().execute("list_labels", {}, api)
        self.assertIsInstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertIn("reconnect", result.error.lower())

    def test_google_userinfo_401_surfaces_the_reconnect_flow(self) -> None:
        api = connected_google_api(gmail.MANIFEST.tool_id, gmail.REQUIRED_GMAIL_SCOPES)
        with patch.object(
            google_shared,
            "json_request",
            side_effect=WebRequestError("unauthorized", status=401),
        ):
            # Write proposals refresh the bound identity first, so the
            # revoked token must surface reconnect_required here too.
            result = GmailTool().execute(
                "send_email",
                {"to": "a@b.c", "subject": "Hi", "blocks": [{"type": "paragraph", "text": "Body"}]},
                api,
            )
        self.assertIsInstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)

    def test_tools_package_has_no_third_party_imports(self) -> None:
        # Tool packages must run anywhere the host runtime runs: standard
        # library only, plus the tools package itself (see the Testing
        # section of docs/architecture/tools/README.md).
        import ast
        import sys
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        allowed_roots = set(sys.stdlib_module_names) | {"tools"}
        offenders: list[str] = []
        for path in sorted((repo_root / "tools").rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots = [node.module.split(".")[0]]
                else:
                    continue
                for root in roots:
                    if root not in allowed_roots:
                        offenders.append(f"{path.relative_to(repo_root)}: {root}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
