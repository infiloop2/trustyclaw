"""Unit tests for bounded, tool-scoped video staging."""

from __future__ import annotations

import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from host.runtime.tools.assets import ASSET_TTL_SECONDS, AssetError, ToolAssetStore
from host.runtime.agent_shim import mcp_shim as tools_mcp_shim
from host.tools.host_api import AssetMetadata


class ToolAssetStoreTests(unittest.TestCase):
    def test_clean_start_discards_prior_process_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "assets"
            root.mkdir()
            (root / "stale-asset").write_bytes(b"stale")

            ToolAssetStore(root, clean_start=True)

            self.assertEqual(list(root.iterdir()), [])

    def test_clean_start_discards_prior_files_and_does_not_follow_root_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            target = parent / "target"
            target.mkdir()
            sentinel = target / "keep"
            sentinel.write_bytes(b"keep")
            root = parent / "assets"
            root.symlink_to(target, target_is_directory=True)

            ToolAssetStore(root, clean_start=True)

            self.assertTrue(root.is_dir())
            self.assertFalse(root.is_symlink())
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_stages_bytes_and_scopes_opaque_id_to_one_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ToolAssetStore(Path(directory) / "assets")
            metadata = store.stage(
                kind="video",
                tool_id="runway",
                filename="clip.mp4",
                media_type="video/mp4",
                size_bytes=512,
                source=io.BytesIO(b"x" * 512),
            )
            self.assertNotIn("clip", metadata.asset_id)
            with store.open("runway", metadata.asset_id) as source:
                self.assertEqual(source.read(), b"x" * 512)
            with self.assertRaisesRegex(AssetError, "invalid or expired"):
                store.describe("instagram", metadata.asset_id)

    def test_rejects_mismatched_type_and_short_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ToolAssetStore(Path(directory) / "assets")
            with self.assertRaisesRegex(AssetError, "does not match"):
                store.stage(
                    kind="video",
                    tool_id="runway",
                    filename="clip.mov",
                    media_type="video/mp4",
                    size_bytes=512,
                    source=io.BytesIO(b"x" * 512),
                )
            with self.assertRaisesRegex(AssetError, "ended before"):
                store.stage(
                    kind="video",
                    tool_id="runway",
                    filename="clip.mp4",
                    media_type="video/mp4",
                    size_bytes=512,
                    source=io.BytesIO(b"short"),
                )
            # A failed stream leaves no reservation or file behind.
            self.assertEqual(store._records, {})

    def test_in_flight_upload_is_not_readable_and_reserves_quota(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ToolAssetStore(Path(directory) / "assets")
            entered = threading.Event()
            release = threading.Event()
            outcome: dict[str, object] = {}
            asset_id = "a" * 43

            class BlockingSource(io.BytesIO):
                def read(self, size: int = -1) -> bytes:
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("test did not release upload")
                    return super().read(size)

            def stage() -> None:
                try:
                    outcome["metadata"] = store.stage(
                        kind="video",
                        tool_id="runway",
                        filename="a.mp4",
                        media_type="video/mp4",
                        size_bytes=512,
                        source=BlockingSource(b"x" * 512),
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    outcome["error"] = exc

            with (
                patch("host.runtime.tools.assets.secrets.token_urlsafe", return_value=asset_id),
                patch("host.runtime.tools.assets.MAX_STAGED_ASSETS", 1),
            ):
                worker = threading.Thread(target=stage)
                worker.start()
                self.assertTrue(entered.wait(2))
                try:
                    with self.assertRaisesRegex(AssetError, "invalid or expired"):
                        store.describe("runway", asset_id)
                    with self.assertRaisesRegex(AssetError, "Too many assets"):
                        store.stage(
                            kind="video",
                            tool_id="runway",
                            filename="b.mp4",
                            media_type="video/mp4",
                            size_bytes=512,
                            source=io.BytesIO(b"y" * 512),
                        )
                finally:
                    release.set()
                    worker.join(3)

            self.assertFalse(worker.is_alive())
            self.assertNotIn("error", outcome)
            metadata = outcome["metadata"]
            self.assertIsInstance(metadata, AssetMetadata)
            self.assertEqual(metadata.asset_id, asset_id)
            self.assertTrue(metadata.sha256)
            with store.open("runway", asset_id) as source:
                self.assertEqual(source.read(), b"x" * 512)

    def test_open_file_descriptor_survives_concurrent_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "assets"
            store = ToolAssetStore(root)
            metadata = store.stage(
                kind="video",
                tool_id="runway",
                filename="clip.mp4",
                media_type="video/mp4",
                size_bytes=512,
                source=io.BytesIO(b"x" * 512),
            )

            with store.open("runway", metadata.asset_id) as source:
                store.delete("runway", metadata.asset_id)
                self.assertFalse((root / metadata.asset_id).exists())
                self.assertEqual(source.read(), b"x" * 512)

            with self.assertRaisesRegex(AssetError, "invalid or expired"):
                store.describe("runway", metadata.asset_id)

    def test_expiry_cleanup_cancels_an_in_flight_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "assets"
            store = ToolAssetStore(root)
            entered = threading.Event()
            release = threading.Event()
            outcome: dict[str, object] = {}
            asset_id = "b" * 43

            class BlockingSource(io.BytesIO):
                def read(self, size: int = -1) -> bytes:
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("test did not release upload")
                    return super().read(size)

            def stage() -> None:
                try:
                    store.stage(
                        kind="video",
                        tool_id="instagram",
                        filename="clip.mp4",
                        media_type="video/mp4",
                        size_bytes=512,
                        source=BlockingSource(b"x" * 512),
                    )
                except Exception as exc:
                    outcome["error"] = exc

            with (
                patch("host.runtime.tools.assets.secrets.token_urlsafe", return_value=asset_id),
                patch("host.runtime.tools.assets.time.time", return_value=1_000),
            ):
                worker = threading.Thread(target=stage)
                worker.start()
                self.assertTrue(entered.wait(2))
                try:
                    store.cleanup_expired(1_000 + ASSET_TTL_SECONDS)
                finally:
                    release.set()
                    worker.join(3)

            self.assertFalse(worker.is_alive())
            self.assertIsInstance(outcome.get("error"), AssetError)
            self.assertIn("cancelled", str(outcome["error"]))
            self.assertEqual(list(root.iterdir()), [])

    def test_stages_supported_images_and_rejects_cross_type_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ToolAssetStore(Path(directory) / "assets")
            metadata = store.stage(
                kind="image",
                tool_id="runway",
                filename="frame.jpeg",
                media_type="image/jpeg",
                size_bytes=512,
                source=io.BytesIO(b"x" * 512),
            )
            self.assertEqual(metadata.media_type, "image/jpeg")
            with self.assertRaisesRegex(AssetError, "Image must be"):
                store.stage(
                    kind="image",
                    tool_id="runway",
                    filename="clip.mp4",
                    media_type="video/mp4",
                    size_bytes=512,
                    source=io.BytesIO(b"x" * 512),
                )

    def test_missing_image_uses_generic_upload_again_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ToolAssetStore(Path(directory) / "assets")
            with self.assertRaisesRegex(AssetError, "Asset id is invalid or expired. Upload the file again") as ctx:
                store.describe("runway", "asset_abcdefghijklmnopqrstuvwxyz123456")
        self.assertNotIn("video", str(ctx.exception).lower())

    def test_recurring_cleanup_removes_expired_unused_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "assets"
            with patch("host.runtime.tools.assets.time.time", return_value=1_000):
                store = ToolAssetStore(root)
                metadata = store.stage(
                    kind="video",
                    tool_id="instagram",
                    filename="clip.mp4",
                    media_type="video/mp4",
                    size_bytes=512,
                    source=io.BytesIO(b"x" * 512),
                )
            self.assertTrue((root / metadata.asset_id).is_file())

            store.cleanup_expired(metadata.expires_at - 1)
            self.assertTrue((root / metadata.asset_id).is_file())
            store.cleanup_expired(metadata.expires_at)

            self.assertFalse((root / metadata.asset_id).exists())
            with self.assertRaisesRegex(AssetError, "invalid or expired"):
                store.describe("instagram", metadata.asset_id)


class ShimVideoStageTests(unittest.TestCase):
    def test_shim_maps_files_root_path_and_stages_regular_video(self) -> None:
        class Response:
            status = 200

            def read(self) -> bytes:
                return b'{"video_asset_id":"opaque-id"}'

        class Connection:
            def __init__(self, socket_path: str) -> None:
                self.socket_path = socket_path
                self.body = b""
                self.headers: dict[str, str] = {}

            def request(self, method: str, path: str, *, body, headers) -> None:
                self.body = body.read()
                self.headers = headers
                self.method = method
                self.path = path

            def getresponse(self) -> Response:
                return Response()

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "workspace" / "videos" / "clip.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"x" * 512)
            connection = Connection("unused")
            with (
                patch.dict("os.environ", {"HOME": directory}),
                patch.object(tools_mcp_shim, "UnixHTTPConnection", return_value=connection),
            ):
                result = tools_mcp_shim._stage_video(
                    {"path": "/workspace/videos/clip.mp4", "for_tool": "runway"}
                )
        self.assertEqual(result, {"video_asset_id": "opaque-id"})
        self.assertEqual(connection.body, b"x" * 512)
        self.assertEqual(connection.path, "/assets/video")
        self.assertEqual(connection.headers["X-TrustyClaw-Filename"], "clip.mp4")
        self.assertNotIn(str(video), connection.headers.values())

    def test_shim_rejects_symlink_without_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "clip.mp4"
            video.write_bytes(b"x" * 512)
            symlink = Path(directory) / "linked.mp4"
            symlink.symlink_to(video)
            with (
                patch.dict("os.environ", {"HOME": directory}),
                patch.object(tools_mcp_shim, "UnixHTTPConnection") as connection,
                self.assertRaisesRegex(RuntimeError, "regular, non-symlink"),
            ):
                tools_mcp_shim._stage_video(
                    {"path": "/linked.mp4", "for_tool": "runway"}
                )
            connection.assert_not_called()

    def test_shim_stages_a_local_image_for_runway(self) -> None:
        class Response:
            status = 200

            def read(self) -> bytes:
                return b'{"image_asset_id":"opaque-image-id"}'

        class Connection:
            def request(self, method: str, path: str, *, body, headers) -> None:
                self.body = body.read()
                self.headers = headers
                self.method = method
                self.path = path

            def getresponse(self) -> Response:
                return Response()

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "frame.webp"
            image.write_bytes(b"x" * 512)
            connection = Connection()
            with (
                patch.dict("os.environ", {"HOME": directory}),
                patch.object(tools_mcp_shim, "UnixHTTPConnection", return_value=connection),
            ):
                result = tools_mcp_shim._stage_image(
                    {"path": "/frame.webp", "for_tool": "runway"}
                )
        self.assertEqual(result, {"image_asset_id": "opaque-image-id"})
        self.assertEqual(connection.body, b"x" * 512)
        self.assertEqual(connection.path, "/assets/image")
        self.assertEqual(connection.headers["Content-Type"], "image/webp")
        self.assertEqual(connection.headers["X-TrustyClaw-Filename"], "frame.webp")
        self.assertNotIn(str(image), connection.headers.values())


if __name__ == "__main__":
    unittest.main()
