"""Unit tests for the Instagram tool package (all third-party calls mocked)."""

from __future__ import annotations

from dataclasses import replace
import unittest
from typing import Any
from unittest.mock import patch

from host.tools.json_types import JSONObject
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools import instagram
from host.tools.instagram import InstagramTool
from host.tools.shared.web import WebRequestError

from test_tools import FakeHostAPI, FRESH_EXPIRES_AT

ME_RESPONSE: JSONObject = {
    "user_id": "17841400000000000",
    "username": "clawcreates",
    "account_type": "BUSINESS",
    "followers_count": 1234,
    "media_count": 56,
}


def connected_api(*, expires_at: int = FRESH_EXPIRES_AT, obtained_at: int = FRESH_EXPIRES_AT - 10_000_000) -> FakeHostAPI:
    api = FakeHostAPI()
    api.config["INSTAGRAM_APP_ID"] = "ig-app"
    api.config["INSTAGRAM_APP_SECRET"] = "ig-secret"
    api.credentials.save(
        {
            "account": {"id": "17841400000000000", "label": "@clawcreates", "scopes": list(instagram.IG_OAUTH_SCOPES)},
            "secret": {"access_token": "ig-access", "expires_at": expires_at, "obtained_at": obtained_at},
            "metadata": {"created_at": 1, "updated_at": 1},
        }
    )
    return api


class InstagramReadTests(unittest.TestCase):
    def test_manifest_shape(self) -> None:
        tool = InstagramTool()
        self.assertEqual(tool.manifest.connection, "oauth")
        self.assertEqual(
            [spec.id for spec in tool.manifest.actions],
            ["get_profile", "get_recent_media", "get_publishing_limit", "post_reel"],
        )

    def test_get_profile_maps_fields(self) -> None:
        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            self.assertIn("/me?", url)
            self.assertIn("access_token=ig-access", url)
            return dict(ME_RESPONSE)

        with patch.object(instagram, "json_request", fake_json_request):
            result = InstagramTool().execute("get_profile", {}, connected_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["username"], "clawcreates")
        self.assertEqual(result.result["followers_count"], 1234)

    def test_provider_account_id_must_be_numeric_before_path_use(self) -> None:
        with patch.object(instagram, "json_request", return_value={"user_id": "../media", "username": "bad"}):
            result = InstagramTool().execute("get_publishing_limit", {}, connected_api())
        assert isinstance(result, ActionFailed)
        self.assertIn("stable account id", result.error)

    def test_get_recent_media_maps_engagement(self) -> None:
        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            self.assertIn("/me/media?", url)
            self.assertIn("limit=5", url)
            return {"data": [{"id": "1", "media_type": "VIDEO", "media_product_type": "REELS",
                              "caption": "hello", "permalink": "https://instagram.com/p/x",
                              "timestamp": "2026-07-01T00:00:00+0000", "like_count": 10, "comments_count": 2}]}

        with patch.object(instagram, "json_request", fake_json_request):
            result = InstagramTool().execute("get_recent_media", {"limit": "5"}, connected_api())
        assert isinstance(result, ActionExecuted)
        media = result.result["media"]
        assert isinstance(media, list)
        item = media[0]
        assert isinstance(item, dict)
        self.assertEqual(item["like_count"], 10)
        self.assertEqual(item["product_type"], "REELS")

    def test_get_recent_media_enforces_limit_on_provider_response(self) -> None:
        provider_items = [{"id": str(index)} for index in range(10)]
        with patch.object(instagram, "json_request", return_value={"data": provider_items}):
            result = InstagramTool().execute("get_recent_media", {"limit": "2"}, connected_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(len(result.result["media"]), 2)

    def test_read_limit_is_rejected_instead_of_silently_clamped(self) -> None:
        for value in ("0", "26", "9" * 100, "²"):
            with self.subTest(value=value):
                result = InstagramTool().execute("get_recent_media", {"limit": value}, connected_api())
                self.assertIsInstance(result, ActionFailed)

    def test_expired_token_requires_reconnect(self) -> None:
        result = InstagramTool().execute("get_profile", {}, connected_api(expires_at=1))
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)

    def test_near_expiry_token_refreshes_in_place(self) -> None:
        current = instagram.now()
        api = connected_api(expires_at=current + 3 * 24 * 3600, obtained_at=current - 30 * 24 * 3600)

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "refresh_access_token" in url:
                return {"access_token": "ig-access-2", "expires_in": 5_184_000}
            if "/me?" in url:
                return dict(ME_RESPONSE)
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(instagram, "json_request", fake_json_request):
            result = InstagramTool().execute("get_profile", {}, api)
        self.assertIsInstance(result, ActionExecuted)
        stored = api.credentials.load()
        assert stored is not None
        self.assertEqual(stored["secret"]["access_token"], "ig-access-2")

    def test_meta_error_code_190_maps_to_reconnect(self) -> None:
        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            raise WebRequestError("failed", status=400, body=b'{"error": {"code": 190, "message": "x"}}')

        with patch.object(instagram, "json_request", fake_json_request):
            result = InstagramTool().execute("get_profile", {}, connected_api())
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)


