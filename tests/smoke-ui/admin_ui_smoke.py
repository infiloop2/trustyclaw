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
                desktop.grant_permissions(["clipboard-read", "clipboard-write"], origin=url.rstrip("/"))
                desktop_page = desktop.new_page()
                stale_password_smoke(desktop_page, url)
                desktop_smoke(desktop_page, url)
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


def stale_password_smoke(page, url: str) -> None:
    from playwright.sync_api import expect

    page.context.add_cookies(
        [{"name": "trustyclaw_admin", "value": "stale", "url": url}]
    )
    page.goto(url)
    expect(page.locator("#login")).to_be_visible()
    expect(page.locator("#notice")).to_be_hidden()
    expect(page.locator("#notice")).not_to_contain_text("unauthorized")
    page.evaluate(
        "() => import('/admin_ui/helpers.js').then(({ notice }) => notice('Another error', 'error'))"
    )
    expect(page.locator("#notice")).to_have_text("Another error")
    expect(page.locator("#notice")).to_be_visible()
    page.evaluate(
        "() => import('/admin_ui/helpers.js').then(({ notice }) => notice('', ''))"
    )
    page.context.clear_cookies()


def assert_only_guide_content_scrolls(page, fixed_selector: str) -> None:
    heading = page.locator(".connection-guide-heading")
    fixed_control = page.locator(fixed_selector)
    content = page.locator("#connection-guide-content")
    heading_before = heading.bounding_box()
    control_before = fixed_control.bounding_box()
    content_box = content.bounding_box()
    if not heading_before or not control_before or not content_box:
        raise AssertionError("integration guide layout is missing a visible fixed region")
    viewport_height = page.evaluate("window.innerHeight")
    if heading_before["y"] < 0 or content_box["y"] + content_box["height"] > viewport_height + 1:
        raise AssertionError("integration guide layout extends outside the viewport")
    page_scroll_before = page.evaluate("window.scrollY")
    scroll_top, scroll_height, client_height = content.evaluate(
        "element => { element.scrollTop = element.scrollHeight; return [element.scrollTop, element.scrollHeight, element.clientHeight]; }"
    )
    if scroll_height <= client_height or scroll_top <= 0:
        raise AssertionError("selected integration guide does not have its own working scroll pane")
    heading_after = heading.bounding_box()
    control_after = fixed_control.bounding_box()
    if not heading_after or not control_after:
        raise AssertionError("integration guide fixed region disappeared after scrolling")
    if abs(heading_after["y"] - heading_before["y"]) > 1 or abs(control_after["y"] - control_before["y"]) > 1:
        raise AssertionError("integration guide heading or selector moved with the article")
    if page.evaluate("window.scrollY") != page_scroll_before:
        raise AssertionError("scrolling an integration guide moved the page")


