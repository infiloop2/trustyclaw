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

import app_smokes


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
        run_browser_smoke(f"http://127.0.0.1:{port}/", headed=args.headed, scope=args.scope)
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
    parser.add_argument(
        "--scope",
        choices=("all", "core", "apps"),
        default="all",
        help="Smoke only the host UI core, only installed apps, or both.",
    )
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


def run_browser_smoke(url: str, *, headed: bool, scope: str) -> None:
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
            if scope in {"all", "core"}:
                desktop = browser.new_context()
                desktop_smoke(desktop.new_page(), url)
                desktop.close()

                mobile = browser.new_context(
                    viewport=IPHONE_VIEWPORT, device_scale_factor=3, is_mobile=True, has_touch=True
                )
                mobile_smoke(mobile.new_page(), url)
                mobile.close()

            if scope in {"all", "apps"}:
                desktop_apps = browser.new_context()
                app_page = desktop_apps.new_page()
                log_in(app_page, url)
                app_smokes.desktop_smoke(app_page)
                desktop_apps.close()

                mobile_apps = browser.new_context(
                    viewport=IPHONE_VIEWPORT, device_scale_factor=3, is_mobile=True, has_touch=True
                )
                app_mobile_page = mobile_apps.new_page()
                log_in(app_mobile_page, url)
                app_smokes.mobile_smoke(app_mobile_page)
                mobile_apps.close()
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
    asset_urls = page.evaluate(
        "() => ["
        "document.querySelector('link[rel=\"stylesheet\"]').href,"
        "document.querySelector('link[rel=\"icon\"]').href,"
        "document.querySelector('link[rel=\"alternate icon\"]').href,"
        "document.querySelector('script[src]').src"
        "]"
    )
    if not all(f"v={VERSION}" in asset_url for asset_url in asset_urls):
        raise AssertionError(f"admin UI assets were not versioned: {asset_urls}")
    expect(page.locator("#agent-name")).to_have_text("Agent: trustyclaw-mock")
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page.locator("#health")).to_contain_text("ok")
    expect(page.locator("#health")).to_contain_text(f"runtime {VERSION}")
    expect(page.locator("#health")).to_contain_text(f"state {VERSION}")
    expect(page.locator("#health")).to_contain_text("Memory")
    expect(page.locator("#health")).to_contain_text("Admin volume")
    expect(page.locator("#health")).to_contain_text("Agent volume")
    expect(page.locator("#panel-home").get_by_role("button", name="Reboot host")).to_be_visible()
    expect(page.locator("#runtime")).to_contain_text("codex")
    expect(page.locator("#runtime-guidance")).to_contain_text(
        "OpenAI provider access is disabled in the active network policy"
    )
    expect(page.locator("#runtime-guidance")).to_contain_text("Open Internet Access and Tools")
    expect(page.locator("#runtime-guidance")).to_contain_text(
        "Claude provider access is disabled in the active network policy"
    )
    expect(page.get_by_role("button", name="Start Codex login")).to_have_count(0)
    expect(page.get_by_role("button", name="Start Claude login")).to_have_count(0)

    page.get_by_role("button", name="Agent session log", exact=True).click()
    expect(page.locator("#panel-agent")).to_be_visible()
    expect(page.locator("#panel-agent").get_by_role("button", name="Reboot host")).to_have_count(0)
    expect(page.locator("#panel-agent #runtime")).to_have_count(0)
    expect(page.locator("#panel-agent #provider-accounts")).to_have_count(0)
    expect(page.locator("#panel-agent .thread-pane")).to_be_visible()
    expect(page.locator("#thread-detail .thread-title")).to_have_text("Agent session log")
    expect(page.locator("#panel-agent").get_by_role("button", name="+ New session")).to_have_count(0)
    expect(page.locator("#panel-agent").get_by_role("button", name="Create task")).to_have_count(0)
    expect(page.locator("#new-task")).to_have_count(0)
    expect(page.locator(".composer")).to_have_count(0)

    # Seeded history from the mock is visible before any new work is created.
    expect(page.locator("#threads")).to_contain_text("website-redesign")
    expect(page.locator("#threads")).to_contain_text("dependency-audit")
    page.locator("#threads .thread-item", has_text="website-redesign").click()
    expect(page.locator("#thread-detail")).to_contain_text("failed")
    expect(page.locator("#thread-detail .thread-head")).to_have_css("border-bottom-width", "0px")
    expect(page.locator(".composer")).to_have_count(0)
    # Tasks appear newest-first with their results inline.
    newest_card = page.locator("#thread-detail .task-card").nth(0)
    older_card = page.locator("#thread-detail .task-card").nth(1)
    expect(newest_card).to_contain_text("denied by policy")
    expect(older_card).to_contain_text("Audit the marketing site")
    older_card.get_by_role("button", name="Events").click()
    expect(older_card).to_contain_text("task.created")
    expect(page.locator("#task-events-detail")).to_have_text("")
    expect(older_card.get_by_role("button", name="Hide events")).to_be_visible()
    failed_card = newest_card
    failed_card.get_by_role("button", name="Events").click()
    expect(failed_card).to_contain_text("denied by policy")
    expect(page.locator("#task-events-detail")).to_have_text("")
    expect(failed_card.get_by_role("button", name="Hide events")).to_be_visible()
    expect(older_card.get_by_role("button", name="Hide events")).to_be_visible()
    expect(failed_card.get_by_role("button", name="Refresh task")).to_have_count(0)
    expect(failed_card.get_by_label("Steering message")).to_have_count(0)
    expect(failed_card.get_by_role("button", name="Steer")).to_have_count(0)
    failed_card.get_by_role("button", name="Hide events").click()
    expect(page.locator("#task-events-detail")).to_have_text("")
    expect(failed_card.get_by_role("button", name="Events")).to_be_visible()
    expect(older_card.get_by_role("button", name="Hide events")).to_be_visible()
    failed_card.get_by_role("button", name="Events").click()
    expect(failed_card).to_contain_text("denied by policy")

    page.locator("#threads .thread-item", has_text="incident-response").click()
    running_card = page.locator("#thread-detail .task-card", has_text="running")
    expect(running_card.get_by_role("button", name="Events")).to_be_visible()
    expect(running_card.get_by_role("button", name="Refresh task")).to_have_count(0)
    expect(running_card.get_by_role("button", name="Steer")).to_have_count(0)
    expect(running_card.get_by_role("button", name="Kill")).to_have_count(0)
    expect(running_card.get_by_label("Steering message")).to_have_count(0)
    page.locator("#threads .thread-item", has_text="docs-cleanup").click()
    queued_card = page.locator("#thread-detail .task-card", has_text="queued")
    expect(queued_card.get_by_role("button", name="Events")).to_be_visible()
    expect(queued_card.get_by_role("button", name="Refresh task")).to_have_count(0)
    expect(queued_card.get_by_role("button", name="Cancel")).to_have_count(0)
    expect(queued_card.get_by_label("Steering message")).to_have_count(0)

    with page.expect_response(lambda response: "/v1/events" in response.url):
        page.get_by_role("button", name="Agent audit log").click()
    expect(page.locator("#panel-agent-log")).to_be_visible()
    expect(page.locator("#panel-agent-log")).to_have_css("opacity", "1")
    expect(page.locator("#events tr").nth(1)).to_be_visible()
    expect(page.locator("#events")).to_contain_text("task.created")
    expect(page.locator("#events")).to_contain_text("agent_runtime.deactivated")
    expect(page.locator("#agent-page-summary")).to_contain_text("Page 1")
    expect(page.locator("#agent-page-summary")).to_contain_text("live")
    expect(page.locator("#agent-event-pager")).to_contain_text("Next")

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

    with page.expect_response(lambda response: "/v1/network/events" in response.url):
        page.get_by_role("button", name="Network audit log").click()
    expect(page.locator("#panel-net-log")).to_be_visible()
    expect(page.locator("#panel-net-log")).to_have_css("opacity", "1")
    expect(page.locator("#net-events tr").nth(1)).to_be_visible()
    expect(page.locator("#net-events")).to_contain_text("deploy.acme.dev")
    expect(page.locator("#net-events")).to_contain_text("denied")
    expect(page.locator("#net-events")).to_contain_text("Host is not in the allowed network policy")
    expect(page.locator("#net-page-summary")).to_contain_text("Page 1")
    expect(page.locator("#net-event-pager")).to_contain_text("Next")
    expect(page.locator("#net-events")).to_contain_text("https://api.github.com:443/repos/acme/infra/actions/runs?status=failure")
    page.get_by_role("button", name="Show denied").click()
    expect(page.get_by_role("button", name="Show all")).to_be_visible()
    expect(page.locator("#net-page-summary")).to_contain_text("denied only")
    expect(page.locator("#net-events")).to_contain_text("Host is not in the allowed network policy")
    expect(page.locator("#net-events")).not_to_contain_text("api.openai.com")

    page.get_by_role("button", name="Internet Access and Tools").click()
    expect(page.locator("#panel-network")).to_be_visible()
    expect(page.locator("#panel-network")).not_to_contain_text("Managed integrations")
    expect(page.locator("#panel-network")).not_to_contain_text("Curated access bundles")
    # Every integration renders as its own card row with a compact header,
    # status, and separate enable/disable buttons.
    for label in ("OpenAI", "Claude", "GitHub", "Python packages", "npm packages"):
        row = page.locator(".integration-row", has_text=label)
        expect(row).to_contain_text("disabled")
        expect(row.get_by_role("button", name="Enable", exact=True)).to_be_enabled()
        expect(row.get_by_role("button", name="Disable", exact=True)).to_be_disabled()
    github_row = page.locator(".integration-row", has_text="GitHub")
    expect(github_row.locator(".preset-with-info h2")).to_have_text("GitHub")
    expect(github_row.locator(".preset-with-info h2")).not_to_contain_text("all reads")
    page.get_by_label("OpenAI internet access details").click()
    expect(page.locator("#preset-info-popover")).to_contain_text("This integration enables direct internet access")
    expect(page.locator("#preset-info-popover")).to_contain_text("api.openai.com")
    page.get_by_label("GitHub internet access details").click()
    expect(page.locator("#preset-info-popover")).to_contain_text("api.github.com")
    expect(page.locator("#preset-info-popover")).to_contain_text("LFS uploads denied")
    expect(page.locator("#preset-info-popover")).to_contain_text("any public repository")
    expect(page.locator("#preset-info-popover")).to_contain_text("any private repository the token reaches")
    # The floating popover dismisses when focus moves anywhere outside it.
    page.mouse.click(8, 8)
    expect(page.locator("#preset-info-popover")).to_be_hidden()
    # Credentials can be staged before the integration is enabled.
    expect(page.locator("#github-credential-status")).to_contain_text("No credential configured")

    # The write-repository controls live in the GitHub dropdown, shown only
    # while GitHub is enabled.
    expect(page.locator("#github-expansion")).to_be_hidden()
    expect(github_row).to_contain_text("disabled")
    github_chevron = github_row.get_by_label("Toggle GitHub details")
    chevron_box = github_chevron.bounding_box()
    github_label_box = github_row.locator(".preset-with-info h2").bounding_box()
    if not chevron_box or not github_label_box:
        raise AssertionError("GitHub chevron and label should be visible")
    if chevron_box["x"] >= github_label_box["x"]:
        raise AssertionError("GitHub chevron should appear before the row label")
    if chevron_box["width"] < 34 or chevron_box["height"] < 34:
        raise AssertionError(f"GitHub chevron hit area is too small: {chevron_box}")
    github_chevron.click()
    expect(github_row).to_contain_text("All GitHub reads are allowed")
    expect(github_row).to_contain_text("scoped to the write repositories")
    expect(page.locator("#github-expansion")).to_be_hidden()
    expect(page.locator("#github-repo")).to_be_disabled()

    # Each integration enables on its own and applies immediately.
    page.locator(".integration-row", has_text="OpenAI").get_by_role("button", name="Enable", exact=True).click()
    expect(page.locator("#policy-message")).to_contain_text("OpenAI enabled")
    expect(page.locator(".integration-row", has_text="OpenAI")).to_contain_text("enabled")
    page.locator(".integration-row", has_text="Claude").get_by_role("button", name="Enable", exact=True).click()
    expect(page.locator("#policy-message")).to_contain_text("Claude enabled")
    # Provider linked-account controls live inside each provider dropdown.
    openai_row = page.locator(".integration-row", has_text="OpenAI")
    openai_row.get_by_label("Toggle OpenAI details").click()
    page.locator(".integration-row", has_text="Claude").get_by_label("Toggle Claude details").click()
    expect(openai_row).to_contain_text("No account linked yet")
    expect(page.locator(".integration-row", has_text="Claude")).to_contain_text("No account linked yet")
    page.once("dialog", lambda dialog: dialog.accept())
    openai_row.get_by_role("button", name="Reset linked account").click()
    expect(page.locator("#policy-message")).to_contain_text("OpenAI linked account reset")
    expect(openai_row).to_contain_text("No account linked yet")
    github_row.get_by_role("button", name="Enable", exact=True).click()
    expect(page.locator("#policy-message")).to_contain_text("GitHub enabled")
    # Enabling expands the row with the repository controls.
    expect(page.locator("#github-expansion")).to_be_visible()
    expect(page.locator("#github-repos")).to_contain_text("No write repositories configured")
    page.locator("#github-repo").fill("infiloop2/trustyclaw")
    page.get_by_role("button", name="Add write repository", exact=True).click()
    expect(page.locator("#policy-message")).to_contain_text("Write repository infiloop2/trustyclaw saved")
    repo_entry = page.locator(".repo-entry", has_text="infiloop2/trustyclaw")
    expect(repo_entry).to_contain_text("infiloop2/trustyclaw")
    expect(repo_entry).not_to_contain_text("read-write")
    expect(repo_entry).to_contain_text("1 warning")
    expect(repo_entry).not_to_contain_text("Repository audit could not verify this write target")
    repo_entry.get_by_label("Toggle repository audit details for infiloop2/trustyclaw").click()
    expect(repo_entry).to_contain_text("Repository audit could not verify this write target")
    expect(repo_entry).to_contain_text("no credential token to audit with")
    # The chevron on the GitHub card collapses and re-expands the details.
    page.get_by_label("Toggle GitHub details").click()
    expect(page.locator("#github-expansion")).to_be_hidden()
    page.get_by_label("Toggle GitHub details").click()
    expect(page.locator("#github-expansion")).to_be_visible()
    # Disabling asks for confirmation and applies immediately.
    page.locator(".integration-row", has_text="npm packages").get_by_role("button", name="Enable", exact=True).click()
    expect(page.locator("#policy-message")).to_contain_text("npm packages enabled")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator(".integration-row", has_text="npm packages").get_by_role("button", name="Disable", exact=True).click()
    expect(page.locator("#policy-message")).to_contain_text("npm packages disabled")
    expect(page.locator(".integration-row", has_text="npm packages")).to_contain_text("disabled")

    # Custom domain access is collapsed by default and shows a live count.
    expect(page.locator("#domain-rule-count")).to_have_text("0 domains enabled")
    expect(page.locator("#domain-rule-count")).to_have_class("status disabled")
    expect(page.locator("#custom-domain-details")).to_be_hidden()
    page.get_by_label("Toggle custom domain access details").click()
    expect(page.locator("#custom-domain-details")).to_be_visible()
    expect(page.locator("#domain-rules")).to_contain_text("No custom domains configured")
    page.locator("#policy-domain").fill("api.example.com")
    page.locator("#policy-methods").fill("GET,HEAD")
    page.get_by_role("button", name="Add domain rule", exact=True).click()
    expect(page.locator("#policy-message")).to_contain_text("Domain rule for api.example.com saved")
    expect(page.locator("#domain-rule-count")).to_have_text("1 domain enabled")
    expect(page.locator("#domain-rule-count")).to_have_class("status enabled")
    expect(page.locator("#domain-rules")).to_contain_text("api.example.com")
    expect(page.locator("#domain-rules")).to_contain_text("GET, HEAD")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#domain-rules").get_by_role("button", name="Remove", exact=True).click()
    expect(page.locator("#policy-message")).to_contain_text("Domain rule for api.example.com removed")
    expect(page.locator("#domain-rule-count")).to_have_text("0 domains enabled")
    expect(page.locator("#domain-rule-count")).to_have_class("status disabled")
    expect(page.locator("#domain-rules")).to_contain_text("No custom domains configured")

    # The credential card: no Clear button until something is configured,
    # then the configured type is stated and Clear appears next to it.
    expect(page.locator("#github-credential-status")).to_contain_text("No credential configured")
    expect(page.locator("#github-credential-form-label")).to_have_text("Set a new credential")
    expect(page.locator("#github-credential-clear")).to_be_hidden()
    page.locator("#github-token").fill("github_pat_mock")
    page.get_by_role("button", name="Set credential").click()
    expect(page.locator("#policy-message")).to_contain_text("GitHub credential stored")
    expect(page.locator("#github-credential-status")).to_contain_text("Configured: fine-grained token (PAT)")
    expect(page.locator("#github-credential-form-label")).to_have_text("Replace credential")
    # PAT mode surfaces the validation status just like app mode does.
    expect(page.locator("#github-credential-status")).to_contain_text("validation: not_checked")
    # Per-repository audits render next to each repository once a credential
    # is stored, and the re-check action refreshes them.
    expect(page.locator("#github-repos")).to_contain_text("infiloop2/trustyclaw")
    expect(page.locator("#github-repos")).to_contain_text("GitHub Actions workflows")
    page.get_by_role("button", name="Re-check repository audits").click()
    expect(page.locator("#policy-message")).to_contain_text("Repository audits refreshed")
    page.get_by_role("button", name="Clear credential").click()
    expect(page.locator("#github-credential-status")).to_contain_text("No credential configured")
    expect(page.locator("#github-credential-form-label")).to_have_text("Set a new credential")
    expect(page.locator("#github-credential-clear")).to_be_hidden()

    page.locator("#github-credential-mode").select_option("app")
    expect(page.locator("#github-app-fields")).to_be_visible()
    page.locator("#github-app-id").fill("12345")
    page.locator("#github-app-installation-id").fill("67890")
    page.locator("#github-app-private-key").fill("-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----")
    page.get_by_role("button", name="Set credential").click()
    expect(page.locator("#policy-message")).to_contain_text("GitHub credential stored")
    expect(page.locator("#github-credential-status")).to_contain_text("Configured: GitHub App 12345, installation 67890")
    expect(page.locator("#github-credential-clear")).to_be_visible()
    page.get_by_role("button", name="Clear credential").click()
    expect(page.locator("#github-credential-status")).to_contain_text("No credential configured")

    page.get_by_role("button", name="Home").click()
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page.get_by_role("button", name="Start Codex login")).to_be_visible()
    expect(page.get_by_role("button", name="Start Codex login")).to_be_enabled()
    expect(page.get_by_role("button", name="Start Claude login")).to_be_visible()
    expect(page.get_by_role("button", name="Start Claude login")).to_be_enabled()
    page.get_by_role("button", name="Start Codex login").click()
    expect(page.locator("#oauth")).to_contain_text("MOCK-CODEX")
    # The dashboard refreshes on its own 5-second poll; wait for the next one
    # rather than reaching into module internals (there is no test-only hook).
    with page.expect_response(
        lambda response: "/v1/agent-runtime/account" in response.url and response.request.method == "GET",
        timeout=8000,
    ):
        pass
    expect(page.get_by_role("button", name="Start Codex login")).to_have_count(0)
    expect(page.locator("#provider-accounts")).to_contain_text("acct_mock_openai")
    expect(page.locator("#provider-accounts")).to_contain_text("akshay@infiloop.io")
    expect(page.locator("#provider-accounts")).to_contain_text("pro")
    expect(page.locator("#provider-accounts")).to_contain_text("5 hour: 60%")
    expect(page.locator("#provider-accounts")).to_contain_text("weekly: 20%")
    expect(page.locator("#provider-accounts")).to_contain_text("credits: none")
    expect(page.locator("#provider-accounts")).to_contain_text("checked")

    # The linked account surfaces in the provider's network-controls row.
    page.get_by_role("button", name="Internet Access and Tools").click()
    expect(page.locator(".integration-row", has_text="OpenAI")).to_contain_text("Linked account")
    expect(page.locator(".integration-row", has_text="OpenAI")).to_contain_text("akshay@infiloop.io")
    page.get_by_role("button", name="Home").click()

    with page.expect_response(lambda response: "/v1/agent-processes" in response.url):
        page.get_by_role("button", name="Agent processes").click()
    expect(page.locator("#panel-processes")).to_be_visible()
    expect(page.locator("#processes")).to_contain_text("codex")
    expect(page.locator("#processes")).to_contain_text("app-server")
    expect(page.locator("#processes")).not_to_contain_text("scope")
    expect(page.locator("#processes")).not_to_contain_text("run-mock-")
    page.get_by_role("button", name="Home").click()

    expect(page.get_by_role("button", name="Start Claude login")).to_be_visible()
    expect(page.get_by_role("button", name="Start Claude login")).to_be_enabled()
    page.get_by_role("button", name="Start Claude login").click()
    expect(page.locator("#oauth")).to_contain_text("Claude Code login")
    page.once("dialog", lambda dialog: dialog.accept("mock-code"))
    page.get_by_role("button", name="Submit code").click()
    with page.expect_response(
        lambda response: "/v1/agent-runtime/account" in response.url and response.request.method == "GET",
        timeout=8000,
    ):
        pass
    expect(page.get_by_role("button", name="Start Claude login")).to_have_count(0)
    expect(page.locator("#provider-accounts")).to_contain_text("acct_mock_claude")
    expect(page.locator("#provider-accounts")).to_contain_text("claude@example.invalid")
    expect(page.locator("#provider-accounts")).to_contain_text("max")
    expect(page.locator("#provider-accounts")).to_contain_text("current session: 14%")
    expect(page.locator("#provider-accounts")).to_contain_text("weekly: 31%")
    expect(page.locator("#provider-accounts")).to_contain_text("resets Jul 7, 3:59pm (UTC)")
    expect(page.locator("#provider-accounts")).to_contain_text("checked")
    with page.expect_response(lambda response: "/v1/agent-runtime/refresh" in response.url):
        page.get_by_label("Refresh provider usage").click()
    expect(page.locator("#provider-accounts")).to_contain_text("current session: 14%")
    expect(page.locator("#provider-accounts")).to_contain_text("checked")


