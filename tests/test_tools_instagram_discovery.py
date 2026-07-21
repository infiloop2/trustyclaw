"""Unit tests for public Instagram discovery (all provider calls mocked)."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from host.tools import instagram_discovery
from host.tools.instagram_discovery import InstagramDiscoveryTool
from host.tools.results import ActionExecuted, ActionFailed
from host.tools.shared.web import WebRequestError

from test_tools import FakeHostAPI


def configured_api() -> FakeHostAPI:
    api = FakeHostAPI()
    api.config["SCRAPECREATORS_API_KEY"] = "scrape-key"
    return api


def reel(shortcode: str = "ABC123") -> dict[str, object]:
    return {
        "id": "42",
        "shortcode": shortcode,
        "url": f"https://www.instagram.com/reel/{shortcode}/",
        "caption": "Public caption",
        "user": {"username": "creator"},
        "taken_at": 1_700_000_000,
        "like_count": 12,
        "comment_count": 3,
        "play_count": 400,
        "video_duration": 8.5,
        "video_url": "https://cdn.example/video.mp4",
        "image_url": "https://cdn.example/image.jpg",
        "clips_music_attribution_info": {"audio_id": "999", "song_name": "Track"},
    }


class InstagramDiscoveryToolTests(unittest.TestCase):
    def test_manifest_exposes_selected_provider_neutral_actions(self) -> None:
        manifest = InstagramDiscoveryTool().manifest
        self.assertEqual(manifest.tool_id, "instagram_discovery")
        self.assertEqual(manifest.connection, "enable_only")
        self.assertEqual(
            [action.id for action in manifest.actions],
            ["search_reels", "get_trending_reels", "search_hashtag", "get_reels_by_audio", "get_reel_details"],
        )
        self.assertTrue(all(action.approval == "direct" for action in manifest.actions))
        self.assertIn("not an objective global ranking", manifest.actions[1].description)
        self.assertEqual(len(manifest.data_summary.cards), 4)
        self.assertEqual(manifest.data_summary.cards[0].title, "What leaves this host")
        self.assertIn("All data in a discovery request", manifest.data_summary.cards[0].description)
        self.assertIn("retained request metadata and error logs", manifest.data_summary.cards[0].description)
        self.assertNotIn("Never include passwords", manifest.data_summary.cards[0].description)
        self.assertNotIn("logged like any other request", manifest.data_summary.cards[0].description)
        self.assertEqual(manifest.data_summary.cards[1].title, "Where it can go")

    def test_keyword_search_sends_documented_params_and_normalizes(self) -> None:
        seen: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any):
            seen["url"] = url
            seen["headers"] = kwargs["headers"]
            return {"success": True, "reels": [reel(), reel()]}

        with patch.object(instagram_discovery, "json_request", fake_json_request):
            result = InstagramDiscoveryTool().execute(
                "search_reels",
                {"query": "dogs", "date_posted": "last-week", "page": "2", "limit": "5"},
                configured_api(),
            )
        assert isinstance(result, ActionExecuted)
        self.assertIn("/v2/instagram/reels/search?", seen["url"])
        self.assertIn("query=dogs", seen["url"])
        self.assertIn("date_posted=last-week", seen["url"])
        self.assertIn("page=2", seen["url"])
        self.assertEqual(seen["headers"], {"x-api-key": "scrape-key"})
        self.assertEqual(len(result.result["reels"]), 1)
        self.assertEqual(result.result["reels"][0]["username"], "creator")
        self.assertEqual(result.result["reels"][0]["audio_id"], "999")

    def test_trending_reads_nested_shape_and_deduplicates(self) -> None:
        with patch.object(instagram_discovery, "json_request", return_value={"success": True, "data": {"reels": [reel(), reel("XYZ")]}}):
            result = InstagramDiscoveryTool().execute("get_trending_reels", {"limit": "1"}, configured_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(len(result.result["reels"]), 1)
        self.assertIn("1 unique", result.result["message"])

    def test_hashtag_defaults_to_reels_and_returns_cursor(self) -> None:
        seen = {}

        def fake_json_request(method: str, url: str, **kwargs: Any):
            seen["url"] = url
            return {"success": True, "posts": [reel()], "cursor": "2"}

        with patch.object(instagram_discovery, "json_request", fake_json_request):
            result = InstagramDiscoveryTool().execute("search_hashtag", {"hashtag": "#makeup"}, configured_api())
        assert isinstance(result, ActionExecuted)
        self.assertIn("hashtag=makeup", seen["url"])
        self.assertIn("media_type=reels", seen["url"])
        self.assertEqual(result.result["next_cursor"], "2")
        self.assertTrue(result.result["has_more"])

    def test_audio_lookup_validates_id_and_chains_cursor(self) -> None:
        seen = {}

        def fake_json_request(method: str, url: str, **kwargs: Any):
            seen["url"] = url
            return {"success": True, "reels": [reel()]}

        with patch.object(instagram_discovery, "json_request", fake_json_request):
            result = InstagramDiscoveryTool().execute("get_reels_by_audio", {"audio_id": "1392969992841787", "cursor": "next"}, configured_api())
        self.assertIsInstance(result, ActionExecuted)
        self.assertIn("audio_id=1392969992841787", seen["url"])
        self.assertIn("cursor=next", seen["url"])
        self.assertIsInstance(InstagramDiscoveryTool().execute("get_reels_by_audio", {"audio_id": "bad"}, configured_api()), ActionFailed)

    def test_details_requests_trim_without_permanent_media(self) -> None:
        seen = {}

        def fake_json_request(method: str, url: str, **kwargs: Any):
            seen["url"] = url
            return {"data": {"xdt_shortcode_media": reel()}}

        with patch.object(instagram_discovery, "json_request", fake_json_request):
            result = InstagramDiscoveryTool().execute(
                "get_reel_details", {"url": "https://instagram.com/reel/ABC123/?utm_source=x#frag"}, configured_api()
            )
        assert isinstance(result, ActionExecuted)
        self.assertIn("trim=true", seen["url"])
        self.assertIn("download_media=false", seen["url"])
        self.assertNotIn("utm_source", seen["url"])
        self.assertEqual(result.result["reel"]["shortcode"], "ABC123")

    def test_details_reads_trimmed_top_level_media(self) -> None:
        with patch.object(
            instagram_discovery,
            "json_request",
            return_value={
                "success": True,
                "credits_used": 1,
                "xdt_shortcode_media": reel("Trimmed123"),
            },
        ):
            result = InstagramDiscoveryTool().execute(
                "get_reel_details",
                {"url": "https://instagram.com/reel/Trimmed123/"},
                configured_api(),
            )

        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["reel"]["shortcode"], "Trimmed123")

    def test_provider_supplied_urls_on_foreign_hosts_are_dropped(self) -> None:
        # The scraper is untrusted; a planted attacker URL must not appear as a
        # Reel's url/video_url/image_url. The permalink is derived from the
        # validated shortcode instead.
        malicious = reel(shortcode="GoodCode1")
        malicious["url"] = "https://attacker.example/phish"
        malicious["video_url"] = "https://attacker.example/v.mp4"
        malicious["image_url"] = "https://attacker.example/i.jpg"

        def fake_json_request(method: str, url: str, **kwargs: Any):
            return {"data": {"xdt_shortcode_media": malicious}}

        with patch.object(instagram_discovery, "json_request", fake_json_request):
            result = InstagramDiscoveryTool().execute(
                "get_reel_details", {"url": "https://instagram.com/reel/GoodCode1/"}, configured_api()
            )
        assert isinstance(result, ActionExecuted)
        reel_out = result.result["reel"]
        self.assertEqual(reel_out["url"], "https://www.instagram.com/reel/GoodCode1/")
        self.assertNotIn("attacker.example", str(reel_out))
        self.assertEqual(reel_out["video_url"], "")
        self.assertEqual(reel_out["image_url"], "")

    def test_provider_urls_with_credentials_or_nonstandard_ports_are_dropped(self) -> None:
        for media_url in (
            "https://user@cdninstagram.com/v.mp4",
            "https://cdninstagram.com:444/v.mp4",
        ):
            response_reel = reel(shortcode="GoodCode1")
            response_reel["video_url"] = media_url
            with self.subTest(media_url=media_url), patch.object(
                instagram_discovery,
                "json_request",
                return_value={"data": {"xdt_shortcode_media": response_reel}},
            ):
                result = InstagramDiscoveryTool().execute(
                    "get_reel_details",
                    {"url": "https://instagram.com/reel/GoodCode1/"},
                    configured_api(),
                )
            assert isinstance(result, ActionExecuted)
            self.assertEqual(result.result["reel"]["video_url"], "")

    def test_provider_urls_with_embedded_controls_are_dropped(self) -> None:
        response_reel = reel(shortcode="GoodCode1")
        response_reel["video_url"] = "https://evil.example\t.cdninstagram.com/video.mp4"
        with patch.object(
            instagram_discovery,
            "json_request",
            return_value={"data": {"xdt_shortcode_media": response_reel}},
        ):
            result = InstagramDiscoveryTool().execute(
                "get_reel_details", {"url": "https://instagram.com/reel/GoodCode1/"}, configured_api()
            )
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["reel"]["video_url"], "")

    def test_absurd_provider_numbers_are_normalized(self) -> None:
        self.assertEqual(instagram_discovery._number(float("inf")), "0")

    def test_rejects_invalid_inputs_and_maps_provider_errors(self) -> None:
        tool = InstagramDiscoveryTool()
        for action, tool_input in (
            ("search_reels", {}),
            ("search_reels", {"query": "x", "page": "0"}),
            ("search_reels", {"query": "x" * 501}),
            ("search_reels", {"query": "x", "page": 1.5}),
            ("search_hashtag", {"hashtag": "two tags"}),
            ("search_hashtag", {"hashtag": "#" + "x" * 101}),
            ("search_hashtag", {"hashtag": "x", "reels_only": "yes"}),
            ("search_hashtag", {"hashtag": "x", "cursor": "x" * 1001}),
            ("get_reel_details", {"url": "https://instagram.com/reel/" + "x" * 2050}),
            ("get_reel_details", {"url": "https://instagram.com/reel/../accounts/login/"}),
            ("get_reel_details", {"url": "https://user@instagram.com/reel/ABC123/"}),
            ("get_reel_details", {"url": "https://instagram.com:444/reel/ABC123/"}),
            ("get_reel_details", {"url": "https://example.com/reel/1"}),
        ):
            with self.subTest(action=action, tool_input=tool_input):
                self.assertIsInstance(tool.execute(action, tool_input, configured_api()), ActionFailed)

        with patch.object(instagram_discovery, "json_request", side_effect=WebRequestError("raw", status=403, body=b"secret")):
            result = tool.execute("get_trending_reels", {}, configured_api())
        assert isinstance(result, ActionFailed)
        self.assertIn("API key", result.error)
        self.assertNotIn("secret", result.error)


if __name__ == "__main__":
    unittest.main()