def desktop_smoke(page, url: str) -> None:
    from playwright.sync_api import expect

    log_in(page, url)
    page.evaluate(
        "() => import('/admin_ui/helpers.js').then(({ notice }) => notice('unauthorized', 'error'))"
    )
    expect(page.locator("#notice")).to_have_text("unauthorized")
    expect(page.locator("#notice")).to_be_visible()
    page.evaluate(
        "() => import('/admin_ui/helpers.js').then(({ notice }) => notice('', ''))"
    )
    expect(page.locator("body")).to_contain_text("trustyclaw-mock")
    expect(page.locator("#agent-name")).to_have_text("Host: trustyclaw-mock")
    expect(page.locator("#mobile-nav-toggle")).to_be_hidden()
    expect(page.locator("#upgrade-notice")).to_be_visible()
    expect(page.locator("#upgrade-notice")).to_have_attribute(
        "aria-label", "Upgrade available: version 99.0.0. Use your operator plane to upgrade."
    )
    expect(page.locator("#upgrade-popover")).to_be_hidden()
    page.locator("#upgrade-notice").hover()
    expect(page.locator("#upgrade-popover")).to_be_visible()
    expect(page.locator("#upgrade-popover")).to_contain_text("Upgrade available: version 99.0.0")
    expect(page.locator("#upgrade-popover")).to_contain_text("Use your operator plane to upgrade.")

    page.locator("#upgrade-notice").click()
    expect(page.locator("#upgrade-notice")).to_have_attribute(
        "aria-label", "Your TrustyClaw is at the latest version."
    )
    expect(page.locator("#upgrade-notice")).to_be_visible()
    expect(page.locator("#upgrade-notice")).to_have_class(re.compile(r"upgrade-current"))
    expect(page.locator("#upgrade-notice .upgrade-check")).to_be_visible()
    expect(page.locator("#upgrade-notice .upgrade-arrow")).to_be_hidden()
    page.locator("#upgrade-notice").hover()
    expect(page.locator("#upgrade-popover")).to_have_text("Your TrustyClaw is at the latest version.")
    page.locator("#upgrade-notice").click()
    expect(page.locator("#upgrade-notice")).to_have_attribute(
        "aria-label", "Upgrade available: version 99.0.0. Use your operator plane to upgrade."
    )
    expect(page.locator("#panel-home")).to_be_visible()
    home_sidebar_box = page.locator("#sidebar").bounding_box()
    if not home_sidebar_box:
        raise AssertionError("desktop sidebar is not visible on Home")
    expect(page.locator("#health")).to_contain_text("ok")
    expect(page.locator("#health")).to_contain_text(f"runtime {VERSION}")
    expect(page.locator("#health")).to_contain_text(f"state {VERSION}")
    expect(page.locator("#health")).to_contain_text("Memory")
    expect(page.locator("#health")).to_contain_text("Admin volume")
    expect(page.locator("#health")).to_contain_text("Agent volume")
    expect(page.locator("#panel-home").get_by_role("button", name="Reboot host")).to_be_visible()
    # Agent Chat is hardwired as the hero app: home opens with its
    # "Begin chat" navigator, and its nav entry sits directly below Home,
    # outside the Apps section.
    expect(page.locator("#home-hero .home-hero-card")).to_be_visible()
    expect(page.locator("#home-hero")).to_contain_text("Agent Chat")
    hero_tab = page.locator("#hero-app-tab").get_by_role("button", name="Agent Chat", exact=True)
    expect(hero_tab).to_be_visible()
    hero_box = hero_tab.bounding_box()
    home_tab_box = page.locator("#tab-home").bounding_box()
    network_tab_box = page.locator("#tab-network").bounding_box()
    if (
        not hero_box
        or not home_tab_box
        or not network_tab_box
        or not home_tab_box["y"] < hero_box["y"] < network_tab_box["y"]
    ):
        raise AssertionError("the hero app entry must sit between Home and the other nav tabs")
    expect(page.locator("#app-tabs").get_by_role("button", name="Agent Chat", exact=True)).to_have_count(0)
    expect(page.locator("#app-tabs").get_by_role("button", name="Mission Pursuit", exact=True)).to_be_visible()
    # App frames load only when selected. Eagerly navigating every hidden app
    # at login can leave deferred frames at about:blank on a small fresh host.
    expect(page.locator("iframe.app-frame[src]")).to_have_count(0)
    # Below the hero, the host tabs are grouped: Configuration, then Audit,
    # then Apps (Beta). The beta explanation is nested in the last heading.
    headings = page.locator("#sidebar .sidebar-section-title:visible")
    expect(headings).to_have_count(3)
    expect(headings.nth(0)).to_have_text("Configuration")
    expect(headings.nth(1)).to_have_text("Audit")
    expect(headings.nth(2).locator(":scope > span").first).to_have_text("Apps (Beta)")
    expect(page.locator("#sidebar-configuration .tab-button")).to_have_count(2)
    expect(page.locator("#sidebar-configuration #tab-network")).to_be_visible()
    expect(page.locator("#sidebar-audit .tab-button")).to_have_count(6)
    expect(page.locator("#sidebar-audit #tab-processes")).to_be_visible()
    expect(page.locator("#sidebar-audit #tab-tool-log")).to_be_visible()
    page.locator("#home-hero").get_by_role("button", name="Begin chat", exact=True).click()
    expect(page.locator("#panel-app-agent_chat")).to_be_visible()
    expect(page.locator('iframe[title="Agent Chat"]')).to_have_attribute(
        "src", "/v1/apps/agent_chat/ui/index.html"
    )
    expect(page.locator('iframe[title="Mission Pursuit"][src]')).to_have_count(0)
    expect(page.locator("#panel-home")).to_be_hidden()
    page.get_by_role("button", name="Home", exact=True).click()
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page.locator("#runtime-overview")).to_contain_text("Codex")
    expect(page.locator("#runtime-overview")).to_contain_text("Claude Code")
    expect(page.locator("#runtime-overview")).to_contain_text("Pi")
    expect(page.locator("#runtime-overview")).to_contain_text("Hermes")
    expect(page.locator("#runtime-overview")).to_contain_text("deactivated")
    expect(page.locator("#runtime-overview").get_by_label("Refresh provider status and usage")).to_be_visible()
    expect(page.locator(".topbar-actions").get_by_label("Refresh provider status and usage")).to_have_count(0)
    # Before any login there is no usage: all four rings (5h and weekly for
    # Codex and Claude Code) render the unavailable "--" form rather than 0%.
    # Bedrock billing is reconciliation metadata in the provider details, not
    # a primary toolbar value.
    expect(page.locator("#runtime-overview .usage-ring.unavailable")).to_have_count(4)
    expect(page.locator("#runtime-overview .runtime-summary-bedrock")).to_have_count(2)
    expect(page.locator("#runtime-overview")).to_contain_text("--")
    expect(page.locator("#panel-home").get_by_text("Agent runtimes")).to_have_count(0)
    expect(page.locator("#panel-home").get_by_text("Provider usage")).to_have_count(0)
    expect(page.get_by_role("button", name="Start Codex login")).to_have_count(0)
    expect(page.get_by_role("button", name="Start Claude login")).to_have_count(0)
    page.locator("#runtime-overview .runtime-summary", has_text="Codex").click()
    expect(page.locator("#panel-network")).to_be_visible()
    disabled_openai_row = page.locator(".integration-row[data-integration]", has_text="OpenAI")
    expect(disabled_openai_row.locator(".integration-details")).to_be_visible()
    title_box = disabled_openai_row.locator(".integration-title").bounding_box()
    account_card_box = disabled_openai_row.locator(".integration-details .detail-card").bounding_box()
    if not title_box or not account_card_box or abs(title_box["x"] - account_card_box["x"]) > 2:
        raise AssertionError("expanded integration content is not aligned with the row title after the chevron")
    expect(disabled_openai_row).not_to_contain_text("No account linked yet")
    expect(disabled_openai_row).not_to_contain_text("deactivated")
    page.get_by_role("button", name="Home").click()

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
    expect(page.locator("#net-events")).to_contain_text("Host not allowed")
    expect(page.locator("#net-page-summary")).to_contain_text("Page 1")
    expect(page.locator("#net-event-pager")).to_contain_text("Next")
    expect(page.locator("#net-events")).to_contain_text("https://api.github.com:443/repos/acme/infra/actions/runs?status=failure")
    page.get_by_role("button", name="Show denied").click()
    expect(page.get_by_role("button", name="Show all")).to_be_visible()
    expect(page.locator("#net-page-summary")).to_contain_text("denied only")
    expect(page.locator("#net-events")).to_contain_text("Host not allowed")
    expect(page.locator("#net-events")).not_to_contain_text("api.openai.com")

    network_tab = page.locator("#tab-network")
    network_label = network_tab.locator("span")
    label_metrics_script = """element => {
      const style = getComputedStyle(element);
      const text = element.firstChild;
      const characterLines = [];
      if (text instanceof Text) {
        for (let index = 0; index < text.length; index += 1) {
          const range = document.createRange();
          range.setStart(text, index);
          range.setEnd(text, index + 1);
          characterLines.push(Math.round(range.getBoundingClientRect().top * 100) / 100);
        }
      }
      const bounds = element.getBoundingClientRect();
      return {
        fontFamily: style.fontFamily,
        fontSize: style.fontSize,
        fontWeight: style.fontWeight,
        letterSpacing: style.letterSpacing,
        lineHeight: style.lineHeight,
        width: Math.round(bounds.width * 100) / 100,
        height: Math.round(bounds.height * 100) / 100,
        characterLines,
      };
    }"""
    inactive_label_metrics = network_label.evaluate(label_metrics_script)
    network_tab.click()
    expect(page.locator("#panel-network")).to_be_visible()
    active_label_metrics = network_label.evaluate(label_metrics_script)
    if active_label_metrics != inactive_label_metrics:
        raise AssertionError(
            "selecting Internet Access and Tools changed its label typography or wrapping: "
            f"inactive={inactive_label_metrics}, active={active_label_metrics}"
        )
    network_sidebar_box = page.locator("#sidebar").bounding_box()
    if not network_sidebar_box or abs(network_sidebar_box["y"] - home_sidebar_box["y"]) > 1:
        raise AssertionError("opening Internet Access and Tools shifted the desktop sidebar")
    expect(page.locator("#panel-network")).not_to_contain_text("Managed integrations")
    expect(page.locator("#panel-network")).not_to_contain_text("Curated access bundles")
    # Every integration renders as its own card row with a compact header,
    # status, and separate enable/disable buttons.
    # Scope to network-integration rows ([data-integration]); bundled tool rows
    # are .integration-row too.
    for integration_id in ("openai", "claude", "bedrock", "github", "python_packages", "npm_packages"):
        row = page.locator(f".integration-row[data-integration='{integration_id}']")
        expect(row).to_contain_text("disabled")
        expect(row.get_by_role("button", name="Enable", exact=True)).to_be_enabled()
        expect(row.get_by_role("button", name="Disable", exact=True)).to_be_disabled()
        expect(row.locator(".icon-tile")).to_have_count(0)
    expect(page.locator(".integration-row[data-integration]", has_text="OpenAI")).not_to_contain_text("deactivated")
    expect(page.locator(".integration-row[data-integration]", has_text="Claude")).not_to_contain_text("deactivated")
    openai_row = page.locator(".integration-row[data-integration]", has_text="OpenAI")
    claude_row = page.locator(".integration-row[data-integration]", has_text="Claude")
    expect(openai_row.locator(".integration-subtitle")).to_have_text("Connect your OpenAI subscription and let your agent use Codex for tasks and cached web search.")
    expect(claude_row.locator(".integration-subtitle")).to_have_text("Connect your Anthropic subscription and let your agent use Claude Code for tasks. Web search is optional and off by default.")
    expect(claude_row.locator(".integration-subtitle")).to_have_css("white-space", "normal")
    expect(page.locator(".integration-row[data-integration]", has_text="Python packages")).to_contain_text("discover and install public Python packages")
    expect(page.locator(".integration-row[data-integration]", has_text="NPM Packages")).to_contain_text("discover and install public JavaScript packages")
    expect(page.locator("#panel-network > .network-group > .network-group-heading")).to_have_text(
        ["AI Inference", "Tools", "Manual"]
    )
    expect(page.locator("#ai-inference-integrations .integration-row h2")).to_have_text(
        ["OpenAI", "Claude", "AWS Bedrock"]
    )
    bedrock_row = page.locator(".integration-row[data-integration='bedrock']")
    expect(bedrock_row.locator(".integration-subtitle")).to_have_text(
        "Connect your AWS account once and let your agent run Pi and Hermes tasks through Bedrock in your own account."
    )
    tool_labels = page.locator("#tools > .integration-row h2").all_text_contents()
    assert tool_labels == sorted(tool_labels, key=str.casefold)
    subtitles = page.locator("#ai-inference-integrations .integration-subtitle, #tools > .integration-row .integration-subtitle").all_text_contents()
    assert subtitles
    assert all(text.startswith(("Connect ", "Enable ", "Lets ")) for text in subtitles)
    expect(page.locator(".network-group-manual .custom-domain-card")).to_contain_text("Creates an explicit network rule")
    github_row = page.locator(".integration-row[data-integration]", has_text="GitHub")
    expect(github_row.locator(".preset-with-info h2")).to_have_text("GitHub")
    expect(github_row.locator(".preset-with-info h2")).not_to_contain_text("all reads")
    expect(github_row).to_contain_text("Connect GitHub and let your agent read repositories")
    page.get_by_label("OpenAI overview and protections").click()
    expect(page.locator("#preset-info-popover")).not_to_contain_text("Connect your OpenAI subscription")
    expect(page.locator("#preset-info-popover")).to_contain_text("another account is denied")
    expect(page.locator("#preset-info-popover")).to_contain_text("Live browsing and remote tool servers are blocked")
    expect(page.locator("#preset-info-popover")).not_to_contain_text("Improve the model for everyone")
    expect(page.locator("#preset-info-popover")).to_contain_text("View integration guide")
    page.get_by_label("Claude overview and protections").click()
    expect(page.locator("#preset-info-popover")).not_to_contain_text("Connect your Anthropic subscription")
    expect(page.locator("#preset-info-popover")).to_contain_text("Web search is off by default")
    expect(page.locator("#preset-info-popover")).not_to_contain_text("Help Improve Claude")
    expect(page.locator("#preset-info-popover")).not_to_contain_text("does not open arbitrary remote tool servers")
    page.get_by_label("GitHub overview and protections").click()
    expect(page.locator("#preset-info-popover")).not_to_contain_text("Connect GitHub and let your agent")
    expect(page.locator("#preset-info-popover")).to_contain_text("writes work only for the repositories you configure")
    expect(page.locator("#preset-info-popover")).not_to_contain_text("listed by the operator")
    expect(page.locator("#preset-info-popover")).to_contain_text("GraphQL")
    expect(page.locator("#preset-info-popover")).to_contain_text("LFS uploads")
    expect(page.locator("#preset-info-popover")).to_contain_text("GitHub Actions run arbitrary code")
    expect(page.locator("#preset-info-popover")).to_contain_text("network access and repository credentials")
    expect(page.locator("#preset-info-popover code")).to_have_text(".github")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    sidebar_box = page.locator("#sidebar").bounding_box()
    if not sidebar_box:
        raise AssertionError("desktop sidebar is not visible before opening Integration Guides")
    page.get_by_role("button", name="View integration guide").click()
    expect(page.locator("#panel-connection-guide")).to_be_visible()
    guide_sidebar_box = page.locator("#sidebar").bounding_box()
    if not guide_sidebar_box or abs(guide_sidebar_box["y"] - sidebar_box["y"]) > 1:
        raise AssertionError("opening Integration Guides shifted the desktop sidebar")
    if page.evaluate("window.scrollY") != 0:
        raise AssertionError("opening an integration guide did not reset the previous tab's page position")
    expect(page.locator(".connection-guide-entry")).to_have_count(1)
    expect(page.locator("[data-guide-section='github']")).to_contain_text("Exact network boundary")
    expect(page.locator("[data-guide-section='github'] .guide-kind")).to_have_text("Direct network integration")
    expect(page.locator("[data-guide-section='github'] .guide-network-scope")).to_be_visible()
    expect(page.locator("[data-guide-section='github'] details.guide-network-scope")).to_have_count(0)
    expect(page.locator(".guide-network-scope .table-scroll")).to_have_count(0)
    github_guide = page.locator("[data-guide-section='github']")
    expect(github_guide.locator(".guide-capabilities")).not_to_contain_text(".github push approvals")
    expect(github_guide.locator(".guide-data-section")).to_contain_text("exposed to the entire internet")
    expect(github_guide.locator(".guide-data-section")).to_contain_text("holds .github pushes for your approval")
    expect(github_guide.locator(".guide-technical-details")).to_contain_text(
        "Disabling GitHub clears the write-repository list"
    )
    expect(github_guide.locator(".guide-technical-details")).not_to_contain_text(
        "GitHub Actions run arbitrary code"
    )
    page.locator("#connection-guide-index").get_by_role("button", name="OpenAI", exact=True).click()
    expect(page.locator(".connection-guide-entry")).to_have_count(1)
    expect(page.locator("[data-guide-section='openai']")).to_contain_text("What happens to your data")
    openai_guide = page.locator("[data-guide-section='openai']")
    openai_section_headings = openai_guide.locator("h3").all_text_contents()
    if openai_section_headings.index("What it enables") > openai_section_headings.index("Connection"):
        raise AssertionError("integration capabilities should appear before connection instructions")
    expect(openai_guide.get_by_role("heading", name="Connection", exact=True)).to_have_count(1)
    expect(openai_guide.locator(":scope > .guide-section").nth(1).locator(":scope > p")).to_have_count(0)
    expect(openai_guide).not_to_contain_text("Connection steps")
    expect(openai_guide).to_contain_text("enter the displayed device code to complete sign-in")
    expect(openai_guide).to_contain_text("any host data available to Codex can go to OpenAI")
    expect(openai_guide).to_contain_text("Cached web search keeps the search query and surrounding context within OpenAI")
    expect(openai_guide).to_contain_text("What OpenAI can do with it")
    expect(openai_guide.locator(".guide-policy-point span", has_text="Before connecting")).to_have_count(1)
    expect(openai_guide).to_contain_text("OpenAI may use new conversations")
    expect(openai_guide).to_contain_text("OpenAI says new conversations are not used for model training")
    expect(openai_guide).to_contain_text("abuse or security investigations")
    expect(openai_guide).to_contain_text("How long OpenAI retains it")
    expect(openai_guide).to_contain_text("permanent deletion within 30 days")
    expect(openai_guide).not_to_contain_text("Personal subscription data settings")
    expect(openai_guide).not_to_contain_text("Third-party policies and promises")
    expect(openai_guide).not_to_contain_text("OpenAI login")
    expect(openai_guide.locator(".guide-data-flow")).to_have_count(0)
    expect(openai_guide.get_by_role("link", name="OpenAI Data Controls instructions")).to_have_attribute("href", "https://help.openai.com/en/articles/7730893-chatgpt-data-usage-for-model-training")
    expect(openai_guide.get_by_role("link", name="OpenAI consumer data usage FAQ")).to_have_attribute("href", "https://help.openai.com/en/articles/7039943")
    expect(openai_guide.get_by_role("link", name="OpenAI Codex retention and deletion")).to_have_attribute("href", "https://help.openai.com/en/articles/20001333-how-to-archive-and-delete-codex-chats-in-the-chatgpt-app")
    expect(openai_guide.get_by_role("link", name="OpenAI Privacy Policy")).to_have_attribute("href", "https://openai.com/policies/privacy-policy/")
    expect(openai_guide.locator(":scope > .guide-section > h3")).to_have_text([
        "What it enables",
        "Connection",
        "What happens to your data",
        "Technical notes",
    ])
    expect(openai_guide.get_by_role("heading", name="Protections and sensitive controls", exact=True)).to_have_count(0)
    expect(openai_guide.locator(".guide-protections li").first).to_be_visible()
    expect(openai_guide.get_by_role("heading", name="Exact network boundary", exact=True)).to_have_count(1)
    page.locator("#connection-guide-index").get_by_role("button", name="Claude", exact=True).click()
    claude_guide = page.locator("[data-guide-section='claude']")
    expect(claude_guide).to_contain_text("paste the authorization result when prompted")
    expect(claude_guide).to_contain_text("any host data available to Claude Code can go to Anthropic")
    expect(claude_guide).to_contain_text("Web search (optional, off by default)")
    expect(claude_guide).to_contain_text("Anthropic runs the search server-side")
    expect(claude_guide).to_contain_text("Anthropic's search partners")
    expect(claude_guide).to_contain_text("Anthropic may use new personal chats")
    expect(claude_guide).to_contain_text("past and new chats")
    expect(claude_guide).to_contain_text("improve Anthropic's safeguards")
    expect(claude_guide).to_contain_text("anonymized or de-identified data may be kept longer")
    expect(claude_guide).to_contain_text("Turn off Help Improve Claude")
    expect(claude_guide).not_to_contain_text("Third-party policies and promises")
    expect(claude_guide).to_contain_text("How long Anthropic retains it")
    expect(claude_guide).to_contain_text("up to 2 years")
    expect(claude_guide).to_contain_text("up to 7 years")
    expect(claude_guide).to_contain_text("Feedback may be kept for 5 years")
    expect(claude_guide.locator(".guide-data-flow")).to_have_count(0)
    expect(claude_guide.get_by_role("link", name="Anthropic Covered Models retention").first).to_have_attribute("href", "https://support.claude.com/en/articles/15425695-covered-models")
    expect(claude_guide.get_by_role("link", name="Anthropic consumer training policy").first).to_have_attribute("href", "https://privacy.claude.com/en/articles/10023580-is-my-data-used-for-model-training")
    expect(page.locator("#connection-guide-select")).to_be_hidden()
    assert_only_guide_content_scrolls(page, "#connection-guide-index")
    page.locator("#connection-guide-index").get_by_role("button", name="Gmail", exact=True).click()
    if page.locator("#connection-guide-content").evaluate("element => element.scrollTop") != 0:
        raise AssertionError("switching integration guides did not start the new article at the top")
    gmail_guide = page.locator("[data-guide-section='tool:gmail']")
    expect(gmail_guide.locator(".guide-kind")).to_have_text("Bundled MCP tool")
    expect(gmail_guide.locator(":scope > .guide-section > h3")).to_have_text([
        "What it enables",
        "Connection",
        "What happens to your data",
    ])
    expect(gmail_guide.locator(".guide-technical-details")).to_have_count(0)
    expect(gmail_guide.locator(":scope > .guide-section").nth(1).locator(":scope > p")).to_have_count(0)
    expect(gmail_guide).to_contain_text("send_email")
    expect(gmail_guide).to_contain_text("approval required")
    expect(gmail_guide).to_contain_text("GOOGLE_OAUTH_CLIENT_ID")
    expect(gmail_guide).to_contain_text(f"{url.rstrip('/')}/oauth/callback")
    # The callback URI and config keys render inside the setup step that needs them.
    callback = gmail_guide.locator(".guide-steps .guide-callback")
    expect(callback).to_have_count(1)
    copy_button = callback.get_by_role("button", name="Copy callback URI")
    expect(copy_button).to_be_visible()
    copy_button.click()
    copy_feedback = callback.locator("[data-callback-copy-feedback]")
    expect(copy_feedback).to_have_text("Copied")
    expect(copy_feedback).to_be_visible()
    copied_callback = page.evaluate("navigator.clipboard.readText()")
    assert copied_callback == f"{url.rstrip('/')}/oauth/callback"
    gmail_guide.get_by_role("heading", name="Gmail", exact=True).click()
    expect(copy_feedback).to_be_hidden()
    expect(gmail_guide.locator(".guide-steps .guide-config")).to_have_count(1)
    expect(gmail_guide).to_contain_text("Gmail search query")
    expect(gmail_guide).to_contain_text("What Google can do with it")
    expect(gmail_guide).to_contain_text("How long Google retains it")
    expect(gmail_guide.locator(".guide-data-summary article")).to_have_count(4)
    expect(gmail_guide.locator(".guide-data-flow")).to_have_count(0)
    expect(gmail_guide).to_contain_text("Google Privacy Policy")
    consent_image = gmail_guide.locator("img[alt*='App name']")
    expect(consent_image).to_have_count(1)
    consent_image.scroll_into_view_if_needed()
    expect(consent_image).to_have_js_property("complete", True)
    if consent_image.evaluate("image => image.naturalWidth") <= 0:
        raise AssertionError("the Google OAuth guide screenshot did not load")
    page.locator("#connection-guide-index").get_by_role("button", name="Custom Domain Access", exact=True).click()
    custom_guide = page.locator("[data-guide-section='custom_domain']")
    expect(custom_guide).to_contain_text("complete HTTPS request")
    expect(custom_guide).to_contain_text("What the third party can do with it")
    expect(custom_guide).to_contain_text("How long the third party retains it")
    expect(custom_guide.locator(".guide-data-summary article")).to_have_count(4)
    for guide_label in (
        "OpenAI",
        "Claude",
        "GitHub",
        "Python packages",
        "NPM Packages",
        "Brave Search",
        "Gmail",
        "Google Calendar",
        "Custom Domain Access",
    ):
        page.locator("#connection-guide-index").get_by_role("button", name=guide_label, exact=True).click()
        current_guide = page.locator(".connection-guide-entry")
        expect(current_guide.get_by_role("heading", name="What happens to your data", exact=True)).to_have_count(1)
        expect(current_guide.locator(".guide-data-summary article")).to_have_count(4)
    guide_labels = page.locator("#connection-guide-index button").all_text_contents()
    assert guide_labels == sorted(guide_labels, key=str.casefold)
    npm_guide_button = page.locator("#connection-guide-index").get_by_role("button", name="NPM Packages", exact=True)
    npm_guide_button.click()
    expect(page.locator("[data-guide-section='npm_packages']")).not_to_contain_text("Review packages before use")
    expect(page.locator(".connection-guide-entry")).to_have_count(1)
    page.get_by_role("button", name="Internet Access and Tools").click()
    expect(page.locator("#panel-network")).to_be_visible()
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
    page.locator(".integration-row[data-integration]", has_text="OpenAI").get_by_role("button", name="Enable", exact=True).click()
    expect(page.locator("[data-integration-message='openai']")).to_contain_text("OpenAI enabled")
    expect(page.locator(".integration-row[data-integration]", has_text="OpenAI")).to_contain_text("enabled")
    page.locator(".integration-row[data-integration]", has_text="Claude").get_by_role("button", name="Enable", exact=True).click()
    expect(page.locator("[data-integration-message='claude']")).to_contain_text("Claude enabled")
    # Provider linked-account controls live inside each provider dropdown.
    openai_row = page.locator(".integration-row[data-integration]", has_text="OpenAI")
    openai_row.get_by_label("Toggle OpenAI details").click()
    page.locator(".integration-row[data-integration]", has_text="Claude").get_by_label("Toggle Claude details").click()
    expect(openai_row).to_contain_text("No account linked yet")
    expect(page.locator(".integration-row[data-integration]", has_text="Claude")).to_contain_text("No account linked yet")
    expect(openai_row.get_by_role("button", name="Disconnect")).to_have_count(0)
    # Bedrock is one provider row and one validated credential, region, account,
    # and billing record. Pi and Hermes remain separate runtime counters.
    bedrock_row = page.locator(".integration-row[data-integration='bedrock']")
    expect(bedrock_row).to_have_count(1)
    expect(page.locator(".integration-row[data-integration='pi']")).to_have_count(0)
    expect(page.locator(".integration-row[data-integration='hermes']")).to_have_count(0)
    bedrock_row.get_by_label("Toggle AWS Bedrock details").click()
    expect(bedrock_row).to_contain_text("No AWS credential stored yet")
    expect(bedrock_row).to_contain_text("required IAM policy")
    page.locator("#bedrock-access-key-id-bedrock").fill("AKIAMOCKOPERATOR0001")
    page.locator("#bedrock-secret-access-key-bedrock").fill("S" * 40)
    page.locator("#bedrock-region-bedrock").select_option("us-west-2")
    bedrock_row.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("[data-integration-message='bedrock']")).to_contain_text(
        "AWS credential accepted."
    )
    expect(page.locator("#bedrock-secret-access-key-bedrock")).to_have_value("")
    expect(bedrock_row).to_contain_text("AKIAMOCKOPERATOR0001")
    bedrock_row.get_by_role("button", name="Enable", exact=True).click()
    expect(page.locator("[data-integration-message='bedrock']")).to_contain_text(
        "AWS Bedrock enabled"
    )
    expect(bedrock_row).to_contain_text("enabled")
    expect(bedrock_row).to_contain_text("arn:aws:iam::123456789012:user/trustyclaw-bedrock")
    # Two separate live usage boxes, one per harness, each with its own
    # month-to-date estimate metered from Bedrock responses.
    expect(bedrock_row.locator(".bedrock-usage-box")).to_have_count(2)
    expect(bedrock_row.locator(".bedrock-usage-box", has_text="Pi")).to_contain_text("MTD est. $12.75")
    expect(bedrock_row.locator(".bedrock-usage-box", has_text="Pi")).to_contain_text("1.8M in")
    expect(bedrock_row.locator(".bedrock-usage-box", has_text="Pi")).to_contain_text("2 of 210 requests unmetered")
    expect(bedrock_row.locator(".bedrock-usage-box", has_text="Hermes")).to_contain_text("MTD est. $0.31")
    expect(bedrock_row).not_to_contain_text("Cost Explorer")
    expect(page.locator("#bedrock-region-bedrock")).to_have_value("us-west-2")
    # Pi and Hermes are separate per-runtime toolbar boxes, each with its own
    # live month-to-date estimate (labelled "MTD est." to flag it is an
    # estimate, not the authoritative AWS bill) and its own per-runtime status.
    pi_box = page.locator("#runtime-overview .runtime-summary", has_text="Pi")
    hermes_box = page.locator("#runtime-overview .runtime-summary", has_text="Hermes")
    expect(pi_box).to_contain_text("MTD est.")
    expect(pi_box).to_contain_text("$12.75")
    expect(pi_box).to_contain_text("1.8M")
    expect(hermes_box).to_contain_text("MTD est.")
    expect(hermes_box).to_contain_text("$0.31")
    expect(page.locator("#runtime-overview .bedrock-toolbar-lag")).to_have_count(0)
    expect(pi_box).to_contain_text("active")
    expect(pi_box.locator(".runtime-running-badge")).to_have_count(0)
    counter_task_response = page.request.post(
        f"{url.rstrip('/')}/v1/tasks",
        headers={"Authorization": f"Bearer {PASSWORD}"},
        data={
            "agent_runtime": "pi",
            "model": "deepseek.v3.2",
            "effort": "medium",
            "input_message": "Exercise the Pi toolbar running counter.",
            "thread_id": "toolbar-pi-counter",
        },
    )
    if not counter_task_response.ok:
        raise AssertionError(
            f"could not create Pi toolbar counter task: {counter_task_response.status} "
            f"{counter_task_response.text()}"
        )
    counter_task = counter_task_response.json()
    expect(pi_box).to_contain_text("1 running", timeout=8000)
    killed = page.request.post(
        f"{url.rstrip('/')}/v1/tasks/{counter_task['task_id']}/kill",
        headers={"Authorization": f"Bearer {PASSWORD}"},
    )
    if not killed.ok:
        raise AssertionError(f"could not stop Pi toolbar counter task: {killed.status} {killed.text()}")
    expect(pi_box.locator(".runtime-running-badge")).to_have_count(0, timeout=8000)
    expect(hermes_box).to_contain_text("active")
    expect(hermes_box.locator(".runtime-running-badge")).to_have_count(0)
    # Disconnect and reconnect operate on the one shared resource.
    page.once("dialog", lambda dialog: dialog.accept())
    bedrock_row.get_by_role("button", name="Disconnect AWS", exact=True).click()
    expect(page.locator("[data-integration-message='bedrock']")).to_contain_text(
        "Shared AWS Bedrock account disconnected"
    )
    expect(bedrock_row).to_contain_text("No AWS credential stored yet")
    expect(bedrock_row.locator(".provider-error")).to_have_count(0)
    expect(bedrock_row.get_by_role("button", name="Disconnect AWS", exact=True)).to_have_count(0)
    expect(pi_box).to_contain_text("awaiting login")
    expect(hermes_box).to_contain_text("awaiting login")
    page.locator("#bedrock-access-key-id-bedrock").fill("AKIAMOCKOPERATOR0003")
    page.locator("#bedrock-secret-access-key-bedrock").fill("S" * 40)
    page.locator("#bedrock-region-bedrock").select_option("us-east-2")
    bedrock_row.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("[data-integration-message='bedrock']")).to_contain_text(
        "AWS credential accepted."
    )
    expect(bedrock_row).to_contain_text("arn:aws:iam::123456789012:user/trustyclaw-bedrock")
    github_row.get_by_role("button", name="Enable", exact=True).click()
    github_message = github_row.locator("[data-integration-message='github']")
    expect(github_message).to_contain_text("GitHub enabled")
    expect(page.locator("#github-require-approval-status")).to_contain_text("held for approval")
    approval_enable = page.locator("[data-action='enable-github-require-approval']")
    approval_disable = page.locator("[data-action='disable-github-require-approval']")
    expect(approval_enable).to_be_disabled()
    expect(approval_enable).to_have_text("Enabled")
    expect(approval_disable).to_be_enabled()
    expect(approval_disable).to_have_text("Disable")
    # Enabling expands the row with the repository controls.
    expect(page.locator("#github-expansion")).to_be_visible()
    expect(page.locator("#github-repos")).to_contain_text("No write repositories configured")
    page.locator("#github-repo").fill("infiloop2/trustyclaw")
    page.get_by_role("button", name="Add write repository", exact=True).click()
    expect(github_message).to_contain_text("Write repository infiloop2/trustyclaw saved")
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
    # A rejected repository input reports an error-styled message and keeps
    # the confirmation styling for ordinary feedback.
    page.locator("#github-repo").fill("not a repo")
    page.get_by_role("button", name="Add write repository", exact=True).click()
    expect(github_message).to_have_class(re.compile(r"inline-message.*error"))
    expect(github_message).to_contain_text("owner/repo")
    # The .github approval gate is on from the first GitHub enable. The mock
    # simulates the agent pushing after its first write repository is added.
    expect(page.locator("#github-require-approval-status")).to_contain_text("held for approval")
    pending_push = page.locator("#github-pending-pushes .pending-push")
    expect(pending_push).to_contain_text("infiloop2/trustyclaw")
    expect(pending_push).to_contain_text(".github/workflows/deploy.yml")
    expect(pending_push).to_contain_text("pending")
    page.once("dialog", lambda dialog: dialog.accept())
    pending_push.get_by_role("button", name="Approve & push").click()
    expect(github_message).to_contain_text("approved and pushed")
    expect(page.locator("#github-pending-pushes")).to_have_text("")
    page.locator("[data-action='disable-github-require-approval']").click()
    expect(github_message).to_contain_text(".github push approval disabled")
    expect(approval_enable).to_be_enabled()
    expect(approval_enable).to_have_text("Enable")
    expect(approval_disable).to_be_disabled()
    expect(approval_disable).to_have_text("Disabled")
    # Disabling asks for confirmation and applies immediately.
    page.locator(".integration-row[data-integration]", has_text="NPM Packages").get_by_role("button", name="Enable", exact=True).click()
    expect(page.locator("[data-integration-message='npm_packages']")).to_contain_text("NPM Packages enabled")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator(".integration-row[data-integration]", has_text="NPM Packages").get_by_role("button", name="Disable", exact=True).click()
    expect(page.locator("[data-integration-message='npm_packages']")).to_contain_text("NPM Packages disabled")
    expect(page.locator(".integration-row[data-integration]", has_text="NPM Packages")).to_contain_text("disabled")

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
    domain_message = page.locator("[data-integration-message='custom_domain']")
    expect(domain_message).to_contain_text("Domain rule for api.example.com saved")
    expect(page.locator("#domain-rule-count")).to_have_text("1 domain enabled")
    expect(page.locator("#domain-rule-count")).to_have_class("status enabled")
    expect(page.locator("#domain-rules")).to_contain_text("api.example.com")
    expect(page.locator("#domain-rules")).to_contain_text("GET, HEAD")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#domain-rules").get_by_role("button", name="Remove", exact=True).click()
    expect(domain_message).to_contain_text("Domain rule for api.example.com removed")
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
    expect(github_message).to_contain_text("GitHub credential stored")
    expect(page.locator("#github-credential-status")).to_contain_text("Configured: fine-grained token (PAT)")
    expect(page.locator("#github-credential-form-label")).to_have_text("Replace credential")
    # PAT mode surfaces the validation status just like app mode does.
    expect(page.locator("#github-credential-status")).to_contain_text("validation: not_checked")
    # Per-repository audits render next to each repository once a credential
    # is stored, and the re-check action refreshes them.
    expect(page.locator("#github-repos")).to_contain_text("infiloop2/trustyclaw")
    expect(page.locator("#github-repos")).to_contain_text("GitHub Actions workflows")
    page.get_by_role("button", name="Re-check repository audits").click()
    expect(github_message).to_contain_text("Repository audits refreshed")
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
    expect(github_message).to_contain_text("GitHub credential stored")
    expect(page.locator("#github-credential-status")).to_contain_text("Configured: GitHub App 12345, installation 67890")
    expect(page.locator("#github-credential-clear")).to_be_visible()
    page.get_by_role("button", name="Clear credential").click()
    expect(page.locator("#github-credential-status")).to_contain_text("No credential configured")

    tools_smoke(page)

    # The tool OAuth callback reloads the page, so provider rows are collapsed
    # again; reopen each account card before exercising provider login.
    openai_row.get_by_label("Toggle OpenAI details").click()
    expect(openai_row.get_by_role("button", name="Start Codex login")).to_be_visible()
    expect(openai_row.get_by_role("button", name="Start Codex login")).to_be_enabled()
    claude_row = page.locator(".integration-row[data-integration]", has_text="Claude")
    claude_row.get_by_label("Toggle Claude details").click()
    expect(claude_row.get_by_role("button", name="Start Claude Code login")).to_be_visible()
    page.get_by_role("button", name="Start Codex login").click()
    expect(openai_row.locator(".provider-oauth")).to_contain_text("MOCK-CODEX")
    # The mock completes the device login out of band a couple of seconds
    # after it starts, like the real flow; the dashboard notices on its own
    # 5-second poll, so allow two poll rounds for the flip to render.
    with page.expect_response(
        lambda response: "/v1/agent-runtime/account" in response.url and response.request.method == "GET",
        timeout=8000,
    ):
        pass
    expect(openai_row.get_by_role("button", name="Start Codex login")).to_have_count(0, timeout=12000)
    expect(openai_row).to_contain_text("connected: akshay@infiloop.io")
    expect(openai_row).to_contain_text("Connected account")
    expect(openai_row.locator(".connection-summary")).to_be_visible()
    expect(openai_row.locator(".connection-summary b")).to_have_count(0)
    expect(openai_row.get_by_role("button", name="Disconnect")).to_be_visible()
    codex_summary = page.locator("#runtime-overview .runtime-summary", has_text="Codex")
    expect(codex_summary).to_contain_text("active")
    expect(codex_summary).to_contain_text("8%")
    expect(codex_summary).to_contain_text("84%")
    # A healthy 5h window (no threshold class) beside a near-full weekly window
    # (warning), so the mock exercises both ring states at once.
    expect(codex_summary.locator(".usage-ring").nth(0)).not_to_have_class(re.compile(r"usage-(warning|critical)"))
    expect(codex_summary.locator(".usage-ring").nth(1)).to_have_class(re.compile(r"usage-warning"))
    # The reset countdown shares the single window-label line, so a summary
    # with countdowns is exactly as tall as one without.
    expect(codex_summary.locator(".usage-window")).to_have_text(["5h · 40m", "wk · 6d"])
    # Every runtime box links to its provider's Internet Access and Tools
    # settings, in any state — active included.
    expect(codex_summary).to_have_attribute("data-action", "open-provider")
    expect(codex_summary).to_have_attribute("data-provider", "openai")

    with page.expect_response(lambda response: "/v1/agent-processes" in response.url):
        page.get_by_role("button", name="Agent processes").click()
    expect(page.locator("#panel-processes")).to_be_visible()
    expect(page.locator("#processes")).to_contain_text("codex")
    expect(page.locator("#processes")).to_contain_text("app-server")
    expect(page.locator("#processes")).not_to_contain_text("scope")
    expect(page.locator("#processes")).not_to_contain_text("run-mock-")
    page.get_by_role("button", name="Internet Access and Tools").click()

    expect(claude_row.get_by_role("button", name="Start Claude Code login")).to_be_visible()
    expect(claude_row.get_by_role("button", name="Start Claude Code login")).to_be_enabled()
    claude_row.get_by_role("button", name="Start Claude Code login").click()
    expect(claude_row.locator(".provider-oauth")).to_contain_text("Claude Code login")
    # A reload must not lose the pending login: the next health poll re-shows
    # the login card inside the expanded provider card without starting a new
    # login.
    page.reload()
    page.get_by_role("button", name="Internet Access and Tools").click()
    claude_row.get_by_label("Toggle Claude details").click()
    expect(claude_row.locator(".provider-oauth")).to_contain_text("Claude Code login", timeout=12000)
    page.once("dialog", lambda dialog: dialog.accept("mock-code"))
    page.get_by_role("button", name="Submit code").click()
    expect(claude_row.locator("[data-integration-message='claude']")).to_contain_text("Claude Code login submitted")
    with page.expect_response(
        lambda response: "/v1/agent-runtime/account" in response.url and response.request.method == "GET",
        timeout=8000,
    ):
        pass
    expect(claude_row.get_by_role("button", name="Start Claude Code login")).to_have_count(0)
    expect(claude_row).to_contain_text("connected: claude@example.invalid")
    expect(claude_row).to_contain_text("Connected account")
    expect(claude_row.locator(".connection-summary b")).to_have_count(0)
    expect(claude_row.get_by_role("button", name="Disconnect")).to_be_visible()
    claude_summary = page.locator("#runtime-overview .runtime-summary", has_text="Claude Code")
    expect(claude_summary).to_contain_text("97%")
    expect(claude_summary).to_contain_text("46%")
    # The Fable weekly window rides along as a third ring labeled "fable".
    expect(claude_summary).to_contain_text("88%")
    # Critical (session), healthy (weekly), and warning (Fable week) side by
    # side: all three ring thresholds in one chip.
    expect(claude_summary.locator(".usage-ring").nth(0)).to_have_class(re.compile(r"usage-critical"))
    expect(claude_summary.locator(".usage-ring").nth(1)).not_to_have_class(re.compile(r"usage-(warning|critical)"))
    expect(claude_summary.locator(".usage-ring").nth(2)).to_have_class(re.compile(r"usage-warning"))
    expect(claude_summary.locator(".usage-window")).to_have_text(["5h · 2h", "wk · 5d", "fable · 5d"])
    expect(claude_summary).to_have_attribute("data-action", "open-provider")
    expect(claude_summary).to_have_attribute("data-provider", "claude")
    with page.expect_response(lambda response: "/v1/agent-runtime/refresh" in response.url):
        page.locator("#runtime-overview").get_by_label("Refresh provider status and usage").click()
    expect(claude_summary).to_contain_text("63%")


