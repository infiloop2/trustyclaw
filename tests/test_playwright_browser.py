from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tests.smoke.playwright_browser import (
    CHROME_EXECUTABLE_ENV,
    BrowserTarget,
    ChromeBrowser,
    _launch_options,
)


class PlaywrightBrowserTests(unittest.TestCase):
    def test_default_launch_uses_the_runner_chrome_channel(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            options = _launch_options()

        self.assertEqual(options["channel"], "chrome")
        self.assertTrue(options["headless"])
        self.assertIn("--no-sandbox", options["args"])

    def test_configured_chrome_path_replaces_the_runner_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp, "chrome")
            executable.write_text("")
            with patch.dict(os.environ, {CHROME_EXECUTABLE_ENV: str(executable)}, clear=True):
                options = _launch_options()

        self.assertEqual(options["executable_path"], str(executable))
        self.assertNotIn("channel", options)

    def test_browser_launches_navigates_and_closes_through_playwright(self) -> None:
        manager = MagicMock()
        playwright = manager.start.return_value
        browser = playwright.chromium.launch.return_value
        context = browser.new_context.return_value
        page = context.new_page.return_value

        with (
            patch("tests.smoke.playwright_browser.sync_playwright", return_value=manager),
            patch.dict(os.environ, {}, clear=True),
        ):
            smoke_browser = ChromeBrowser("http://127.0.0.1:3010/")
            smoke_browser.close()

        playwright.chromium.launch.assert_called_once_with(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            channel="chrome",
        )
        browser.new_context.assert_called_once_with(viewport={"width": 1440, "height": 1000})
        page.goto.assert_called_once_with(
            "http://127.0.0.1:3010/",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        context.close.assert_called_once_with()
        browser.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()

    def test_navigation_failure_closes_the_browser_and_stops_playwright(self) -> None:
        manager = MagicMock()
        playwright = manager.start.return_value
        browser = playwright.chromium.launch.return_value
        context = browser.new_context.return_value
        page = context.new_page.return_value
        page.goto.side_effect = RuntimeError("net::ERR_CONNECTION_REFUSED")

        with (
            patch("tests.smoke.playwright_browser.sync_playwright", return_value=manager),
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(RuntimeError, "ERR_CONNECTION_REFUSED"),
        ):
            ChromeBrowser("http://127.0.0.1:3010/")

        context.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()

    def test_target_returns_the_matching_app_frame(self) -> None:
        frame = MagicMock()
        frame.url = "http://127.0.0.1:3010/v1/apps/mission_pursuit/ui/index.html"
        frame.evaluate.return_value = "ready"
        browser = object.__new__(ChromeBrowser)
        browser._page = MagicMock()
        browser._page.frames = [MagicMock(), frame]
        browser._page.frames[0].url = "http://127.0.0.1:3010/"

        target = browser.target("/v1/apps/mission_pursuit/ui/index.html")

        self.assertIsInstance(target, BrowserTarget)
        self.assertEqual(target.evaluate("document.readyState"), "ready")


if __name__ == "__main__":
    unittest.main()
