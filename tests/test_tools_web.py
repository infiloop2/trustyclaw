"""Unit tests for the shared web helper's response-size cap (urllib mocked)."""

from __future__ import annotations

import io
from pathlib import Path
import unittest
from typing import Any
from unittest.mock import patch

from host.tools.shared import web
from host.tools.shared.web import (
    MAX_RESPONSE_BYTES,
    RESPONSE_TOO_LARGE_MESSAGE,
    WebRequestError,
    request_bytes,
    stream_request_bytes,
)


class _FakeResponse:
    """Stand-in for a urllib HTTP response that serves a fixed body."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.headers: dict[str, str] = {}

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body if size is None or size < 0 else self._body[:size]


def _serving(body: bytes) -> object:
    # Requests go through the module's hardened opener (redirects disabled), so
    # tests intercept its open() rather than urllib.request.urlopen.
    return patch.object(web._OPENER, "open", new=lambda *a, **k: _FakeResponse(body))


class WebResponseCapTests(unittest.TestCase):
    def test_tool_packages_centralize_urllib_requests(self) -> None:
        tools_root = Path(__file__).resolve().parents[1] / "host" / "tools"
        direct_users = []
        for path in tools_root.rglob("*.py"):
            if path == tools_root / "shared" / "web.py":
                continue
            source = path.read_text()
            if "urllib.request" in source or "urlopen(" in source:
                direct_users.append(str(path.relative_to(tools_root)))
        self.assertEqual(
            direct_users,
            [],
            "tool packages must use shared.web so redirects are refused and responses are bounded",
        )

    def test_body_at_cap_is_returned_whole(self) -> None:
        body = b"x" * 100
        with _serving(body):
            got = request_bytes("GET", "https://api.example.com/x", failure_message="failed", max_bytes=100)
        self.assertEqual(got, body)

    def test_oversized_body_fails_explicitly_instead_of_truncating(self) -> None:
        with _serving(b"x" * 101):
            with self.assertRaises(WebRequestError) as ctx:
                request_bytes("GET", "https://api.example.com/x", failure_message="failed", max_bytes=100)
        self.assertEqual(str(ctx.exception), RESPONSE_TOO_LARGE_MESSAGE)

    def test_json_request_threads_max_bytes(self) -> None:
        # A cap smaller than the padded body must fail before parsing, proving
        # the per-call override reaches the JSON path.
        with _serving(b'{"ok": true}' + b" " * 100):
            with self.assertRaises(WebRequestError):
                web.json_request(
                    "GET",
                    "https://api.example.com/x",
                    failure_message="failed",
                    invalid_response_message="invalid",
                    max_bytes=10,
                )

    def test_json_request_rejects_invalid_utf8(self) -> None:
        with _serving(b'{"message":"\xff"}'):
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                web.json_request(
                    "GET",
                    "https://api.example.com/x",
                    failure_message="failed",
                    invalid_response_message="invalid",
                )

    def test_json_request_rejects_nonstandard_numeric_constants(self) -> None:
        for constant in (b"NaN", b"Infinity", b"-Infinity", b"1e999"):
            with self.subTest(constant=constant), _serving(b'{"value":' + constant + b"}"):
                with self.assertRaisesRegex(RuntimeError, "invalid"):
                    web.json_request(
                        "GET",
                        "https://api.example.com/x",
                        failure_message="failed",
                        invalid_response_message="invalid",
                    )

    def test_json_request_rejects_non_finite_outgoing_values(self) -> None:
        with self.assertRaises(ValueError):
            web.json_request(
                "POST",
                "https://api.example.com/x",
                body={"value": float("nan")},
                failure_message="failed",
                invalid_response_message="invalid",
            )

    def test_default_cap_is_eight_mib(self) -> None:
        self.assertEqual(MAX_RESPONSE_BYTES, 8 * 1024 * 1024)

    def test_stream_request_sets_known_length_without_buffering_body(self) -> None:
        class Response:
            status = 204

            def read(self, size: int) -> bytes:
                return b""

        class Connection:
            def __init__(self) -> None:
                self.seen: tuple[Any, ...] = ()

            def request(self, *args: object, **kwargs: object) -> None:
                self.seen = (*args, kwargs)

            def getresponse(self) -> Response:
                return Response()

            def close(self) -> None:
                pass

        connection = Connection()
        source = io.BytesIO(b"video")
        with patch.object(web.http.client, "HTTPSConnection", return_value=connection):
            self.assertEqual(
                stream_request_bytes(
                    "POST",
                    "https://uploads.example.com/path?signature=x",
                    headers={"Content-Type": "video/mp4"},
                    body=source,
                    content_length=5,
                    failure_message="upload failed",
                ),
                b"",
            )
        self.assertEqual(connection.seen[0:2], ("POST", "/path?signature=x"))
        request_kwargs = connection.seen[2]
        self.assertIs(request_kwargs["body"], source)
        headers = request_kwargs["headers"]
        self.assertEqual(headers["Content-Length"], "5")

    def test_stream_request_rejects_non_https_url(self) -> None:
        with self.assertRaises(WebRequestError):
            stream_request_bytes(
                "POST",
                "http://uploads.example.com/path",
                headers={},
                body=io.BytesIO(b"x"),
                content_length=1,
                failure_message="upload failed",
            )

    def test_request_rejects_non_https_url(self) -> None:
        # http:// must fail as a mapped WebRequestError, not a raw RuntimeError.
        with self.assertRaises(WebRequestError):
            request_bytes("GET", "http://api.example.com/x", failure_message="failed")

    def test_redirects_are_refused_not_followed(self) -> None:
        # A provider open-redirect must never make the opener replay the
        # Authorization header (or any request) to the redirect target: the
        # hardened handler turns the 3xx into a mapped failure.
        import urllib.error

        redirected_to: list[str] = []

        class _CapturingOpener:
            def open(self, request: Any, timeout: object = None) -> Any:
                redirected_to.append(request.full_url)
                raise urllib.error.HTTPError(request.full_url, 302, "Found", {}, None)

        with patch.object(web, "_OPENER", _CapturingOpener()):
            with self.assertRaises(WebRequestError) as ctx:
                request_bytes(
                    "GET",
                    "https://api.example.com/x",
                    headers={"Authorization": "Bearer secret-token"},
                    failure_message="failed",
                )
        self.assertEqual(str(ctx.exception), "failed")
        # The token-bearing request was never re-sent to any redirect target.
        self.assertEqual(redirected_to, ["https://api.example.com/x"])

    def test_no_redirect_handler_returns_none(self) -> None:
        # Unit-level proof that the handler declines every redirect.
        self.assertIsNone(
            web._NoRedirectHandler().redirect_request(None, None, 302, "Found", {}, "http://evil.example/")
        )

    def test_post_connect_errors_map_to_failure_message(self) -> None:
        import http.client

        def _raise_incomplete(*a: object, **k: object) -> Any:
            raise http.client.IncompleteRead(b"partial")

        with patch.object(web._OPENER, "open", new=_raise_incomplete):
            with self.assertRaises(WebRequestError) as ctx:
                request_bytes("GET", "https://api.example.com/x", failure_message="failed")
        self.assertEqual(str(ctx.exception), "failed")


if __name__ == "__main__":
    unittest.main()