def tools_smoke(page) -> None:
    """Tool rows on the Internet Access and Tools tab: seeded state, rows
    collapsed by default in the managed-integration format, config set/clear,
    enablement not gated on config, the mock OAuth connect round trip, the
    shared info popover, approval decisions, and the Tool audit log tab."""
    from playwright.sync_api import expect

    tools = page.locator("#tools")
    expect(tools).to_contain_text("Gmail")
    expect(tools).to_contain_text("Google Calendar")
    expect(tools).to_contain_text("Brave Search")

    # Discovery is the inventory boundary: every bundled package must render
    # one row and one guide without adding a hand-maintained UI registry entry.
    expected_tool_ids = sorted(
        path.parent.name
        for path in (REPO_ROOT / "host/tools").glob("*/__init__.py")
        if path.parent.name != "shared"
    )
    rendered_tool_ids = sorted(
        page.locator("#tools .integration-row[data-tool-row]").evaluate_all(
            "rows => rows.map(row => row.dataset.toolRow)"
        )
    )
    assert rendered_tool_ids == expected_tool_ids
    expect(
        page.locator("#tools [data-tool-row] .integration-info-icon")
    ).to_have_count(len(expected_tool_ids))
    expect(
        page.locator("#tools [data-integration='github'] .integration-info-icon")
    ).to_have_count(1)
    expect(
        page.locator("#ai-inference-integrations .integration-info-icon")
    ).to_have_count(3)
    expect(
        page.locator(".custom-domain-card .integration-info-icon")
    ).to_have_count(1)
    expect(page.locator(".info-button:not(:has(.integration-info-icon))")).to_have_count(0)
    expect(page.locator(".protection-lock-icon")).to_have_count(0)
    for tool_id in expected_tool_ids:
        row = page.locator(f"#tools .integration-row[data-tool-row='{tool_id}']")
        expect(row.locator("h2")).not_to_have_text("")
        expect(row.locator(".integration-subtitle")).not_to_have_text("")
        expect(row.locator("[data-tool-details]")).to_be_hidden()
        row.locator("[data-action='toggle-tool-expansion']").click()
        expect(row.locator("[data-tool-details]")).to_be_visible()
        expect(row.locator(".detail-card-head", has_text="Approvals")).to_be_visible()
        row.locator("[data-action='toggle-tool-expansion']").click()

    # Each tool is its own integration row, collapsed by default: the summary
    # shows the status chips, the chevron opens connection, config, and
    # approvals — the same format as the GitHub and OpenAI rows above.
    gmail_row = page.locator("#tools .integration-row[data-tool-row='gmail']")
    expect(gmail_row).to_contain_text("connected: akshay@infiloop.io")
    expect(page.locator("#tools .icon-tile")).to_have_count(0)
    expect(gmail_row.locator("[data-tool-details='gmail']")).to_be_hidden()
    gmail_row.get_by_role("button", name="Toggle Gmail details").click()
    expect(gmail_row.locator("[data-tool-details='gmail']")).to_be_visible()
    expect(gmail_row).to_contain_text("GOOGLE_OAUTH_CLIENT_ID")
    # The dropdown is structured like the GitHub expansion: one card per
    # concern with a sentence-case header.
    gmail_details = gmail_row.locator("[data-tool-details='gmail']")
    for concern in ("Connection", "Configuration", "Approvals"):
        expect(gmail_details.locator(".detail-card-head", has_text=concern)).to_be_visible()
    expect(gmail_details.locator(".connection-summary")).to_be_visible()
    expect(gmail_details.locator(".connection-summary b")).to_have_count(0)

    # The info popover stays high-level; detailed actions and setup live in the
    # linked integration guide.
    page.get_by_label("Gmail overview and protections", exact=True).click()
    expect(page.locator("#preset-info-popover")).not_to_contain_text("Connect your Google account")
    expect(page.locator("#preset-info-popover")).to_contain_text("OAuth tokens stay in the host credential store")
    expect(page.locator("#preset-info-popover")).to_contain_text("explicit operator approval")
    expect(page.locator("#preset-info-popover")).to_contain_text("View integration guide")
    expect(page.locator("#preset-info-popover")).not_to_contain_text("send_email")
    page.get_by_label("Gmail overview and protections", exact=True).click()
    expect(page.locator("#preset-info-popover")).to_be_hidden()

    # Protections stay operator-facing; byte-level approval binding belongs in
    # the architecture doc, not this compact popover.
    page.get_by_label("Instagram overview and protections", exact=True).click()
    expect(page.locator("#preset-info-popover")).to_contain_text(
        "Publishing happens only after your approval."
    )
    expect(page.locator("#preset-info-popover")).not_to_contain_text("SHA-256")
    page.get_by_label("Instagram overview and protections", exact=True).click()
    expect(page.locator("#preset-info-popover")).to_be_hidden()

    page.get_by_label("Interactive Brokers overview and protections", exact=True).click()
    expect(page.locator("#preset-info-popover")).to_contain_text(
        "IBKR does not make the OAuth credential read-only"
    )
    expect(page.locator("#preset-info-popover")).not_to_contain_text("Diffie-Hellman")
    expect(page.locator("#preset-info-popover")).not_to_contain_text("private RSA")
    page.get_by_label("Interactive Brokers overview and protections", exact=True).click()
    expect(page.locator("#preset-info-popover")).to_be_hidden()

    page.get_by_label("LinkedIn overview and protections", exact=True).click()
    expect(page.locator("#preset-info-popover")).to_contain_text(
        "Publishing happens only after your approval."
    )
    expect(page.locator("#preset-info-popover")).to_contain_text(
        "personal LinkedIn profile"
    )
    expect(page.locator("#preset-info-popover")).not_to_contain_text("target URN")
    expect(page.locator("#preset-info-popover")).not_to_contain_text("escaped text")
    page.get_by_label("LinkedIn overview and protections", exact=True).click()
    expect(page.locator("#preset-info-popover")).to_be_hidden()

    page.get_by_label("Instagram Discovery overview and protections", exact=True).click()
    expect(page.locator("#preset-info-popover")).to_contain_text(
        "at most 25 unique items per request"
    )
    expect(page.locator("#preset-info-popover")).not_to_contain_text("numeric audio ids")
    expect(page.locator("#preset-info-popover")).not_to_contain_text("vendor responses")
    page.get_by_label("Instagram Discovery overview and protections", exact=True).click()
    expect(page.locator("#preset-info-popover")).to_be_hidden()

    page.get_by_label("Runway Media Generation overview and protections", exact=True).click()
    expect(page.locator("#preset-info-popover")).to_contain_text(
        "local images and videos are uploaded to Runway only when used as inputs"
    )
    expect(page.locator("#preset-info-popover")).to_contain_text(
        "TrustyClaw does not publish the media"
    )
    expect(page.locator("#preset-info-popover")).to_contain_text(
        "saved from Runway's authoritative temporary URL into the agent workspace"
    )
    expect(page.locator("#preset-info-popover")).not_to_contain_text(
        "save anything"
    )
    expect(page.locator("#preset-info-popover")).not_to_contain_text("tool-scoped")
    expect(page.locator("#preset-info-popover")).not_to_contain_text("get_task")
    page.get_by_label("Runway Media Generation overview and protections", exact=True).click()
    expect(page.locator("#preset-info-popover")).to_be_hidden()

    page.get_by_label("X (Twitter) overview and protections", exact=True).click()
    expect(page.locator("#preset-info-popover")).to_contain_text(
        "Reading does not require approval."
    )
    expect(page.locator("#preset-info-popover")).to_contain_text(
        "Publishing a post, reply, or quote post happens only after your approval."
    )
    expect(page.locator("#preset-info-popover")).to_contain_text(
        "Your X credentials stay encrypted in write-only tool config."
    )
    expect(page.locator("#preset-info-popover")).not_to_contain_text(
        "app-only authentication"
    )
    expect(page.locator("#preset-info-popover")).not_to_contain_text("re-bound")
    page.get_by_label("X (Twitter) overview and protections", exact=True).click()
    expect(page.locator("#preset-info-popover")).to_be_hidden()

    # Enablement is not gated on config: Brave Search enables even before its
    # API key is set (its config status is visible in the expanded row).
    brave_row = page.locator("#tools .integration-row[data-tool-row='brave_search']")
    brave_row.get_by_role("button", name="Toggle Brave Search details").click()
    expect(brave_row).to_contain_text("not set")
    # An unused tool shows a plain empty state, not an empty table skeleton.
    expect(brave_row).to_contain_text("No approvals for this tool yet.")
    expect(brave_row.locator(".tool-approvals-table th")).to_have_count(0)
    brave_row.get_by_role("button", name="Enable").click()
    expect(brave_row).to_contain_text("enabled")
    expect(brave_row).to_contain_text("not set")
    expect(brave_row.locator("[data-tool-message='brave_search']")).to_contain_text("Brave Search enabled")
    # Setting the key afterwards flips its status; the tool stays enabled.
    page.locator("#tool-config-brave_search-BRAVE_SEARCH_API_KEY").fill("mock-brave-key")
    brave_row.get_by_role("button", name="Save").click()
    expect(brave_row).to_contain_text("set")
    expect(brave_row.locator("[data-tool-message='brave_search']")).to_contain_text("BRAVE_SEARCH_API_KEY saved")

    # The OAuth connect round trip against the mock provider: disconnect, then
    # reconnect through /oauth/callback and land back on this tab connected.
    page.once("dialog", lambda dialog: dialog.accept())
    gmail_row.get_by_role("button", name="Disconnect").click()
    expect(gmail_row).to_contain_text("not connected")
    gmail_row.get_by_role("button", name="Disable", exact=True).click()
    expect(gmail_row).to_contain_text("disabled")
    expect(gmail_row).not_to_contain_text("not connected")
    expect(gmail_row.locator(".detail-card-head", has_text="Connection")).to_have_count(0)
    gmail_row.get_by_role("button", name="Enable", exact=True).click()
    expect(gmail_row).to_contain_text("not connected")
    gmail_row.get_by_role("button", name="Connect").click()
    expect(page.locator("#panel-network")).to_be_visible()
    expect(gmail_row.locator("[data-tool-message='gmail']")).to_contain_text("Connected gmail as operator@example.com")
    expect(page.locator("#notice")).to_have_text("")
    expect(gmail_row).to_contain_text("connected: operator@example.com")

    # Every dynamically discovered tool also has a complete guide sourced from
    # its manifest: actions, setup, and four data-boundary cards. Technical notes
    # appear only when the integration has a non-duplicative implementation nuance.
    page.get_by_role("button", name="Integration Guides").click()
    for tool_id in expected_tool_ids:
        button = page.locator(
            f"#connection-guide-index button[data-guide='tool:{tool_id}']"
        )
        expect(button).to_be_visible()
        button.click()
        guide = page.locator(f"[data-guide-section='tool:{tool_id}']")
        expect(guide).to_be_visible()
        expect(guide.locator(".guide-capability")).not_to_have_count(0)
        expect(guide.locator(".guide-data-summary article")).to_have_count(4)
        # Tools whose request parameters are guarded, plus instagram_discovery
        # (its own vendor-mapping note), render a technical-details section.
        tools_with_technical_details = {
            "brave_search",
            "instagram_discovery",
            "linkedin_discovery",
            "polymarket",
            "runway",
            "twitter",
        }
        expected_technical_sections = 1 if tool_id in tools_with_technical_details else 0
        expect(guide.locator(".guide-technical-details")).to_have_count(
            expected_technical_sections
        )
        for repeated_note in (
            "Enable and Disable apply immediately to this tool only.",
            "Clear a configured secret by saving the field blank",
            "Every call, connection change, configuration change, and approval decision",
        ):
            expect(guide).not_to_contain_text(repeated_note)
    for removed_usage_step in (
        "Verify a read-only call",
        "Upload a Reel video",
        "Verify discovery without an Instagram login",
        "Plan for reconnects",
        "Verify the indexed-snippet boundary",
        "Verify public market data",
        "Verify generation and polling",
        "Stage local media only when needed",
        "Verify billing and optional personalized trends",
    ):
        expect(
            page.locator(".guide-step h4", has_text=removed_usage_step)
        ).to_have_count(0)
    page.locator(
        "#connection-guide-index button[data-guide='tool:instagram']"
    ).click()
    instagram_guide = page.locator("[data-guide-section='tool:instagram']")
    expect(instagram_guide).to_contain_text("View professional dashboard")
    expect(instagram_guide).to_contain_text(
        "the account is already professional and no conversion is needed"
    )
    expect(instagram_guide).to_contain_text(
        "the account is personal; complete the next step to convert it"
    )
    expect(instagram_guide).to_contain_text("App roles > Roles")
    expect(instagram_guide).to_contain_text("Business login settings")
    expect(instagram_guide).not_to_contain_text("stage_video")
    expect(instagram_guide).not_to_contain_text("video_url")
    my_apps_link = instagram_guide.get_by_role(
        "link", name="Open My Apps in Meta for Developers"
    )
    expect(my_apps_link).to_have_attribute("href", "https://developers.facebook.com/apps/")
    expect(my_apps_link.locator("xpath=ancestor::p")).to_have_count(1)
    expect(
        my_apps_link.locator("xpath=parent::div[contains(@class, 'guide-step-copy')]")
    ).to_have_count(0)
    expect(
        instagram_guide.get_by_role("link", name="Open Instagram tester invitations")
    ).to_have_attribute("href", "https://www.instagram.com/accounts/manage_access/")
    page.locator(
        "#connection-guide-index button[data-guide='tool:instagram_discovery']"
    ).click()
    discovery_guide = page.locator(
        "[data-guide-section='tool:instagram_discovery']"
    )
    expect(discovery_guide.locator(".guide-technical-details")).to_contain_text(
        "maps vendor responses to fixed fields"
    )
    expect(discovery_guide.locator(".guide-technical-details")).to_contain_text(
        "numeric audio ids"
    )
    expect(discovery_guide.locator(".guide-data-summary")).to_contain_text(
        "All data in a discovery request that passes TrustyClaw's validation is sent"
    )
    expect(discovery_guide.locator(".guide-data-summary")).to_contain_text(
        "retained request metadata and error logs"
    )
    expect(discovery_guide.locator(".guide-data-summary")).not_to_contain_text(
        "Never include passwords"
    )
    expect(discovery_guide.locator(".guide-data-summary")).not_to_contain_text(
        "logged like any other request"
    )
    page.locator(
        "#connection-guide-index button[data-guide='tool:linkedin_discovery']"
    ).click()
    linkedin_discovery_guide = page.locator(
        "[data-guide-section='tool:linkedin_discovery']"
    )
    expect(linkedin_discovery_guide.locator(".guide-data-summary")).to_contain_text(
        "no separate retention period for search queries or activity logs"
    )
    expect(
        linkedin_discovery_guide.get_by_role("link", name="Serper Privacy Policy").first
    ).to_have_attribute("href", "https://serper.dev/privacy")
    page.locator(
        "#connection-guide-index button[data-guide='tool:ibkr']"
    ).click()
    ibkr_guide = page.locator("[data-guide-section='tool:ibkr']")
    expect(ibkr_guide).to_contain_text("does not place this flow in the normal Client Portal menus")
    expect(ibkr_guide).to_contain_text("not an IBKR account number and it is not secret")
    expect(ibkr_guide).to_contain_text("could trade if it ever left this host")
    expect(ibkr_guide.locator(".guide-data-summary")).to_contain_text(
        "cannot send arbitrary request text, orders, or trading instructions"
    )
    expect(
        ibkr_guide.get_by_role("link", name="Open IBKR OAuth self-service")
    ).to_have_attribute(
        "href", "https://ndcdyn.interactivebrokers.com/sso/Login?RL=1&action=OAUTH"
    )
    page.locator(
        "#connection-guide-index button[data-guide='tool:linkedin']"
    ).click()
    linkedin_guide = page.locator("[data-guide-section='tool:linkedin']")
    expect(linkedin_guide).to_contain_text("personal LinkedIn profile")
    expect(linkedin_guide).to_contain_text("Understand why you need a LinkedIn Page")
    expect(linkedin_guide).to_contain_text("name a LinkedIn Page as its publisher")
    expect(linkedin_guide).to_contain_text("does not add the Page to your profile's Experience section")
    expect(linkedin_guide).to_contain_text(
        "TrustyClaw connects your personal profile and never reads from or posts to the Page"
    )
    expect(linkedin_guide).to_contain_text("For Business > Create a Company Page > Company")
    expect(linkedin_guide).to_contain_text("Myself Only")
    expect(linkedin_guide).to_contain_text("public GitHub Gist")
    expect(linkedin_guide).to_contain_text("plain temporary square image")
    expect(linkedin_guide).to_contain_text("disconnect clears only the local credential")
    expect(linkedin_guide).not_to_contain_text("Publish a simple privacy policy")
    expect(linkedin_guide).not_to_contain_text("Understand who can connect")
    expect(linkedin_guide).not_to_contain_text("no tester-only or development-mode account list")
    expect(linkedin_guide).not_to_contain_text("Create a free GitHub Pages policy")
    page.locator(
        "#connection-guide-index button[data-guide='tool:twitter']"
    ).click()
    twitter_guide = page.locator("[data-guide-section='tool:twitter']")
    expect(twitter_guide).to_contain_text("You do not need a separate website")
    expect(twitter_guide).to_contain_text("public TrustyClaw base URL")
    expect(twitter_guide.locator(".guide-data-summary")).to_contain_text(
        "Posts, replies, and quote posts"
    )
    expect(twitter_guide.locator(".guide-data-summary")).to_contain_text(
        "A post, reply, or quote post reaches X only after your approval"
    )
    page.locator(
        "#connection-guide-index button[data-guide='tool:runway']"
    ).click()
    runway_guide = page.locator("[data-guide-section='tool:runway']")
    runway_data = runway_guide.locator(".guide-data-summary")
    expect(runway_data).to_contain_text("Gen-4.5, Gen-4 Turbo, and Aleph 2")
    expect(runway_data).to_contain_text("Google Veo 3.1 or ByteDance Seedance 2")
    expect(runway_data).to_contain_text("to OpenAI's GPT Image 2")
    expect(runway_data).to_contain_text("to ElevenLabs Multilingual v2")
    expect(runway_data).to_contain_text("no self-service training opt-out")
    expect(runway_data).to_contain_text("third-party model providers do not train")
    expect(runway_data).not_to_contain_text("staged")
    expect(runway_data).not_to_contain_text("26 hours")
    page.get_by_role("button", name="Internet Access and Tools").click()

    # The callback navigation reloaded the page, so rows are collapsed again.
    # Approvals are per tool, an always-visible section of the expanded row:
    # expand Gmail's, approve its pending send, and see it flip to executed.
    expect(gmail_row.locator("[data-tool-details='gmail']")).to_be_hidden()
    gmail_row.get_by_role("button", name="Toggle Gmail details").click()
    gmail_approvals = gmail_row.locator(".tool-approvals")
    expect(gmail_approvals).to_contain_text("Invoice follow-up")
    pending_row = gmail_approvals.locator("tr", has_text="Invoice follow-up")
    pending_row.get_by_text("exact payload").click()
    expect(pending_row).to_contain_text("billing@acme.dev")
    page.once("dialog", lambda dialog: dialog.accept())
    pending_row.get_by_role("button", name="Approve").click()
    expect(gmail_row.locator("[data-tool-message='gmail']")).to_contain_text("Approved and executed")
    expect(gmail_approvals.locator("tr", has_text="Invoice follow-up")).to_contain_text("executed")
    expect(gmail_approvals.get_by_role("button", name="Approve")).to_have_count(0)

    # Another tool's approvals live under its own row: Calendar shows only its
    # own decisions (executed and pending), never Gmail's.
    calendar_row = page.locator("#tools .integration-row[data-tool-row='google_calendar']")
    calendar_row.get_by_role("button", name="Toggle Google Calendar details").click()
    calendar_approvals = calendar_row.locator(".tool-approvals")
    expect(calendar_approvals).to_contain_text("Team retro")
    expect(calendar_approvals).to_contain_text("executed")
    expect(calendar_approvals).not_to_contain_text("Invoice follow-up")

    # Denial is the other terminal decision: deny the pending calendar delete
    # and see it flip to denied with no further decision buttons.
    deny_row = calendar_approvals.locator("tr", has_text="Quarterly planning")
    deny_row.get_by_role("button", name="Deny").click()
    expect(calendar_row.locator("[data-tool-message='google_calendar']")).to_contain_text("Denied")
    expect(calendar_approvals.locator("tr", has_text="Quarterly planning")).to_contain_text("denied")
    expect(calendar_approvals.get_by_role("button", name="Deny")).to_have_count(0)

    # The Brave popover summarizes the boundary and links to setup.
    page.get_by_label("Brave Search overview and protections", exact=True).click()
    expect(page.locator("#preset-info-popover")).not_to_contain_text("Lets your agent search the public web")
    expect(page.locator("#preset-info-popover")).to_contain_text("API key stays in write-only host config")
    expect(page.locator("#preset-info-popover")).to_contain_text("read-only")
    expect(page.locator("#preset-info-popover")).to_contain_text("do not require operator approval")
    expect(page.locator("#preset-info-popover")).not_to_contain_text("Queries are capped")
    expect(page.locator("#preset-info-popover")).to_contain_text("View integration guide")
    page.get_by_label("Brave Search overview and protections", exact=True).click()

    # Clearing a config value: saving an empty input clears the key, and the
    # tool stays enabled (credential state is orthogonal to enablement).
    brave_row.get_by_role("button", name="Toggle Brave Search details").click()
    page.locator("#tool-config-brave_search-BRAVE_SEARCH_API_KEY").fill("")
    brave_row.get_by_role("button", name="Save").click()
    expect(brave_row).to_contain_text("not set")
    expect(brave_row).to_contain_text("enabled")

    # Disable flips the row back to the enable state without expanding it.
    brave_row.get_by_role("button", name="Disable").click()
    expect(brave_row.get_by_role("button", name="Enable")).to_be_enabled()
    expect(brave_row).to_contain_text("disabled")

    # Tool events live on their own audit-log tab now, next to the network
    # audit log, with the same paged table format.
    with page.expect_response(lambda response: "/v1/tools/events" in response.url):
        page.get_by_role("button", name="Tool audit log").click()
    expect(page.locator("#panel-tool-log")).to_be_visible()
    expect(page.locator("#panel-network")).to_be_hidden()
    expect(page.locator("#tool-events")).to_contain_text("brave_search")
    expect(page.locator("#tool-events")).to_contain_text("oauth_connect")
    expect(page.locator("#tool-events")).to_contain_text("Brave Search API rejected the configured API key.")
    search_event = page.locator("#tool-events tr", has_text="search_messages")
    search_event.get_by_text("view", exact=True).click()
    expect(search_event.locator("pre.metadata")).to_contain_text('"query": "invoice from last week"')
    oauth_event = page.locator("#tool-events tr", has_text="oauth_connect")
    expect(oauth_event.get_by_text("view", exact=True)).to_have_count(0)
    expect(page.locator("#tool-page-summary")).to_contain_text("Page 1")
    page.get_by_role("button", name="Internet Access and Tools").click()
    expect(page.locator("#panel-network")).to_be_visible()