class InstagramReelTests(unittest.TestCase):
    def test_post_reel_description_uses_workspace_language(self) -> None:
        action = next(action for action in instagram.MANIFEST.actions if action.id == "post_reel")
        self.assertIn("video from the agent workspace", action.description)
        self.assertNotIn("stage_video", action.description)
        self.assertNotIn("video_url", action.input_schema["properties"])
        self.assertEqual(action.input_schema["required"], ["video_asset_id"])

    def test_post_reel_rejects_overlong_caption(self) -> None:
        api = connected_api()
        asset_id = api.assets.add(filename="reel.mp4", media_type="video/mp4", data=b"video")
        result = InstagramTool().execute(
            "post_reel",
            {"video_asset_id": asset_id, "caption": "x" * (instagram.MAX_CAPTION_CHARS + 1)},
            api,
        )
        assert isinstance(result, ActionFailed)
        self.assertIn("at most", result.error)

    def test_reel_summary_bounds_provider_account_label(self) -> None:
        summary = instagram._reel_summary(
            {
                "caption": "caption",
                "share_to_feed": True,
                "video_asset": {"filename": "reel.mp4", "size_bytes": 5, "sha256": "a" * 64},
            },
            "@" + "界" * 1_000,
        )
        self.assertLessEqual(len(summary.encode("utf-8")), 500)

    def test_post_reel_stages_asset_metadata_then_uploads_only_after_approval(self) -> None:
        api = connected_api()
        asset_id = api.assets.add(filename="final.mov", media_type="video/quicktime", data=b"video")

        def fake_pending(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/me?" in url:
                return dict(ME_RESPONSE)
            raise AssertionError(f"no video request before approval: {url}")

        with (
            patch.object(instagram, "json_request", fake_pending),
            patch.object(
                instagram,
                "stream_request_bytes",
                side_effect=AssertionError("video must not upload before approval"),
            ),
        ):
            pending = InstagramTool().execute(
                "post_reel", {"video_asset_id": asset_id, "caption": "Local"}, api
            )
        assert isinstance(pending, ActionPendingApproval)
        self.assertIn("final.mov", pending.summary)
        self.assertIn("SHA-256", pending.summary)
        record = api.approvals.approve(pending.approval_id)

        calls: list[str] = []

        def fake_execute(method: str, url: str, **kwargs: Any) -> JSONObject:
            calls.append(url)
            if "/me?" in url:
                return dict(ME_RESPONSE)
            if "/media?" in url and method == "POST":
                self.assertIn("upload_type=resumable", url)
                self.assertNotIn("video_url=", url)
                return {"id": "9990002", "uri": "https://rupload.facebook.com/ig-api-upload/x"}
            if "/9990002?" in url:
                return {"status_code": "FINISHED"}
            if "/media_publish?" in url:
                return {"id": "180000001"}
            raise AssertionError(url)

        streamed: dict[str, Any] = {}

        def fake_stream(method: str, url: str, **kwargs: Any) -> bytes:
            streamed.update(method=method, url=url, **kwargs)
            self.assertEqual(kwargs["body"].read(), b"video")
            return b""

        with (
            patch.object(instagram, "json_request", fake_execute),
            patch.object(instagram, "stream_request_bytes", fake_stream),
        ):
            result = InstagramTool().execute_approved(record, api)
        assert isinstance(result, ApprovalExecuted)
        self.assertEqual(streamed["url"], "https://rupload.facebook.com/ig-api-upload/x")
        self.assertEqual(streamed["headers"]["Authorization"], "OAuth ig-access")
        self.assertEqual(streamed["headers"]["offset"], "0")
        # The agent-facing workspace file remains; this internal approval spool
        # copy is consumed after Meta accepts it.
        self.assertNotIn(asset_id, api.assets.records)

    def test_post_reel_validates_input(self) -> None:
        tool = InstagramTool()
        api = connected_api()
        asset_id = api.assets.add(filename="reel.mp4", media_type="video/mp4", data=b"video")
        for bad_input in (
            {},
            {"video_url": "http://x.example/v.mp4"},
            {"video_url": "https://x.example/v.mp4", "video_asset_id": asset_id},
            {"video_asset_id": 123},
            {"video_asset_id": asset_id, "share_to_feed": "yes"},
        ):
            self.assertIsInstance(tool.execute("post_reel", bad_input, api), ActionFailed, bad_input)

        image_asset_id = api.assets.add(
            asset_id="asset_imageabcdefghijklmnopqrstuv123456",
            filename="frame.png",
            media_type="image/png",
            data=b"image",
        )
        wrong_type = tool.execute("post_reel", {"video_asset_id": image_asset_id}, api)
        assert isinstance(wrong_type, ActionFailed)
        self.assertIn("MP4 or MOV", wrong_type.error)

    def test_execute_approved_rejects_non_meta_upload_origin_before_sending_token(self) -> None:
        api = connected_api()
        asset_id = api.assets.add(filename="reel.mp4", media_type="video/mp4", data=b"video")

        with patch.object(instagram, "json_request", return_value=dict(ME_RESPONSE)):
            pending = InstagramTool().execute("post_reel", {"video_asset_id": asset_id}, api)
        assert isinstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)

        def fake_execute(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/me?" in url:
                return dict(ME_RESPONSE)
            if "/media?" in url:
                return {"id": "9990001", "uri": "https://attacker.example/upload"}
            raise AssertionError(url)

        with (
            patch.object(instagram, "json_request", fake_execute),
            patch.object(instagram, "stream_request_bytes") as stream,
        ):
            result = InstagramTool().execute_approved(approved_record, api)
        assert isinstance(result, ActionFailed)
        self.assertIn("resumable video upload URI", result.error)
        stream.assert_not_called()
        self.assertIn(asset_id, api.assets.records)

    def test_execute_approved_creates_polls_and_publishes(self) -> None:
        api = connected_api()
        asset_id = api.assets.add(filename="reel.mp4", media_type="video/mp4", data=b"video")

        def fake_pending(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/me?" in url:
                return dict(ME_RESPONSE)
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(instagram, "json_request", fake_pending):
            pending = InstagramTool().execute(
                "post_reel", {"video_asset_id": asset_id, "caption": "Go"}, api
            )
        assert isinstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)

        calls: list[str] = []

        def fake_execute(method: str, url: str, **kwargs: Any) -> JSONObject:
            calls.append(f"{method} {url}")
            if "/me?" in url:
                return dict(ME_RESPONSE)
            if "/media?" in url and method == "POST":
                self.assertIn("media_type=REELS", url)
                self.assertIn("upload_type=resumable", url)
                self.assertNotIn("video_url=", url)
                return {"id": "9990001", "uri": "https://rupload.facebook.com/ig-api-upload/x"}
            if "/9990001?" in url:
                return {"status_code": "IN_PROGRESS"} if len([c for c in calls if "/9990001?" in c]) == 1 else {"status_code": "FINISHED"}
            if "/media_publish?" in url and method == "POST":
                self.assertIn("creation_id=9990001", url)
                return {"id": "180000000"}
            raise AssertionError(f"unexpected call: {url}")

        with (
            patch.object(instagram, "json_request", fake_execute),
            patch.object(instagram, "stream_request_bytes", return_value=b""),
            patch.object(instagram.time, "sleep"),
        ):
            result = InstagramTool().execute_approved(approved_record, api)
        assert isinstance(result, ApprovalExecuted)
        self.assertIn("180000000", result.message)

    def test_execute_approved_rejects_non_reel_action(self) -> None:
        api = connected_api()
        asset_id = api.assets.add(filename="reel.mp4", media_type="video/mp4", data=b"video")
        with patch.object(instagram, "json_request", return_value=dict(ME_RESPONSE)):
            pending = InstagramTool().execute("post_reel", {"video_asset_id": asset_id}, api)
        assert isinstance(pending, ActionPendingApproval)
        approved_record = replace(api.approvals.approve(pending.approval_id), action_id="get_profile")

        with patch.object(instagram, "json_request") as request:
            result = InstagramTool().execute_approved(approved_record, api)

        assert isinstance(result, ActionFailed)
        self.assertEqual(result.error, "Instagram approval action is invalid.")
        request.assert_not_called()
        self.assertIn(asset_id, api.assets.records)

    def test_execute_approved_fails_on_container_error(self) -> None:
        api = connected_api()
        asset_id = api.assets.add(filename="reel.mp4", media_type="video/mp4", data=b"video")

        def fake_pending(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/me?" in url:
                return dict(ME_RESPONSE)
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(instagram, "json_request", fake_pending):
            pending = InstagramTool().execute("post_reel", {"video_asset_id": asset_id}, api)
        assert isinstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)

        def fake_execute(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/me?" in url:
                return dict(ME_RESPONSE)
            if "/media?" in url:
                return {"id": "9990001", "uri": "https://rupload.facebook.com/ig-api-upload/x"}
            if "/9990001?" in url:
                return {"status_code": "ERROR"}
            raise AssertionError(f"unexpected call: {url}")

        with (
            patch.object(instagram, "json_request", fake_execute),
            patch.object(instagram, "stream_request_bytes", return_value=b""),
            patch.object(instagram.time, "sleep"),
        ):
            result = InstagramTool().execute_approved(approved_record, api)
        assert isinstance(result, ActionFailed)
        self.assertIn("could not process the video", result.error)
        self.assertIn(asset_id, api.assets.records)

    def test_execute_approved_fails_when_account_changed(self) -> None:
        api = connected_api()
        asset_id = api.assets.add(filename="reel.mp4", media_type="video/mp4", data=b"video")

        def fake_pending(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/me?" in url:
                return dict(ME_RESPONSE)
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(instagram, "json_request", fake_pending):
            pending = InstagramTool().execute("post_reel", {"video_asset_id": asset_id}, api)
        assert isinstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)

        def fake_other(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/me?" in url:
                return {"user_id": "999", "username": "other"}
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(instagram, "json_request", fake_other):
            result = InstagramTool().execute_approved(approved_record, api)
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)


class InstagramCredentialFlowTests(unittest.TestCase):
    def test_complete_connect_exchanges_short_then_long_lived_token(self) -> None:
        api = FakeHostAPI()
        api.config["INSTAGRAM_APP_ID"] = "ig-app"
        api.config["INSTAGRAM_APP_SECRET"] = "ig-secret"
        flow = InstagramTool().credentials
        start = flow.start_connect({"redirect_uri": "https://host.example/cb"}, api)
        self.assertTrue(start["authorization_url"].startswith("https://www.instagram.com/oauth/authorize?"))
        self.assertIn("instagram_business_content_publish", start["authorization_url"])

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if url == instagram.IG_TOKEN_URL:
                return {"data": [{"access_token": "short-token", "user_id": 178414, "permissions": "..."}]}
            if "/access_token?" in url:
                self.assertIn("grant_type=ig_exchange_token", url)
                self.assertIn("access_token=short-token", url)
                return {"access_token": "long-token", "token_type": "bearer", "expires_in": 5_184_000}
            if "/me?" in url:
                self.assertIn("access_token=long-token", url)
                return dict(ME_RESPONSE)
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(instagram, "json_request", fake_json_request):
            result = flow.complete_connect(
                {"code": "auth-code", "state": start["state"], "redirect_uri": "https://host.example/cb"}, api
            )
        self.assertEqual(result["account"]["id"], "17841400000000000")
        self.assertEqual(result["account"]["label"], "@clawcreates")
        stored = api.credentials.load()
        assert stored is not None
        self.assertEqual(stored["secret"]["access_token"], "long-token")


if __name__ == "__main__":
    unittest.main()
