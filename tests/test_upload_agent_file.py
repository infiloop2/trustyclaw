from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from host.runtime.admin_api import service as admin_api
from host.runtime.root_helpers import upload_agent_file


class _BinaryStdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


class UploadAgentFileTests(unittest.TestCase):
    def test_upload_is_atomic_and_timestamp_prefixed(self) -> None:
        payload = b"image-bytes"
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            with (
                patch.object(upload_agent_file, "AGENT_HOME", home_path),
                patch.object(upload_agent_file.sys, "stdin", _BinaryStdin(payload)),
            ):
                result = upload_agent_file.upload("photo one.png", len(payload))

            stored_name = str(result["name"])
            self.assertRegex(stored_name, r"^\d{8}T\d{6}\.\d{6}Z_photo one\.png$")
            self.assertEqual(result["path"], f"user-files/{stored_name}")
            self.assertEqual(result["original_name"], "photo one.png")
            self.assertEqual(result["size_bytes"], len(payload))
            self.assertEqual((home_path / str(result["path"])).read_bytes(), payload)
            self.assertFalse(any(path.name.startswith(".uploading-") for path in (home_path / "user-files").iterdir()))

    def test_short_upload_is_never_published(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            with (
                patch.object(upload_agent_file, "AGENT_HOME", home_path),
                patch.object(upload_agent_file.sys, "stdin", _BinaryStdin(b"short")),
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                upload_agent_file.upload("photo.png", 20)

            self.assertEqual(list((home_path / "user-files").iterdir()), [])

    def test_upload_directory_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as outside:
            home_path = Path(home)
            (home_path / "user-files").symlink_to(outside, target_is_directory=True)
            with (
                patch.object(upload_agent_file, "AGENT_HOME", home_path),
                patch.object(upload_agent_file.sys, "stdin", _BinaryStdin(b"payload")),
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                upload_agent_file.upload("photo.png", 7)

            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_filename_and_size_bounds_match_the_admin_protocol(self) -> None:
        self.assertEqual(upload_agent_file.MAX_UPLOAD_BYTES, admin_api.AGENT_FILE_UPLOAD_MAX_BYTES)
        self.assertEqual(upload_agent_file.MAX_FILENAME_BYTES, admin_api.AGENT_FILE_UPLOAD_FILENAME_MAX_BYTES)
        for invalid in ("", ".", "..", "../photo.png", "dir\\photo.png", "bad\nname.png"):
            with self.subTest(filename=invalid), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                upload_agent_file.validate_filename(invalid)
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            upload_agent_file.validate_filename("é" * 101)
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            upload_agent_file.parse_size(str(upload_agent_file.MAX_UPLOAD_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