def mobile_smoke(page, url: str) -> None:
    """iPhone-sized pass: layout must not overflow and core flows must work."""
    from playwright.sync_api import expect

    log_in(page, url)
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page.locator("#health")).to_contain_text("ok")
    expect(page.locator("#agent-name")).to_be_visible()
    expect(page.locator("#agent-name")).to_have_text("Host: trustyclaw-mock")
    expect(page.locator("#mobile-nav-toggle")).to_be_visible()
    expect(page.locator("#mobile-nav-toggle")).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#upgrade-notice")).to_be_visible()
    page.locator("#upgrade-notice").focus()
    expect(page.locator("#upgrade-popover")).to_be_visible()
    expect(page.locator("#nav-backdrop")).to_be_hidden()
    # Both runtimes are active by now (the desktop pass logged them in);
    # Claude Code carries the extra model-week ring.
    for runtime, rings in (("Codex", 2), ("Claude Code", 3)):
        summary = page.locator("#runtime-overview .runtime-summary", has_text=runtime)
        expect(summary).to_be_visible()
        expect(summary.locator(".usage-ring")).to_have_count(rings)
        for index in range(rings):
            expect(summary.locator(".usage-ring").nth(index)).to_be_visible()
    pi_box = page.locator("#runtime-overview .runtime-summary", has_text="Pi")
    hermes_box = page.locator("#runtime-overview .runtime-summary", has_text="Hermes")
    expect(pi_box).to_be_visible()
    expect(hermes_box).to_be_visible()
    expect(page.locator("#runtime-overview .runtime-summary-bedrock")).to_have_count(2)
    # Each box is its own live per-runtime usage readout; there is no shared
    # provider total.
    expect(page.locator("#runtime-overview .runtime-stat-cost")).to_have_count(2)
    # The hero navigator is the phone's entry into chat: visible on home
    # without opening the drawer, with a thumb-sized CTA.
    expect(page.locator("#home-hero")).to_contain_text("Agent Chat")
    hero_cta = page.locator("#home-hero").get_by_role("button", name="Begin chat", exact=True)
    expect(hero_cta).to_be_visible()
    hero_cta_box = hero_cta.bounding_box()
    if not hero_cta_box or hero_cta_box["height"] < 40:
        raise AssertionError(f"the Begin chat CTA is below thumb size on a phone: {hero_cta_box}")
    assert_no_horizontal_overflow(page, "home")

    # The drawer closes on backdrop click, Escape, and destination selection.
    open_mobile_navigation(page)
    expect(page.locator("#hero-app-tab").get_by_role("button", name="Agent Chat", exact=True)).to_be_visible()
    page.locator("#nav-backdrop").click(position={"x": 380, "y": 400})
    expect(page.locator("#nav-backdrop")).to_be_hidden()
    open_mobile_navigation(page)
    page.keyboard.press("Escape")
    expect(page.locator("#nav-backdrop")).to_be_hidden()

    mobile_go_to(page, "Agent session log", exact=True)
    expect(page.locator("#panel-agent")).to_be_visible()
    expect(page.locator("#threads")).to_contain_text("website-redesign")
    page.locator("#threads .thread-item", has_text="website-redesign").click()
    expect(page.locator("#thread-detail")).to_contain_text("denied by policy")
    assert_no_horizontal_overflow(page, "agent")

    mobile_go_to(page, "Agent audit log")
    expect(page.locator("#events")).to_contain_text("task.created")
    assert_no_horizontal_overflow(page, "agent event log")

    mobile_go_to(page, "Agent processes")
    expect(page.locator("#panel-processes")).to_be_visible()
    assert_no_horizontal_overflow(page, "agent processes")

    mobile_go_to(page, "Agent workspace", exact=True)
    expect(page.locator("#file-list")).to_contain_text("workspace")
    assert_no_horizontal_overflow(page, "agent workspace")

    mobile_go_to(page, "Internet Access and Tools")
    expect(page.locator("#panel-network")).to_be_visible()
    assert_no_horizontal_overflow(page, "internet access")
    # Wide status chips (a connected account) must not crush the row name:
    # the title keeps a readable width on a phone.
    calendar_title = page.locator(".integration-row[data-tool-row='google_calendar'] h2")
    title_box = calendar_title.bounding_box()
    if not title_box or title_box["width"] < 60:
        raise AssertionError(f"tool row title is crushed on a phone viewport: {title_box}")

    # Exercise all three summary states on a phone: managed disabled,
    # enabled-only, and enabled plus a connected OAuth identity. Status owns a
    # full line above the action pair so the account cannot be squeezed.
    disabled_row = page.locator(".integration-row[data-integration='python_packages']")
    enabled_row = page.locator(".integration-row[data-tool-row='brave_search']")
    connected_row = page.locator(".integration-row[data-tool-row='google_calendar']")
    if "disabled" in (enabled_row.locator(".status-chips").text_content() or ""):
        enabled_row.get_by_role("button", name="Enable", exact=True).click()
    state_rows = (disabled_row, enabled_row, connected_row)
    expect(state_rows[0].locator(".status-chips")).to_contain_text("disabled")
    expect(state_rows[1].locator(".status-chips")).to_contain_text("enabled")
    expect(state_rows[2].locator(".status-chips")).to_contain_text("enabled")
    expect(state_rows[2].locator(".status-chips")).to_contain_text("connected: akshay@infiloop.io")
    for row in state_rows:
        chips_box = row.locator(".status-chips").bounding_box()
        actions_box = row.locator(".integration-actions").bounding_box()
        if not chips_box or not actions_box or chips_box["y"] + chips_box["height"] > actions_box["y"] + 1:
            raise AssertionError("phone integration status overlaps or competes with its actions")
    connected_label = state_rows[2].locator(".chip-label")
    if connected_label.evaluate("element => element.scrollWidth > element.clientWidth + 1"):
        raise AssertionError("connected account identity is truncated on a phone")

    claude_subtitle = page.locator(".integration-row[data-integration='claude'] .integration-subtitle")
    expect(claude_subtitle).to_be_visible()
    expect(claude_subtitle).to_have_text("Connect your Anthropic subscription and let your agent use Claude Code for tasks. Web search is optional and off by default.")
    subtitles = page.locator(".integration-row .integration-subtitle")
    for index in range(subtitles.count()):
        subtitle = subtitles.nth(index)
        expect(subtitle).to_be_visible()
        clipped = subtitle.evaluate(
            "element => element.scrollHeight > element.clientHeight + 1 || element.scrollWidth > element.clientWidth + 1"
        )
        if clipped:
            raise AssertionError(f"integration row description {index} is clipped instead of wrapping")

    mobile_go_to(page, "Integration Guides")
    expect(page.locator("#panel-connection-guide")).to_be_visible()
    expect(page.locator("#connection-guide-index")).to_be_hidden()
    page.locator("#connection-guide-select").select_option(label="Gmail")
    expect(page.locator(".connection-guide-entry")).to_have_count(1)
    gmail_guide = page.locator("[data-guide-section='tool:gmail']")
    expect(gmail_guide.get_by_role("heading", name="Connection", exact=True)).to_have_count(1)
    expect(gmail_guide).not_to_contain_text("Connection steps")
    expect(page.locator("[data-guide-section='tool:gmail']")).to_contain_text("What happens to your data")
    expect(page.locator("#connection-guide-select")).to_be_visible()
    assert_only_guide_content_scrolls(page, "#connection-guide-select")
    assert_no_horizontal_overflow(page, "connection guide")

    mobile_go_to(page, "Network audit log")
    expect(page.locator("#net-events")).to_contain_text("deploy.acme.dev")
    expect(page.locator("#net-events")).to_contain_text("Host not allowed")
    expect(page.locator("#net-event-pager")).to_contain_text("1")
    expect(page.locator("#net-event-pager")).to_contain_text("Next")
    assert_no_horizontal_overflow(page, "network audit log")

    mobile_go_to(page, "Tool audit log")
    expect(page.locator("#tool-events")).to_contain_text("oauth_connect")
    assert_no_horizontal_overflow(page, "tool audit log")


