"""Unit tests for the LinkedIn tool package (all third-party calls mocked)."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from host.tools.json_types import JSONObject
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools import linkedin
from host.tools.linkedin import LinkedInTool
from host.tools.shared.web import WebRequestError

from test_tools import FakeHostAPI, FRESH_EXPIRES_AT

USERINFO: JSONObject = {
    "sub": "AbC123",
    "name": "Claw Dev",
    "email": "claw@example.com",
    "email_verified": True,
    "picture": "https://media.licdn.com/profile.jpg",
}


def connected_api(*, expires_at: int = FRESH_EXPIRES_AT) -> FakeHostAPI:
    api = FakeHostAPI()
    api.config["LINKEDIN_OAUTH_CLIENT_ID"] = "li-client"
    api.config["LINKEDIN_OAUTH_CLIENT_SECRET"] = "li-secret"
    api.credentials.save(
        {
            "account": {"id": "AbC123", "label": "claw@example.com", "scopes": ["openid", "profile", "email", "w_member_social"]},
            "secret": {"access_token": "li-access", "expires_at": expires_at},
            "metadata": {"created_at": 1, "updated_at": 1},
        }
    )
    return api


class LinkedInToolTests(unittest.TestCase):
    def test_manifest_shape(self) -> None:
        tool = LinkedInTool()
        self.assertEqual(tool.manifest.connection, "oauth")
        self.assertEqual(
            [spec.id for spec in tool.manifest.actions],
            ["get_profile", "create_post"],
        )
        self.assertIn("personal LinkedIn profile", tool.manifest.description)
        self.assertEqual(len(tool.manifest.setup_steps), 6)
        step_titles = [step.title for step in tool.manifest.setup_steps]
        self.assertEqual(step_titles[0], "Understand why you need a LinkedIn Page")
        self.assertIn(
            "name a LinkedIn Page as its publisher",
            tool.manifest.setup_steps[0].description,
        )
        self.assertIn("does not add the Page", tool.manifest.setup_steps[0].description)
        self.assertNotIn("Publish a simple privacy policy", step_titles)
        self.assertNotIn("Understand who can connect", step_titles)

    def test_get_profile_returns_identity(self) -> None:
        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            self.assertEqual(url, linkedin.LINKEDIN_USERINFO_URL)
            return dict(USERINFO)

        with patch.object(linkedin, "json_request", fake_json_request):
            result = LinkedInTool().execute("get_profile", {}, connected_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["member_id"], "AbC123")
        self.assertEqual(result.result["email"], "claw@example.com")
        self.assertEqual(result.result["picture"], "https://media.licdn.com/profile.jpg")

    def test_expired_token_requires_reconnect(self) -> None:
        api = connected_api(expires_at=1)
        result = LinkedInTool().execute("get_profile", {}, api)
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertIn("60-day", result.error)
        self.assertEqual(LinkedInTool().credentials.connection_status(api), {"connected": False})

    def test_create_post_queues_approval(self) -> None:
        api = connected_api()

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if url == linkedin.LINKEDIN_USERINFO_URL:
                return dict(USERINFO)
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(linkedin, "json_request", fake_json_request):
            result = LinkedInTool().execute("create_post", {"text": "Big launch today"}, api)
        assert isinstance(result, ActionPendingApproval)
        self.assertIn("Post on LinkedIn as claw@example.com (PUBLIC)", result.summary)
        self.assertIn("Big launch today", result.summary)
        record = api.approvals.get(result.approval_id)
        assert record is not None
        proposal = record.payload["proposal"]
        assert isinstance(proposal, dict)
        self.assertEqual(proposal["visibility"], "PUBLIC")

    def test_create_post_validates_input(self) -> None:
        tool = LinkedInTool()
        for bad_input in ({}, {"text": " "}, {"text": "x", "visibility": "FRIENDS"}, {"text": "x", "extra": 1}):
            self.assertIsInstance(tool.execute("create_post", bad_input, connected_api()), ActionFailed, bad_input)

    def test_execute_approved_posts_with_escaped_text_and_reports_id(self) -> None:
        api = connected_api()

        def fake_pending(method: str, url: str, **kwargs: Any) -> JSONObject:
            if url == linkedin.LINKEDIN_USERINFO_URL:
                return dict(USERINFO)
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(linkedin, "json_request", fake_pending):
            pending = LinkedInTool().execute("create_post", {"text": "Check (this) #launch"}, api)
        assert isinstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)

        posted: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if url == linkedin.LINKEDIN_USERINFO_URL:
                return dict(USERINFO)
            raise AssertionError(f"unexpected call: {url}")

        def fake_json_request_with_headers(method: str, url: str, **kwargs: Any) -> tuple[JSONObject, dict[str, str]]:
            posted["url"] = url
            posted["body"] = kwargs["body"]
            posted["headers"] = kwargs["headers"]
            return {}, {"x-restli-id": "urn:li:share:777"}

        with patch.object(linkedin, "json_request", fake_json_request), patch.object(
            linkedin, "json_request_with_headers", fake_json_request_with_headers
        ):
            result = LinkedInTool().execute_approved(approved_record, api)
        assert isinstance(result, ApprovalExecuted)
        self.assertIn("urn:li:share:777", result.message)
        self.assertEqual(posted["url"], linkedin.LINKEDIN_POSTS_URL)
        body = posted["body"]
        self.assertEqual(body["author"], "urn:li:person:AbC123")
        self.assertEqual(body["commentary"], "Check \\(this\\) \\#launch")
        self.assertEqual(posted["headers"]["linkedin-version"], linkedin.LINKEDIN_VERSION)
        self.assertEqual(posted["headers"]["x-restli-protocol-version"], "2.0.0")

    def test_execute_approved_fails_when_account_changed(self) -> None:
        api = connected_api()

        def fake_pending(method: str, url: str, **kwargs: Any) -> JSONObject:
            if url == linkedin.LINKEDIN_USERINFO_URL:
                return dict(USERINFO)
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(linkedin, "json_request", fake_pending):
            pending = LinkedInTool().execute("create_post", {"text": "x"}, api)
        assert isinstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)

        def fake_other_account(method: str, url: str, **kwargs: Any) -> JSONObject:
            return {"sub": "Other999", "name": "Other"}

        with patch.object(linkedin, "json_request", fake_other_account):
            result = LinkedInTool().execute_approved(approved_record, api)
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)

    def test_execute_approved_maps_revoked_token_to_reconnect(self) -> None:
        api = connected_api()
        with patch.object(linkedin, "json_request", return_value=dict(USERINFO)):
            pending = LinkedInTool().execute("create_post", {"text": "x"}, api)
        assert isinstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)

        with patch.object(
            linkedin,
            "json_request",
            side_effect=WebRequestError("profile lookup failed", status=401),
        ):
            result = LinkedInTool().execute_approved(approved_record, api)
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertIn("reconnect", result.error.lower())

    def test_forbidden_maps_to_product_hint(self) -> None:
        api = connected_api()

        def fake_pending(method: str, url: str, **kwargs: Any) -> JSONObject:
            if url == linkedin.LINKEDIN_USERINFO_URL:
                return dict(USERINFO)
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(linkedin, "json_request", fake_pending):
            pending = LinkedInTool().execute("create_post", {"text": "x"}, api)
        assert isinstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)

        def fake_forbidden(method: str, url: str, **kwargs: Any) -> tuple[JSONObject, dict[str, str]]:
            raise WebRequestError("failed", status=403)

        with patch.object(linkedin, "json_request", fake_pending), patch.object(
            linkedin, "json_request_with_headers", fake_forbidden
        ):
            result = LinkedInTool().execute_approved(approved_record, api)
        assert isinstance(result, ActionFailed)
        self.assertIn("forbidden", result.error)


class LinkedInCredentialFlowTests(unittest.TestCase):
    def test_complete_connect_saves_sixty_day_token(self) -> None:
        api = FakeHostAPI()
        api.config["LINKEDIN_OAUTH_CLIENT_ID"] = "li-client"
        api.config["LINKEDIN_OAUTH_CLIENT_SECRET"] = "li-secret"
        flow = LinkedInTool().credentials
        start = flow.start_connect({"redirect_uri": "https://host.example/cb"}, api)
        self.assertTrue(start["authorization_url"].startswith("https://www.linkedin.com/oauth/v2/authorization?"))

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if url == linkedin.LINKEDIN_TOKEN_URL:
                self.assertEqual(kwargs["form"]["grant_type"], "authorization_code")
                return {"access_token": "li-access", "expires_in": 5_183_999,
                        "scope": "openid,profile,email,w_member_social"}
            if url == linkedin.LINKEDIN_USERINFO_URL:
                return dict(USERINFO)
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(linkedin, "json_request", fake_json_request):
            result = flow.complete_connect(
                {"code": "auth-code", "state": start["state"], "redirect_uri": "https://host.example/cb"}, api
            )
        self.assertEqual(result["account"]["id"], "AbC123")
        stored = api.credentials.load()
        assert stored is not None
        self.assertEqual(stored["secret"]["access_token"], "li-access")
        self.assertNotIn("refresh_token", stored["secret"])

    def test_complete_connect_rejects_any_missing_requested_scope(self) -> None:
        api = FakeHostAPI()
        api.config["LINKEDIN_OAUTH_CLIENT_ID"] = "li-client"
        api.config["LINKEDIN_OAUTH_CLIENT_SECRET"] = "li-secret"
        flow = LinkedInTool().credentials
        start = flow.start_connect({"redirect_uri": "https://host.example/cb"}, api)

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            return {"access_token": "li-access", "expires_in": 5_183_999, "scope": "openid,profile,email"}

        with patch.object(linkedin, "json_request", fake_json_request):
            with self.assertRaisesRegex(RuntimeError, "missing required permissions"):
                flow.complete_connect(
                    {"code": "auth-code", "state": start["state"], "redirect_uri": "https://host.example/cb"}, api
                )

        for scopes in (
            "profile,email,w_member_social",
            "openid,email,w_member_social",
            "openid,profile,w_member_social",
        ):
            with self.subTest(scopes=scopes), patch.object(
                linkedin,
                "json_request",
                return_value={"access_token": "li-access", "expires_in": 5_183_999, "scope": scopes},
            ):
                with self.assertRaisesRegex(RuntimeError, "missing required permissions"):
                    flow.complete_connect(
                        {"code": "auth-code", "state": start["state"], "redirect_uri": "https://host.example/cb"}, api
                    )


if __name__ == "__main__":
    unittest.main()
