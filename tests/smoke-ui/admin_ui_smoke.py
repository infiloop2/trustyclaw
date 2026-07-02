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
PASSWORD = "dev"
PLAYWRIGHT_CACHE = Path.home() / ".cache/ms-playwright"
CHROMIUM_EXECUTABLE_ENV = "PLAYWRIGHT_CHROMIUM_EXECUTABLE"


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
        from playwright.sync_api import expect, sync_playwright
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
        page = browser.new_page()
        try:
            page.goto(url)
            expect(page.locator("#login")).to_be_visible()
            page.locator("#password").fill(PASSWORD)
            page.get_by_role("button", name="Login").click()

            expect(page.locator("#app")).to_be_visible()
            expect(page.locator("body")).to_contain_text("trustyclaw-mock")
            expect(page.locator("#panel-home")).to_be_visible()
            expect(page.locator("#health")).to_contain_text("ok")
            expect(page.locator("#health")).to_contain_text("runtime 0.2.0")
            expect(page.locator("#health")).to_contain_text("state 0.2.0")
            expect(page.locator("#panel-home").get_by_role("button", name="Reboot host")).to_be_visible()
            expect(page.locator("#runtime")).to_contain_text("codex")
            expect(page.locator("#runtime-guidance")).to_contain_text(
                "OpenAI provider access is disabled in the active network policy"
            )
            expect(page.locator("#runtime-guidance")).to_contain_text("Open Network settings")
            expect(page.locator("#runtime-guidance")).to_contain_text(
                "Claude provider access is disabled in the active network policy"
            )
            expect(page.locator("#runtime")).not_to_contain_text("active tasks")
            expect(page.get_by_role("button", name="Start Codex login")).to_have_count(0)
            expect(page.get_by_role("button", name="Start Claude login")).to_have_count(0)

            page.get_by_role("button", name="Agent").click()
            expect(page.locator("#panel-agent")).to_be_visible()
            expect(page.locator("#panel-agent").get_by_role("button", name="Reboot host")).to_have_count(0)
            expect(page.locator("#panel-agent #runtime")).to_have_count(0)
            expect(page.locator("#panel-agent #provider-accounts")).to_have_count(0)
            expect(page.locator("#panel-agent > section").nth(0)).to_contain_text("Threads")
            expect(page.locator("#panel-agent > section").nth(1)).to_contain_text("Tasks")

            page.locator("#new-task").fill("browser smoke task")
            page.locator("#new-task-thread").fill("smoke-ui")
            page.locator("#new-task-runtime").select_option("codex")
            page.get_by_role("button", name="Create task").click()
            expect(page.locator("#tasks")).to_contain_text("browser smoke task")
            expect(page.locator("#threads")).to_contain_text("smoke-ui")

            page.locator("#threads").get_by_role("button", name="Open").first.click()
            expect(page.locator("#thread-detail")).to_contain_text("browser smoke task")
            page.locator("#thread-detail").get_by_role("button", name="Events").first.click()
            expect(page.locator("#task-events-detail")).to_contain_text("task.created")

            page.get_by_role("button", name="Files", exact=True).click()
            expect(page.locator("#panel-files")).to_be_visible()
            expect(page.locator("#file-list th").nth(0)).to_have_text("name")
            expect(page.locator("#file-list th").nth(1)).to_have_text("type")
            expect(page.locator("#file-list")).to_contain_text(".codex")
            page.locator("#file-list").get_by_role("button", name="workspace").click()
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
            expect(page.locator("#file-content")).to_contain_text("Mock workspace file contents")

            page.get_by_role("button", name="Network", exact=True).click()
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
            expect(page.locator("#provider-accounts")).to_contain_text("pro")
            expect(page.locator("#provider-accounts")).to_contain_text("current session: 0%")
            expect(page.locator("#provider-accounts")).to_contain_text("weekly: 0%")
            expect(page.locator("#provider-accounts")).to_contain_text("resets Jul 3, 3:59pm (UTC)")
            expect(page.locator("#provider-accounts")).to_contain_text("checked")
        finally:
            browser.close()


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