def open_mobile_navigation(page) -> None:
    from playwright.sync_api import expect

    page.locator("#mobile-nav-toggle").click()
    expect(page.locator("#mobile-nav-toggle")).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#sidebar")).to_have_class(re.compile(r"mobile-open"))
    expect(page.locator("#nav-backdrop")).to_be_visible()
    expect(page.locator("#mobile-nav-close")).to_be_focused()
    if page.locator(".topbar").evaluate("element => element.inert") is not True:
        raise AssertionError("the top bar must be inert behind the open navigation drawer")
    if page.locator("main").evaluate("element => element.inert") is not True:
        raise AssertionError("the active page must be inert behind the open navigation drawer")


def mobile_go_to(page, name: str, *, exact: bool = False) -> None:
    from playwright.sync_api import expect

    open_mobile_navigation(page)
    page.locator("#sidebar").get_by_role("button", name=name, exact=exact).click()
    expect(page.locator("#nav-backdrop")).to_be_hidden()
    expect(page.locator("#mobile-nav-toggle")).to_have_attribute("aria-expanded", "false")
    if page.locator(".topbar").evaluate("element => element.inert") is not False:
        raise AssertionError("the top bar must leave inert state after the drawer closes")
    if page.locator("main").evaluate("element => element.inert") is not False:
        raise AssertionError("the active page must leave inert state after the drawer closes")


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
