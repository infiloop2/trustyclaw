#!/usr/bin/env python3
"""Playwright smoke test for the admin UI against the local mock backend."""

from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.request


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER = REPO_ROOT / "tests/smoke-ui/run_admin_ui_mock.py"
VERSION = (REPO_ROOT / "VERSION").read_text().strip()
PASSWORD = "dev"
PLAYWRIGHT_CACHE = Path.home() / ".cache/ms-playwright"
CHROMIUM_EXECUTABLE_ENV = "PLAYWRIGHT_CHROMIUM_EXECUTABLE"
IPHONE_VIEWPORT = {"width": 390, "height": 844}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    port = args.port or free_port()
    server = subprocess.Popen(
        [sys.executable, str(SERVER), "--port", str(port)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_for_server(port, server)
        run_browser_smoke(f"http://127.0.0.1:{port}/", headed=args.headed)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="Local port to use; defaults to a free ephemeral port.")
    parser.add_argument("--headed", action="store_true", help="Run the browser visibly.")
    return parser.parse_args(argv)


def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_server(port: int, proc: subprocess.Popen[str]) -> None:
    deadline = time.time() + 10
    url = f"http://127.0.0.1:{port}/"
    while time.time() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"mock server exited early with {proc.returncode}\n{output}")
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"mock server did not become ready at {url}")


def run_browser_smoke(url: str, *, headed: bool) -> None:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Playwright is not installed. Run:\n"
            "  python3 -m pip install -r tests/requirements.txt\n"
            "  python3 -m playwright install chromium"
        ) from exc

    with sync_playwright() as playwright:
        executable_path = chromium_executable_path()
        launch_options = {"headless": not headed}
        if executable_path:
            launch_options["executable_path"] = executable_path
        try:
            browser = playwright.chromium.launch(**launch_options)
        except PlaywrightError as exc:
            raise SystemExit(
                "Playwright Chromium is not installed. Run:\n"
                "  python3 -m playwright install chromium\n"
                f"or set {CHROMIUM_EXECUTABLE_ENV} to an existing Chromium/Chrome executable."
            ) from exc
        try:
            desktop = browser.new_context()
            desktop_smoke(desktop.new_page(), url)
            desktop.close()

            mobile = browser.new_context(viewport=IPHONE_VIEWPORT, device_scale_factor=3, is_mobile=True, has_touch=True)
            mobile_smoke(mobile.new_page(), url)
            mobile.close()
        finally:
            browser.close()


def log_in(page, url: str) -> None:
    from playwright.sync_api import expect

    page.goto(url)
    expect(page.locator("#login")).to_be_visible()
    page.locator("#password").fill(PASSWORD)
    page.get_by_role("button", name="Log in").click()
    expect(page.locator("#app")).to_be_visible()


