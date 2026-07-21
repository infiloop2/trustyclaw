"""Playwright wrapper for the credential-bearing fresh AWS smoke.

The workflow installs the pinned Playwright driver before AWS credentials are
injected, then this module uses the hosted runner's preinstalled Chrome. No
browser binary or package is downloaded in the credential-bearing step.
"""

from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Any

from playwright.sync_api import Frame, sync_playwright


CHROME_EXECUTABLE_ENV = "TRUSTYCLAW_SMOKE_CHROME"


def _launch_options() -> dict[str, Any]:
    options: dict[str, Any] = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    }
    configured = os.environ.get(CHROME_EXECUTABLE_ENV)
    if configured:
        if not Path(configured).is_file():
            raise RuntimeError(f"{CHROME_EXECUTABLE_ENV} does not point to a file: {configured}")
        options["executable_path"] = configured
    else:
        options["channel"] = "chrome"
    return options


class BrowserTarget:
    """A sandboxed app iframe exposed as a Playwright frame."""

    def __init__(self, frame: Frame) -> None:
        self._frame = frame

    def evaluate(self, expression: str) -> Any:
        return self._frame.evaluate(expression)

    def wait_for(self, expression: str, *, timeout: float = 30.0, description: str) -> Any:
        deadline = time.monotonic() + timeout
        last_value: Any = None
        while time.monotonic() < deadline:
            last_value = self.evaluate(expression)
            if last_value:
                return last_value
            time.sleep(0.1)
        raise AssertionError(f"browser timed out waiting for {description}; last value: {last_value!r}")


class ChromeBrowser:
    """One Playwright-controlled Chrome page for the live smoke."""

    def __init__(self, url: str) -> None:
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(**_launch_options())
            self._context = self._browser.new_context(viewport={"width": 1440, "height": 1000})
            self._page = self._context.new_page()
            self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            context = getattr(self, "_context", None)
            if context is not None:
                context.close()
            else:
                browser = getattr(self, "_browser", None)
                if browser is not None:
                    browser.close()
            self._playwright.stop()
            raise

    def evaluate(self, expression: str) -> Any:
        return self._page.evaluate(expression)

    def wait_for(
        self,
        expression: str,
        *,
        timeout: float = 30.0,
        description: str,
    ) -> Any:
        deadline = time.monotonic() + timeout
        last_value: Any = None
        while time.monotonic() < deadline:
            last_value = self.evaluate(expression)
            if last_value:
                return last_value
            time.sleep(0.1)
        raise AssertionError(f"browser timed out waiting for {description}; last value: {last_value!r}")

    def target(self, url_fragment: str, *, timeout: float = 30.0) -> BrowserTarget:
        deadline = time.monotonic() + timeout
        frame_urls: list[str] = []
        iframe_sources: list[str] = []
        while time.monotonic() < deadline:
            # A sandboxed frame without allow-same-origin has an opaque origin.
            # Some hosted Chrome/CDP combinations report its Frame.url as an
            # empty string even after navigation. The operator-visible iframe
            # src is still authoritative for selecting the browsing context;
            # the following app-level wait proves its document actually loaded.
            iframe_sources = []
            for locator in self._page.locator("iframe").all():
                src = locator.get_attribute("src") or ""
                iframe_sources.append(src[:512])
                if url_fragment not in src:
                    continue
                handle = locator.element_handle()
                selected = handle.content_frame() if handle is not None else None
                if selected is not None:
                    return BrowserTarget(selected)
            frames = self._page.frames
            frame_urls = [frame.url[:512] for frame in frames[:8]]
            selected = next((frame for frame in frames if url_fragment in frame.url), None)
            if selected is not None:
                return BrowserTarget(selected)
            time.sleep(0.1)
        raise AssertionError(
            f"browser timed out waiting for frame URL containing {url_fragment!r}; "
            f"current frame URLs: {frame_urls!r}; iframe src values: {iframe_sources!r}"
        )

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            try:
                self._browser.close()
            finally:
                self._playwright.stop()

    def __enter__(self) -> "ChromeBrowser":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