def mobile_smoke(page, url: str) -> None:
    """iPhone-sized pass: layout must not overflow and core flows must work."""
    from playwright.sync_api import expect

    log_in(page, url)
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page.locator("#health")).to_contain_text("ok")
    assert_no_horizontal_overflow(page, "home")

    page.get_by_role("button", name="Agent session log", exact=True).click()
    expect(page.locator("#panel-agent")).to_be_visible()
    expect(page.locator("#threads")).to_contain_text("website-redesign")
    page.locator("#threads .thread-item", has_text="website-redesign").click()
    expect(page.locator("#thread-detail")).to_contain_text("denied by policy")
    assert_no_horizontal_overflow(page, "agent")

    page.get_by_role("button", name="Agent audit log").click()
    expect(page.locator("#events")).to_contain_text("task.created")
    assert_no_horizontal_overflow(page, "agent event log")

    page.get_by_role("button", name="Agent processes").click()
    expect(page.locator("#panel-processes")).to_be_visible()
    assert_no_horizontal_overflow(page, "agent processes")

    page.get_by_role("button", name="Agent workspace", exact=True).click()
    expect(page.locator("#file-list")).to_contain_text("workspace")
    assert_no_horizontal_overflow(page, "agent workspace")

    page.get_by_role("button", name="Internet Access and Tools").click()
    expect(page.locator("#panel-network")).to_be_visible()
    assert_no_horizontal_overflow(page, "internet access")

    page.get_by_role("button", name="Network audit log").click()
    expect(page.locator("#net-events")).to_contain_text("deploy.acme.dev")
    expect(page.locator("#net-events")).to_contain_text("Host is not in the allowed network policy")
    expect(page.locator("#net-event-pager")).to_contain_text("1")
    expect(page.locator("#net-event-pager")).to_contain_text("Next")
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
