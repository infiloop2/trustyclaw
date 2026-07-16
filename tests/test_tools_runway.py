"""Unit tests for the Runway tool package (all third-party calls mocked)."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from host.tools.json_types import JSONObject
from host.tools.results import ActionExecuted, ActionFailed
from host.tools import runway
from host.tools.runway import RunwayTool
from host.tools.shared.web import WebRequestError

from test_tools import FakeHostAPI


def api_with_key() -> FakeHostAPI:
    api = FakeHostAPI()
    api.config["RUNWAY_API_SECRET"] = "rw-key"
    return api


class RunwayToolTests(unittest.TestCase):
    def test_manifest_is_enable_only_with_five_actions(self) -> None:
        tool = RunwayTool()
        self.assertEqual(tool.manifest.connection, "enable_only")
        self.assertIsNone(tool.credentials)
        self.assertEqual(
            [spec.id for spec in tool.manifest.actions],
            ["generate_video", "edit_video", "generate_image", "generate_speech", "get_task"],
        )
        cards = tool.manifest.data_summary.cards
        self.assertEqual(len(cards), 4)
        self.assertEqual([card.title for card in cards][:2], ["What leaves this host", "Where it can go"])
        can_do = next(card for card in cards if card.title == "What Runway can do with it")
        self.assertIn("train and improve", can_do.description)
        self.assertIn("no self-service training opt-out", can_do.description)
        self.assertIn("third-party model providers do not train", can_do.description)
        self.assertTrue(any(link.url.endswith("/terms-of-use") for link in can_do.links))
        leaves = next(card for card in cards if card.title == "What leaves this host")
        self.assertEqual([point.label for point in leaves.points], ["Generation requests", "Workspace media"])
        destinations = next(card for card in cards if card.title == "Where it can go")
        destination_text = " ".join(point.text for point in destinations.points)
        self.assertIn("Gen-4.5, Gen-4 Turbo, and Aleph 2", destination_text)
        self.assertIn("Google Veo 3.1 or ByteDance Seedance 2", destination_text)
        self.assertIn("to OpenAI's GPT Image 2", destination_text)
        self.assertIn("to ElevenLabs Multilingual v2", destination_text)
        retention = next(card for card in cards if card.title == "How long Runway retains it")
        self.assertNotIn("local copy", retention.description)

    def test_generate_video_text_to_video_defaults_to_gen45(self) -> None:
        seen: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["method"] = method
            seen["url"] = url
            seen["body"] = kwargs["body"]
            seen["headers"] = kwargs["headers"]
            return {"id": "task-123"}

        with patch.object(runway, "json_request", fake_json_request):
            result = RunwayTool().execute(
                "generate_video",
                {"prompt": "a fox runs on a beach", "duration_seconds": "8", "ratio": "720:1280", "seed": "42"},
                api_with_key(),
            )
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["task_id"], "task-123")
        self.assertEqual(result.result["task_status"], "PENDING")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["url"], runway.TEXT_TO_VIDEO_ENDPOINT)
        self.assertEqual(seen["headers"]["authorization"], "Bearer rw-key")
        self.assertEqual(seen["headers"]["x-runway-version"], runway.RUNWAY_API_VERSION)
        body = seen["body"]
        self.assertEqual(body["model"], "gen4.5")
        self.assertEqual(body["promptText"], "a fox runs on a beach")
        self.assertEqual(body["ratio"], "720:1280")
        self.assertEqual(body["duration"], 8)
        self.assertEqual(body["seed"], 42)
        self.assertNotIn("promptImage", body)

    def test_generate_video_with_image_routes_to_image_endpoint_and_gen4_turbo(self) -> None:
        seen: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["url"] = url
            seen["body"] = kwargs["body"]
            return {"id": "task-456"}

        with patch.object(runway, "json_request", fake_json_request):
            result = RunwayTool().execute(
                "generate_video",
                {"prompt": "animate this", "image_url": "https://example.com/frame.jpg"},
                api_with_key(),
            )
        assert isinstance(result, ActionExecuted)
        self.assertEqual(seen["url"], runway.IMAGE_TO_VIDEO_ENDPOINT)
        body = seen["body"]
        self.assertEqual(body["model"], "gen4_turbo")
        self.assertEqual(body["promptImage"], "https://example.com/frame.jpg")

    def test_generate_video_streams_a_staged_image_to_runway(self) -> None:
        api = api_with_key()
        asset_id = api.assets.add(
            filename="frame.png", media_type="image/png", data=b"local image bytes"
        )
        requests: list[tuple[str, str, JSONObject]] = []

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            requests.append((method, url, kwargs.get("body") or {}))
            if url == runway.UPLOADS_ENDPOINT:
                return {
                    "uploadUrl": "https://uploads.runway.example/image",
                    "fields": {"key": "ephemeral/image", "policy": "signed"},
                    "runwayUri": "runway://ephemeral/image",
                }
            if url == runway.IMAGE_TO_VIDEO_ENDPOINT:
                return {"id": "task-local-image"}
            raise AssertionError(url)

        streamed: dict[str, Any] = {}

        def fake_stream(method: str, url: str, **kwargs: Any) -> bytes:
            streamed["url"] = url
            streamed["body"] = b"".join(kwargs["body"])
            streamed["content_length"] = kwargs["content_length"]
            return b""

        with (
            patch.object(runway, "json_request", fake_json_request),
            patch.object(runway, "stream_request_bytes", fake_stream),
        ):
            result = RunwayTool().execute(
                "generate_video",
                {"prompt": "animate this", "image_asset_id": asset_id},
                api,
            )
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["task_id"], "task-local-image")
        self.assertEqual(requests[0][2], {"filename": "frame.png", "type": "ephemeral"})
        self.assertEqual(requests[1][2]["promptImage"], "runway://ephemeral/image")
        self.assertEqual(requests[1][2]["model"], "gen4_turbo")
        self.assertEqual(streamed["url"], "https://uploads.runway.example/image")
        self.assertIn(b"local image bytes", streamed["body"])
        self.assertEqual(streamed["content_length"], len(streamed["body"]))
        self.assertNotIn(asset_id, api.assets.records)

    def test_generate_video_rejects_conflicting_or_cross_type_image_input(self) -> None:
        api = api_with_key()
        video_asset_id = api.assets.add()
        both = RunwayTool().execute(
            "generate_video",
            {
                "prompt": "animate",
                "image_url": "https://example.com/frame.jpg",
                "image_asset_id": video_asset_id,
            },
            api,
        )
        wrong_type = RunwayTool().execute(
            "generate_video",
            {"prompt": "animate", "image_asset_id": video_asset_id},
            api,
        )
        assert isinstance(both, ActionFailed)
        assert isinstance(wrong_type, ActionFailed)
        self.assertIn("at most one", both.error)
        self.assertIn("does not refer to a staged image", wrong_type.error)

    def test_generate_video_rejects_image_only_model_without_image(self) -> None:
        result = RunwayTool().execute(
            "generate_video",
            {"prompt": "x", "model": "gen4_turbo"},
            api_with_key(),
        )
        assert isinstance(result, ActionFailed)
        self.assertIn("image-to-video only", result.error)

    def test_generate_video_validates_model_specific_duration_and_common_ratios(self) -> None:
        seen_bodies: list[JSONObject] = []

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen_bodies.append(kwargs["body"])
            return {"id": "task-model-options"}

        with patch.object(runway, "json_request", fake_json_request):
            veo = RunwayTool().execute("generate_video", {"prompt": "x", "model": "veo3.1"}, api_with_key())
            seedance = RunwayTool().execute(
                "generate_video", {"prompt": "x", "model": "seedance2", "duration_seconds": "15"}, api_with_key()
            )
        self.assertIsInstance(veo, ActionExecuted)
        self.assertIsInstance(seedance, ActionExecuted)
        self.assertEqual(seen_bodies[0]["duration"], 4)
        self.assertEqual(seen_bodies[1]["duration"], 15)

        bad_inputs = (
            {"prompt": "x", "model": "veo3.1", "duration_seconds": "5"},
            {"prompt": "x", "model": "seedance2", "duration_seconds": "3"},
            {"prompt": "x", "model": "gen4.5", "duration_seconds": "11"},
            {"prompt": "x", "model": "gen4.5", "ratio": "960:960"},
        )
        for tool_input in bad_inputs:
            self.assertIsInstance(RunwayTool().execute("generate_video", tool_input, api_with_key()), ActionFailed)

    def test_generate_video_validates_input(self) -> None:
        tool = RunwayTool()
        bad_inputs = [
            {},
            {"prompt": " "},
            {"prompt": "x", "duration_seconds": "20"},
            {"prompt": "x", "duration_seconds": "1"},
            {"prompt": "x", "image_url": "http://insecure.example.com/a.jpg"},
            {"prompt": "x", "model": "sora-2"},
            {"prompt": "x", "ratio": "42:42"},
            {"prompt": "x", "seed": "not-a-number"},
            {"prompt": "x", "seed": "-1"},
            {"prompt": "x", "seed": "4294967296"},
            {"prompt": "x", "duration_seconds": "²"},
            {"prompt": "x", "seed": "²"},
            {"prompt": "x", "seed": "--5"},
            {"prompt": "x", "image_url": "https://user@example.com/a.jpg"},
            {"prompt": "x", "image_url": "https://example.com:444/a.jpg"},
            {"prompt": "x", "image_url": "https://example.com/" + "a" * 4096},
            {"prompt": "x", "unknown": True},
        ]
        for bad_input in bad_inputs:
            result = tool.execute("generate_video", bad_input, api_with_key())
            self.assertIsInstance(result, ActionFailed, bad_input)

    def test_generation_rejects_malformed_provider_task_id(self) -> None:
        with patch.object(runway, "json_request", return_value={"id": "../../other-path"}):
            result = RunwayTool().execute("generate_video", {"prompt": "x"}, api_with_key())
        assert isinstance(result, ActionFailed)
        self.assertIn("no task id", result.error)

    def test_edit_video_builds_video_to_video_body(self) -> None:
        seen: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["url"] = url
            seen["body"] = kwargs["body"]
            return {"id": "task-789"}

        with patch.object(runway, "json_request", fake_json_request):
            result = RunwayTool().execute(
                "edit_video",
                {"video_url": "https://example.com/clip.mp4", "prompt": "make it night time"},
                api_with_key(),
            )
        assert isinstance(result, ActionExecuted)
        self.assertEqual(seen["url"], runway.VIDEO_TO_VIDEO_ENDPOINT)
        body = seen["body"]
        self.assertEqual(body["model"], "aleph2")
        self.assertEqual(body["videoUri"], "https://example.com/clip.mp4")
        self.assertEqual(body["promptText"], "make it night time")

    def test_edit_video_streams_staged_asset_to_runway_ephemeral_upload(self) -> None:
        api = api_with_key()
        asset_id = api.assets.add(data=b"local video bytes")
        requests: list[tuple[str, str, JSONObject]] = []

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            requests.append((method, url, kwargs.get("body") or {}))
            if url == runway.UPLOADS_ENDPOINT:
                return {
                    "uploadUrl": "https://uploads.runway.example/object",
                    "fields": {"key": "ephemeral/object", "policy": "signed"},
                    "runwayUri": "runway://ephemeral/object",
                }
            if url == runway.VIDEO_TO_VIDEO_ENDPOINT:
                return {"id": "task-local"}
            raise AssertionError(url)

        streamed: dict[str, Any] = {}

        def fake_stream(method: str, url: str, **kwargs: Any) -> bytes:
            streamed["method"] = method
            streamed["url"] = url
            streamed["headers"] = kwargs["headers"]
            streamed["body"] = b"".join(kwargs["body"])
            streamed["content_length"] = kwargs["content_length"]
            return b""

        with (
            patch.object(runway, "json_request", fake_json_request),
            patch.object(runway, "stream_request_bytes", fake_stream),
        ):
            result = RunwayTool().execute(
                "edit_video",
                {"video_asset_id": asset_id, "prompt": "make it cinematic"},
                api,
            )
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["task_id"], "task-local")
        self.assertEqual(requests[0][2], {"filename": "video.mp4", "type": "ephemeral"})
        self.assertEqual(requests[1][2]["videoUri"], "runway://ephemeral/object")
        self.assertEqual(streamed["url"], "https://uploads.runway.example/object")
        self.assertIn(b"local video bytes", streamed["body"])
        self.assertEqual(streamed["content_length"], len(streamed["body"]))
        self.assertNotIn(asset_id, api.assets.records)

    def test_malformed_task_response_preserves_uploaded_asset_for_retry(self) -> None:
        api = api_with_key()
        asset_id = api.assets.add(data=b"local video bytes")

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if url == runway.UPLOADS_ENDPOINT:
                return {
                    "uploadUrl": "https://uploads.runway.example/object",
                    "fields": {"key": "ephemeral/object", "policy": "signed"},
                    "runwayUri": "runway://ephemeral/object",
                }
            if url == runway.VIDEO_TO_VIDEO_ENDPOINT:
                return {"status": "accepted"}
            raise AssertionError(url)

        with (
            patch.object(runway, "json_request", fake_json_request),
            patch.object(runway, "stream_request_bytes", return_value=b""),
        ):
            result = RunwayTool().execute(
                "edit_video",
                {"video_asset_id": asset_id, "prompt": "make it cinematic"},
                api,
            )
        assert isinstance(result, ActionFailed)
        self.assertIn("no task id", result.error)
        self.assertIn(asset_id, api.assets.records)

    def test_multipart_rejects_control_characters_in_the_staged_filename(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid control characters"):
            runway._multipart_parts(
                {},
                filename="frame.png\r\nX-Injected: true",
                media_type="image/png",
                boundary="test-boundary",
            )

    def test_multipart_rejects_control_characters_in_provider_fields(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid form fields"):
            runway._multipart_parts(
                {"key\r\nX-Injected": "value"},
                filename="frame.png",
                media_type="image/png",
                boundary="test-boundary",
            )

    def test_runway_urls_reject_ips_credentials_ports_and_oversize(self) -> None:
        self.assertTrue(runway._is_https_runway_url("https://uploads.runway.example/object"))
        for value in (
            "https://127.0.0.1/object",
            "https://user@example.com/object",
            "https://uploads.runway.example:444/object",
            "https://localhost/object",
            "https://example.com/" + "x" * 2_100,
        ):
            with self.subTest(value=value[:100]):
                self.assertFalse(runway._is_https_runway_url(value))

    def test_edit_video_validates_input(self) -> None:
        tool = RunwayTool()
        bad_inputs = [
            {"prompt": "edit"},
            {"video_url": "http://insecure.example.com/c.mp4", "prompt": "edit"},
            {"video_url": "https://example.com/c.mp4"},
            {"video_url": "https://example.com/c.mp4", "video_asset_id": "asset", "prompt": "edit"},
            {"video_url": "https://example.com/c.mp4", "prompt": "edit", "unknown": 1},
        ]
        for bad_input in bad_inputs:
            result = tool.execute("edit_video", bad_input, api_with_key())
            self.assertIsInstance(result, ActionFailed, bad_input)

        api = api_with_key()
        image_asset_id = api.assets.add(filename="frame.png", media_type="image/png")
        wrong_type = tool.execute(
            "edit_video", {"video_asset_id": image_asset_id, "prompt": "edit"}, api
        )
        assert isinstance(wrong_type, ActionFailed)
        self.assertIn("does not refer to a staged video", wrong_type.error)

    def test_generate_image_uses_gpt_image_2_through_runway(self) -> None:
        seen: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["url"] = url
            seen["body"] = kwargs["body"]
            return {"id": "image-task"}

        with patch.object(runway, "json_request", fake_json_request):
            result = RunwayTool().execute(
                "generate_image",
                {"prompt": "a red fox", "ratio": "1280:1920", "quality": "medium"},
                api_with_key(),
            )
        assert isinstance(result, ActionExecuted)
        self.assertEqual(seen["url"], runway.TEXT_TO_IMAGE_ENDPOINT)
        self.assertEqual(
            seen["body"],
            {
                "model": "gpt_image_2",
                "promptText": "a red fox",
                "ratio": "1280:1920",
                "quality": "medium",
                "outputCount": 1,
            },
        )
        self.assertEqual(result.result["output_kind"], "image")

    def test_generate_speech_uses_elevenlabs_through_runway(self) -> None:
        seen: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["url"] = url
            seen["body"] = kwargs["body"]
            return {"id": "audio-task"}

        with patch.object(runway, "json_request", fake_json_request):
            result = RunwayTool().execute(
                "generate_speech", {"text": "Hello world", "voice": "Serene"}, api_with_key()
            )
        assert isinstance(result, ActionExecuted)
        self.assertEqual(seen["url"], runway.TEXT_TO_SPEECH_ENDPOINT)
        self.assertEqual(
            seen["body"],
            {
                "model": "eleven_multilingual_v2",
                "promptText": "Hello world",
                "voice": {"type": "runway-preset", "presetId": "Serene"},
            },
        )
        self.assertEqual(result.result["output_kind"], "audio")

    def test_image_and_speech_validate_input(self) -> None:
        tool = RunwayTool()
        bad_calls = [
            ("generate_image", {}),
            ("generate_image", {"prompt": "x", "ratio": "1024:1024"}),
            ("generate_image", {"prompt": "x", "quality": "ultra"}),
            ("generate_speech", {}),
            ("generate_speech", {"text": "x", "voice": "unknown"}),
            ("generate_speech", {"text": "x", "extra": True}),
        ]
        for action, tool_input in bad_calls:
            with self.subTest(action=action, tool_input=tool_input):
                self.assertIsInstance(tool.execute(action, tool_input, api_with_key()), ActionFailed)

    def test_get_task_maps_terminal_states(self) -> None:
        cases = [
            ({"id": "task-1", "status": "RUNNING", "progress": 0.5}, "RUNNING", None),
            ({"id": "task-1", "status": "THROTTLED"}, "THROTTLED", None),
            (
                {"id": "task-1", "status": "SUCCEEDED", "output": ["https://cdn.example/v.mp4"]},
                "SUCCEEDED",
                "https://cdn.example/v.mp4",
            ),
            ({"id": "task-1", "status": "FAILED", "failureCode": "SAFETY.INPUT"}, "FAILED", None),
            ({"id": "task-1", "status": "CANCELLED"}, "CANCELLED", None),
        ]
        for response, expected_status, expected_url in cases:
            def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
                self.assertTrue(url.endswith("/tasks/task-1"))
                return dict(response)

            with patch.object(runway, "json_request", fake_json_request):
                result = RunwayTool().execute("get_task", {"task_id": "task-1"}, api_with_key())
            assert isinstance(result, ActionExecuted)
            self.assertEqual(result.result["task_status"], expected_status)
            if expected_url:
                self.assertEqual(result.result["video_url"], expected_url)
            else:
                self.assertNotIn("video_url", result.result)

    def test_get_task_success_without_output_url_is_not_a_url(self) -> None:
        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            return {"id": "task-2", "status": "SUCCEEDED", "output": []}

        with patch.object(runway, "json_request", fake_json_request):
            result = RunwayTool().execute("get_task", {"task_id": "task-2"}, api_with_key())
        assert isinstance(result, ActionExecuted)
        self.assertNotIn("video_url", result.result)
        self.assertIn("no output URL", result.result["message"])

    def test_get_task_drops_non_https_or_credentialed_output_url(self) -> None:
        for output_url in (
            "http://cdn.example/v.mp4",
            "https://user@cdn.example/v.mp4",
            "https://cdn.example:444/v.mp4",
        ):
            with self.subTest(output_url=output_url), patch.object(
                runway,
                "json_request",
                return_value={"id": "task-2", "status": "SUCCEEDED", "output": [output_url]},
            ):
                result = RunwayTool().execute("get_task", {"task_id": "task-2"}, api_with_key())
            assert isinstance(result, ActionExecuted)
            self.assertNotIn("video_url", result.result)

    def test_get_task_names_non_video_output(self) -> None:
        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            return {"id": "task-image", "status": "SUCCEEDED", "output": ["https://cdn.example/i.webp"]}

        with patch.object(runway, "json_request", fake_json_request):
            result = RunwayTool().execute(
                "get_task", {"task_id": "task-image", "output_kind": "image"}, api_with_key()
            )
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["image_url"], "https://cdn.example/i.webp")
        self.assertNotIn("video_url", result.result)

    def test_get_task_validates_task_id(self) -> None:
        result = RunwayTool().execute("get_task", {"task_id": "bad id/../x"}, api_with_key())
        self.assertIsInstance(result, ActionFailed)

    def test_missing_key_and_provider_failures(self) -> None:
        tool = RunwayTool()
        result = tool.execute("generate_video", {"prompt": "x"}, FakeHostAPI())
        assert isinstance(result, ActionFailed)
        self.assertIn("RUNWAY_API_SECRET is not set", result.error)

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            raise WebRequestError("failed", status=429)

        with patch.object(runway, "json_request", fake_json_request):
            result = tool.execute("generate_video", {"prompt": "x"}, api_with_key())
        assert isinstance(result, ActionFailed)
        self.assertEqual(result.error, "Runway rate limit or daily generation quota was reached.")

        def fake_400(method: str, url: str, **kwargs: Any) -> JSONObject:
            raise WebRequestError("failed", status=400)

        with patch.object(runway, "json_request", fake_400):
            result = tool.execute("generate_video", {"prompt": "x"}, api_with_key())
        assert isinstance(result, ActionFailed)
        self.assertIn("compatible", result.error)

    def test_unsupported_action_and_no_approvals(self) -> None:
        tool = RunwayTool()
        self.assertIsInstance(tool.execute("delete_task", {}, api_with_key()), ActionFailed)
        self.assertIsInstance(tool.execute_approved("approval-1", api_with_key()), ActionFailed)


if __name__ == "__main__":
    unittest.main()