def desktop_smoke(page, url: str) -> None:
    from playwright.sync_api import expect

    log_in(page, url)
    expect(page.locator("body")).to_contain_text("trustyclaw-mock")
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page.locator("#health")).to_contain_text("ok")
    expect(page.locator("#health")).to_contain_text(f"runtime {VERSION}")
    expect(page.locator("#health")).to_contain_text(f"state {VERSION}")
    expect(page.locator("#panel-home").get_by_role("button", name="Reboot host")).to_be_visible()
    expect(page.locator("#runtime")).to_contain_text("codex")
    expect(page.locator("#runtime-guidance")).to_contain_text(
        "OpenAI provider access is disabled in the active network policy"
    )
    expect(page.locator("#runtime-guidance")).to_contain_text("Open Network settings")
    expect(page.locator("#runtime-guidance")).to_contain_text(
        "Claude provider access is disabled in the active network policy"
    )
    expect(page.get_by_role("button", name="Start Codex login")).to_have_count(0)
    expect(page.get_by_role("button", name="Start Claude login")).to_have_count(0)

    page.get_by_role("button", name="Agent", exact=True).click()
    expect(page.locator("#panel-agent")).to_be_visible()
    expect(page.locator("#panel-agent").get_by_role("button", name="Reboot host")).to_have_count(0)
    expect(page.locator("#panel-agent #runtime")).to_have_count(0)
    expect(page.locator("#panel-agent #provider-accounts")).to_have_count(0)
    expect(page.locator("#panel-agent .thread-pane")).to_be_visible()
    expect(page.locator("#composer-target")).to_have_text("New thread")

    # Seeded history from the mock is visible before any new work is created.
    expect(page.locator("#threads")).to_contain_text("website-redesign")
    expect(page.locator("#threads")).to_contain_text("dependency-audit")
    page.locator("#threads .thread-item", has_text="website-redesign").click()
    expect(page.locator("#composer-target")).to_contain_text("website-redesign")
    expect(page.locator("#thread-detail")).to_contain_text("failed")
    # Tasks appear in chronological order with their results inline.
    expect(page.locator("#thread-detail .task-card").nth(0)).to_contain_text("Audit the marketing site")
    expect(page.locator("#thread-detail .task-card").nth(1)).to_contain_text("denied by policy")
    page.locator("#thread-detail .task-card", has_text="failed").get_by_role("button", name="Events").click()
    expect(page.locator("#task-events-detail")).to_contain_text("denied by policy")

    page.get_by_role("button", name="+ New thread").click()
    expect(page.locator("#composer-target")).to_have_text("New thread")
    page.locator("#new-task").fill("browser smoke task")
    page.locator("#new-task-thread").fill("smoke-ui")
    page.locator("#new-task-runtime").select_option("codex")
    page.get_by_role("button", name="Create task").click()
    expect(page.locator("#thread-detail")).to_contain_text("browser smoke task")
    expect(page.locator("#thread-detail")).to_contain_text("queued")
    expect(page.locator("#threads")).to_contain_text("smoke-ui")
    page.locator("#thread-detail .task-card", has_text="browser smoke task").get_by_role("button", name="Events").click()
    expect(page.locator("#task-events-detail")).to_contain_text("task.created")

    page.get_by_role("button", name="Agent audit log").click()
    expect(page.locator("#panel-agent-log")).to_be_visible()
    expect(page.locator("#events")).to_contain_text("task.created")
    expect(page.locator("#events")).to_contain_text("agent_runtime.deactivated")

    page.get_by_role("button", name="Agent workspace", exact=True).click()
    expect(page.locator("#panel-files")).to_be_visible()
    expect(page.locator("#file-list th").nth(0)).to_have_text("name")
    expect(page.locator("#file-list th").nth(1)).to_have_text("type")
    expect(page.locator("#file-list")).to_contain_text(".codex")
    page.locator("#file-list").get_by_role("button", name="workspace", exact=True).click()
    expect(page.locator("#file-path")).to_have_value("/workspace")
    page.locator("#file-list").get_by_role("button", name='bad" onclick="window.__xss=1" x=".txt').click()
    expect(page.locator("#file-content")).to_contain_text("quote-bearing mock file")
    if page.evaluate("() => window.__xss") is not None:
        raise AssertionError("quote-bearing filename executed as inline script")
    hostile_name = '<img src=x onerror="window.__fileNameXss=1">.txt'
    page.locator("#file-list").get_by_role("button", name=hostile_name).click()
    expect(page.locator("#file-viewer-title")).to_contain_text(hostile_name)
    expect(page.locator("#file-content")).to_contain_text("<script>window.__fileContentXss=1</script>")
    expect(page.locator("#file-content")).to_contain_text("Mock unsafe-looking file contents")
    if page.locator("#file-list img").count() != 0:
        raise AssertionError("hostile filename/type rendered as HTML in file list")
    if page.locator("#file-content img").count() != 0:
        raise AssertionError("hostile file content rendered as HTML")
    executed = page.evaluate(
        "() => [window.__fileNameXss, window.__fileTypeXss, "
        "window.__fileContentXss, window.__fileContentImageXss]"
    )
    if any(value is not None for value in executed):
        raise AssertionError(f"hostile file explorer value executed script: {executed}")
    page.locator("#file-list").get_by_role("button", name="notes.txt").click()
    expect(page.locator("#file-viewer-title")).to_contain_text("/workspace/notes.txt")
    expect(page.locator("#file-content")).to_contain_text("Mobile audit fixes")

    page.get_by_role("button", name="Network audit log").click()
    expect(page.locator("#panel-net-log")).to_be_visible()
    expect(page.locator("#net-events")).to_contain_text("deploy.acme.dev")
    expect(page.locator("#net-events")).to_contain_text("denied")

    page.get_by_role("button", name="Internet Access and Tools").click()
    expect(page.locator("#panel-network")).to_be_visible()
    expect(page.locator("#active-policy")).to_have_value(re.compile(r'"managed_ai_provider_network_access": \{\}'))
    expect(page.locator("#policy-status")).to_contain_text("Proposal matches current policy")
    page.get_by_label("OpenAI preset domains").click()
    expect(page.locator("#preset-info-popover")).to_contain_text("api.openai.com")
    page.get_by_label("GitHub preset domains").click()
    expect(page.locator("#preset-info-popover")).to_contain_text("api.github.com")

    page.get_by_role("button", name="Add GitHub").click()
    expect(page.locator("#policy-status")).to_contain_text("unapplied changes")
    expect(page.get_by_role("button", name="Remove GitHub")).to_be_enabled()
    expect(page.locator("#active-policy")).to_have_value(re.compile(r'"managed_ai_provider_network_access": \{\}'))

    page.get_by_role("button", name="Remove GitHub").click()
    expect(page.locator("#policy-status")).to_contain_text("Proposal matches current policy")
    expect(page.get_by_role("button", name="Add GitHub")).to_be_enabled()

    page.get_by_role("button", name="Add OpenAI").click()
    expect(page.get_by_role("button", name="Remove OpenAI")).to_be_enabled()
    page.get_by_role("button", name="Add Claude").click()
    expect(page.get_by_role("button", name="Remove Claude")).to_be_enabled()
    page.get_by_role("button", name="Add GitHub").click()
    expect(page.locator("#policy-status")).to_contain_text("unapplied changes")
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Replace active policy with proposal").click()
    expect(page.locator("#policy-message")).to_contain_text("Active network policy replaced")
    expect(page.locator("#active-policy")).to_have_value(re.compile(r'"openai": true'))
    expect(page.locator("#active-policy")).to_have_value(re.compile(r'"claude": true'))
    expect(page.locator("#active-policy")).to_have_value(re.compile(r'"github.com"'))

    page.get_by_role("button", name="Home").click()
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page.get_by_role("button", name="Start Codex login")).to_be_visible()
    expect(page.get_by_role("button", name="Start Codex login")).to_be_enabled()
    expect(page.get_by_role("button", name="Start Claude login")).to_be_visible()
    expect(page.get_by_role("button", name="Start Claude login")).to_be_enabled()
    page.get_by_role("button", name="Start Codex login").click()
    expect(page.locator("#oauth")).to_contain_text("MOCK-CODEX")
    page.evaluate("() => tick()")
    expect(page.get_by_role("button", name="Start Codex login")).to_have_count(0)
    expect(page.locator("#provider-accounts")).to_contain_text("acct_mock_openai")
    expect(page.locator("#provider-accounts")).to_contain_text("akshay@infiloop.io")
    expect(page.locator("#provider-accounts")).to_contain_text("pro")
    expect(page.locator("#provider-accounts")).to_contain_text("5 hour: 60%")
    expect(page.locator("#provider-accounts")).to_contain_text("weekly: 20%")
    expect(page.locator("#provider-accounts")).to_contain_text("credits: none")
    expect(page.locator("#provider-accounts")).to_contain_text("checked")

    expect(page.get_by_role("button", name="Start Claude login")).to_be_visible()
    expect(page.get_by_role("button", name="Start Claude login")).to_be_enabled()
    page.get_by_role("button", name="Start Claude login").click()
    expect(page.locator("#oauth")).to_contain_text("Claude Code login")
    page.once("dialog", lambda dialog: dialog.accept("mock-code"))
    page.get_by_role("button", name="Submit code").click()
    page.evaluate("() => tick()")
    expect(page.get_by_role("button", name="Start Claude login")).to_have_count(0)
    expect(page.locator("#provider-accounts")).to_contain_text("acct_mock_claude")
    expect(page.locator("#provider-accounts")).to_contain_text("claude@example.invalid")
    expect(page.locator("#provider-accounts")).to_contain_text("max")
    expect(page.locator("#provider-accounts")).to_contain_text("current session: 14%")
    expect(page.locator("#provider-accounts")).to_contain_text("weekly: 31%")
    expect(page.locator("#provider-accounts")).to_contain_text("resets Jul 7, 3:59pm (UTC)")
    expect(page.locator("#provider-accounts")).to_contain_text("checked")

    # With providers enabled and login done, the queued smoke task starts
    # running on its own and completes after several seconds. The selected
    # thread stays open and auto-refreshes.
    page.get_by_role("button", name="Agent", exact=True).click()
    expect(page.locator("#thread-detail")).to_contain_text("browser smoke task")
    expect(page.locator("#thread-detail .task-card", has_text="browser smoke task")).to_contain_text(
        "completed", timeout=45000
    )
    page.locator("#thread-detail .task-card", has_text="browser smoke task").get_by_role("button", name="Events").click()
    expect(page.locator("#task-events-detail")).to_contain_text("task.completed")


def mobile_smoke(page, url: str) -> None:
    """iPhone-sized pass: layout must not overflow and core flows must work."""
    from playwright.sync_api import expect

    log_in(page, url)
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page.locator("#health")).to_contain_text("ok")
    assert_no_horizontal_overflow(page, "home")

    page.get_by_role("button", name="Agent", exact=True).click()
    expect(page.locator("#panel-agent")).to_be_visible()
    expect(page.locator("#threads")).to_contain_text("website-redesign")
    page.locator("#threads .thread-item", has_text="website-redesign").click()
    expect(page.locator("#thread-detail")).to_contain_text("denied by policy")
    assert_no_horizontal_overflow(page, "agent")

    page.get_by_role("button", name="Agent audit log").click()
    expect(page.locator("#events")).to_contain_text("task.created")
    assert_no_horizontal_overflow(page, "agent event log")

    page.get_by_role("button", name="Agent workspace", exact=True).click()
    expect(page.locator("#file-list")).to_contain_text("workspace")
    assert_no_horizontal_overflow(page, "agent workspace")

    page.get_by_role("button", name="Internet Access and Tools").click()
    expect(page.locator("#panel-network")).to_be_visible()
    assert_no_horizontal_overflow(page, "internet access")

    page.get_by_role("button", name="Network audit log").click()
    expect(page.locator("#net-events")).to_contain_text("deploy.acme.dev")
    assert_no_horizontal_overflow(page, "network audit log")


def assert_no_horizontal_overflow(page, panel: str) -> None:
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    if overflow > 1:
        raise AssertionError(f"{panel} panel overflows horizontally by {overflow}px on a phone viewport")


def chromium_executable_path() -> str | None:
    configured = os.environ.get(CHROMIUM_EXECUTABLE_ENV)
    if configured:
        configured_path = Path(configured)
        if configured_path.is_file():
            return str(configured_path)
        raise SystemExit(f"{CHROMIUM_EXECUTABLE_ENV} does not point to a file: {configured}")

    for pattern in (
        "chromium-*/chrome-linux/chrome",
        "chromium_headless_shell-*/chrome-linux/headless_shell",
    ):
        for candidate in sorted(PLAYWRIGHT_CACHE.glob(pattern), reverse=True):
            if candidate.is_file():
                return str(candidate)
    return None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
