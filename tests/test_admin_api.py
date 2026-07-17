from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import io
from http import HTTPStatus
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
import subprocess
from typing import Any
import urllib.error
import urllib.request

import pg_harness

from host.config import parse_network_controls
from host.network_integrations.github.push_gate import pending as github_pending_push
from host.runtime import (
    app_api_proxy,
    app_backend_admin_api,
    admin_api,
    github_credential,
    orchestrator,
    tools_admin_api,
    tools_api,
)
from host.runtime.network_policy import load_policy
from host.runtime.state import save_network_policy as save_policy
from host.runtime.state import save_github_credential
from host.runtime import state
from host.runtime.state import (
    append_network_event,
    read_claude_account,
    read_openai_account,
    read_proxy_claude_account,
    read_proxy_openai_account_id,
    save_config,
    save_claude_account,
    save_openai_account,
)
from state_seed import load_state, save_state


def save_approved_openai_account(account_id: str, **extra: Any) -> None:
    save_openai_account(
        {"account_id": account_id, "operator_approval": orchestrator.OPENAI_OPERATOR_APPROVAL, **extra}
    )


def save_attested_claude_account(account_id: str, **extra: Any) -> None:
    save_claude_account(
        {"account_id": account_id, "identity_attestation": orchestrator.CLAUDE_IDENTITY_ATTESTATION, **extra}
    )


class AdminUiStaticTests(unittest.TestCase):
    def test_database_free_admin_ui_contract(self) -> None:
        # The database-backed integration-test class is skipped when local PostgreSQL is
        # unavailable, but this method reads static assets only. Run the same
        # assertions here so exact UI-copy and domain-list contracts are always
        # exercised before CI.
        AdminApiIntegrationTests.test_admin_ui_has_thread_task_event_smoke_path(self)

    def test_connection_guide_preserves_network_and_data_disclosure_contracts(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime"
        catalog = (runtime / "admin_ui" / "integration_catalog.js").read_text()
        guide = (runtime / "admin_ui" / "connection_guide.js").read_text()
        html = (runtime / "admin_ui.html").read_text()

        for required_text in (
            "GraphQL denied",
            "LFS uploads denied",
            "github.com",
            "api.github.com",
            "uploads.github.com",
            "codeload.github.com",
            "objects.githubusercontent.com",
            "github-cloud.githubusercontent.com",
            "raw.githubusercontent.com",
            "release-assets.githubusercontent.com",
            "pypi.org",
            "files.pythonhosted.org",
            "nodejs.org",
            "registry.npmjs.org",
        ):
            self.assertIn(required_text, catalog)
        self.assertIn("query parameters", catalog)
        self.assertIn("renderDataSummary", guide)
        self.assertNotIn("renderDataFlows", guide)
        self.assertIn("What happens to your data", guide)
        self.assertIn("Technical notes", guide)
        self.assertIn("renderGuide(selected)", guide)
        self.assertNotIn("guide.connection", guide)
        self.assertNotIn("guides.map(renderGuide)", guide)
        self.assertNotIn("scrollIntoView", guide)
        self.assertIn("Integration Guides", html)
        self.assertNotIn("What each integration enables", html)

    def test_connection_guide_screenshots_are_local_png_assets(self) -> None:
        repo = Path(__file__).parents[1]
        asset_dir = repo / "host/tools/shared/guide_assets/google"
        mock = (repo / "tests/smoke-ui/run_admin_ui_mock.py").read_text()
        for name in (
            "google-auth-app-information.png",
            "google-auth-data-access.png",
            "google-auth-web-client.png",
        ):
            asset = asset_dir / name
            self.assertTrue(asset.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertIn(f"/guide-assets/{name}", admin_api.UI_ASSETS)
        self.assertIn('route = f"/guide-assets/{asset.name}"', mock)
        self.assertFalse((repo / "host/runtime/admin_ui/guide_assets").exists())

    def test_disabled_integrations_omit_irrelevant_connection_state(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime"
        network_js = (runtime / "admin_ui" / "network.js").read_text()
        tools_js = (runtime / "admin_ui" / "tools.js").read_text()
        css = (runtime / "admin_ui.css").read_text()

        self.assertIn('if (!enabled) {\n      setHtml(node, "");', network_js)
        self.assertIn('const summary = !enabled && !identity', network_js)
        self.assertIn('tool.connection === "oauth" && (tool.enabled || connected)', tools_js)
        self.assertIn(".connection-summary {", css)
        self.assertIn("color: var(--text-dim);", css)

    def test_mobile_navigation_uses_an_accessible_drawer(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime"
        html = (runtime / "admin_ui.html").read_text()
        app_js = (runtime / "admin_ui" / "app.js").read_text()
        css = (runtime / "admin_ui.css").read_text()

        self.assertIn('id="mobile-nav-toggle"', html)
        self.assertIn('aria-controls="sidebar"', html)
        self.assertIn('id="nav-backdrop"', html)
        self.assertIn("function setMobileNavOpen(open, restoreFocus = false)", app_js)
        self.assertIn('sidebar.inert = mobile && !mobileNavOpen', app_js)
        self.assertIn('document.querySelector(".topbar").inert = mobileNavOpen', app_js)
        self.assertIn('document.querySelector("main").inert = mobileNavOpen', app_js)
        self.assertIn(".sidebar.mobile-open { transform: translateX(0); }", css)

    def test_provider_usage_rings_have_warning_and_critical_thresholds(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime"
        health_js = (runtime / "admin_ui" / "health.js").read_text()
        css = (runtime / "admin_ui.css").read_text()

        self.assertIn('percent > 90 ? " usage-critical" : percent > 80 ? " usage-warning"', health_js)
        # The rings use the muted usage-* palette so they read quietly in the
        # dark top bar; ok/warning/critical stay visually distinct.
        self.assertIn(".usage-ring.usage-warning .usage-ring-value { stroke: var(--usage-warn); }", css)
        self.assertIn(".usage-ring.usage-critical .usage-ring-value { stroke: var(--usage-critical); }", css)

    def test_provider_usage_rings_show_compact_reset_countdowns(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime"
        health_js = (runtime / "admin_ui" / "health.js").read_text()
        css = (runtime / "admin_ui.css").read_text()

        self.assertIn("function resetCountdown(value, now = Date.now())", health_js)
        self.assertIn("current_session_resets_at", health_js)
        self.assertNotIn("resets_at_text", health_js)
        # The countdown shares the single window-label line under the ring so
        # the top bar keeps a constant height.
        self.assertIn('${esc(label)}${countdown ? ` · ${countdown}` : ""}', health_js)
        self.assertNotIn("usage-reset", health_js)
        self.assertNotIn(".usage-reset {", css)
        self.assertIn(".usage-window {", css)

    def test_upgrade_notice_is_compact_accessible_and_version_driven(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime"
        html = (runtime / "admin_ui.html").read_text()
        health_js = (runtime / "admin_ui" / "health.js").read_text()
        css = (runtime / "admin_ui.css").read_text()

        self.assertIn('id="upgrade-notice"', html)
        self.assertIn('id="upgrade-popover"', html)
        self.assertNotIn('href="https://github.com/infiloop2/trustyclaw', html)
        self.assertIn("renderUpgradeNotice(health.upgrade)", health_js)
        self.assertIn('"Your TrustyClaw is at the latest version."', health_js)
        self.assertIn('"Use your operator plane to upgrade."', health_js)
        self.assertIn('notice.setAttribute("aria-label", label)', health_js)
        self.assertIn(".upgrade-notice {", css)
        self.assertIn("background: var(--ok-soft);", css)
        self.assertIn("border-radius: 999px;", css)
        self.assertIn(".upgrade-popover {", css)
        self.assertIn(".upgrade-notice:hover .upgrade-popover", css)

    def test_icons_have_intrinsic_sizes_and_share_the_favicon_asset(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime"
        html = (runtime / "admin_ui.html").read_text()
        css = (runtime / "admin_ui.css").read_text()

        favicon_src = '/favicon.svg'
        self.assertIn(f'<img class="brand-mark" width="30" height="30" src="{favicon_src}" alt="">', html)
        self.assertIn(f'<img class="login-mark" width="44" height="44" src="{favicon_src}" alt="">', html)
        self.assertEqual(html.count('<svg width="19" height="19" viewBox="0 0 20 20"'), 9)
        self.assertIn('/favicon.svg', html)
        self.assertIn('/favicon.ico', html)
        self.assertIn('/admin_ui.css', html)
        self.assertIn('<script type="module" src="/admin_ui/app.js"></script>', html)
        self.assertIn(".brand-mark { display: block; flex: 0 0 30px; height: 30px; width: 30px; }", css)
        self.assertIn(".login-mark { display: inline-block; height: 44px; margin-bottom: 0.4rem; width: 44px; }", css)
        self.assertIn(".tab-button svg { display: block; height: 19px; width: 19px; }", css)
        self.assertIn(".memory-swap-values", css)
        self.assertIn("button.icon-button svg", css)
        self.assertNotIn("animation: panel-in", css)
        self.assertIn("position: fixed", css)
        self.assertIn('id="tab-processes"', html)
        self.assertIn('id="processes"', html)
        self.assertIn('id="sidebar-apps"', html)
        self.assertIn('id="app-tabs"', html)
        self.assertIn('id="app-tabs"', html)
        self.assertIn("under development and may not function properly", html)
        app_js = (runtime / "admin_ui" / "app.js").read_text()
        self.assertIn("trustyclaw-app-api", app_js)
        self.assertIn("trustyclaw-app-open-file", app_js)
        self.assertIn('"X-TrustyClaw-App-Bridge": app.id', app_js)
        # The bridge scope is enforced server-side (route() 403s any
        # bridge-tagged request outside the app's own API); the shell keeps
        # only a friendly prefix pre-check.
        bridge_code = app_js.split("async function handleAppApiMessage", 1)[1].split("document.addEventListener", 1)[0]
        self.assertIn("app.backend.api_route", bridge_code)
        self.assertNotIn("/v1/tasks", bridge_code)
        self.assertNotIn("host-runtime", bridge_code)
        self.assertNotIn("network/policy", bridge_code)
        self.assertNotIn("agent-files", bridge_code)
        files_js = (runtime / "admin_ui" / "files.js").read_text()
        self.assertNotIn(".innerHTML", files_js)
        self.assertIn("button.textContent", files_js)
        self.assertIn("video.src = activeFileUrl", files_js)
        self.assertNotIn("window.open", files_js)
        self.assertNotIn("location.", files_js)

    def test_app_backend_auth_maps_peer_uid_to_installed_app(self) -> None:
        class User:
            pw_uid = 12345

        with patch("host.runtime.app_backend_admin_api.pwd.getpwnam", return_value=User()):
            self.assertEqual(app_backend_admin_api.app_id_for_peer_uid(12345), "agent_chat")
            self.assertIsNone(app_backend_admin_api.app_id_for_peer_uid(54321))

    def test_app_backend_auth_rejects_claimed_app_id_mismatch(self) -> None:
        class Request:
            pass

        class Handler:
            headers = {"X-TrustyClaw-App-Backend": "agent_chat"}
            request = Request()

        with (
            patch("host.runtime.app_backend_admin_api._peer_uid", return_value=12345),
            patch("host.runtime.app_backend_admin_api.app_id_for_peer_uid", return_value="other_app"),
            self.assertRaises(admin_api.ApiError) as error,
        ):
            app_backend_admin_api.Handler._authenticate_app_backend_id(Handler())  # type: ignore[arg-type]

        self.assertEqual(error.exception.status, HTTPStatus.UNAUTHORIZED)

    def test_app_backend_route_allowlist_starts_with_task_routes(self) -> None:
        allowed = [
            ("POST", "/v1/tasks"),
            ("GET", "/v1/tools"),
            ("GET", "/v1/network/policy"),
            ("GET", "/v1/tasks/task_1"),
            ("POST", "/v1/tasks/task_1/cancel"),
            ("POST", "/v1/tasks/task_1/kill"),
            ("POST", "/v1/tasks/task_1/steer"),
            ("GET", "/v1/threads"),
            ("GET", "/v1/threads/thread_1/tasks"),
            ("GET", "/v1/threads/thread_1/events"),
        ]

        for method, path in allowed:
            with self.subTest(method=method, path=path):
                app_backend_admin_api._require_app_backend_route(method, path)

    def test_app_backend_route_allowlist_rejects_host_admin_routes(self) -> None:
        denied = [
            ("GET", "/v1/tasks"),
            ("PUT", "/v1/tasks/task_1"),
            ("GET", "/v1/health"),
            ("PUT", "/v1/network/policy"),
            ("GET", "/v1/agent-files"),
            ("POST", "/v1/tools/brave_search/enable"),
            ("GET", "/v1/tools/brave_search/approvals"),
            ("GET", "/v1/network-tools/github-credential"),
            ("GET", "/v1/agent-runtime/account"),
            ("GET", "/v1/events"),
        ]

        for method, path in denied:
            with self.subTest(method=method, path=path):
                with self.assertRaises(admin_api.ApiError) as error:
                    app_backend_admin_api._require_app_backend_route(method, path)
                self.assertEqual(error.exception.status, HTTPStatus.FORBIDDEN)

    def test_app_backend_prefixes_thread_ids_and_strips_responses(self) -> None:
        body = app_backend_admin_api._internal_body(
            "agent_chat",
            "POST",
            "/v1/tasks",
            {"input_message": "x", "thread_id": "chat", "agent_runtime": "codex"},
        )
        self.assertEqual(body["thread_id"], "agent_chat__chat")
        self.assertEqual(
            app_backend_admin_api._internal_path("agent_chat", "GET", "/v1/threads/chat/tasks"),
            "/v1/threads/agent_chat__chat/tasks",
        )

        response = app_backend_admin_api._visible_response(
            "agent_chat",
            {"tasks": [{"task_id": "task_1", "thread_id": "agent_chat__chat"}]},
        )
        self.assertEqual(response["tasks"][0]["task_id"], "task_1")
        self.assertEqual(response["tasks"][0]["thread_id"], "chat")

    def test_app_backend_thread_task_listing_uses_app_prefixed_thread_path(self) -> None:
        with patch(
            "host.runtime.app_backend_admin_api.admin_api.route",
            return_value={"tasks": [{"task_id": "task_1", "thread_id": "agent_chat__chat"}]},
        ) as route:
            response = app_backend_admin_api.route_app_backend_request(
                "agent_chat", "GET", "/v1/threads/chat/tasks", {}, None
            )

        route.assert_called_once_with("GET", "/v1/threads/agent_chat__chat/tasks", {}, None, app_backend_id="agent_chat")
        self.assertEqual(response["tasks"], [{"task_id": "task_1", "thread_id": "chat"}])

    def test_app_backend_thread_events_use_app_prefixed_thread_path(self) -> None:
        with patch(
            "host.runtime.app_backend_admin_api.admin_api.route",
            return_value={"events": [{"seq": 4, "task_id": "task_1", "event_type": "task.message"}]},
        ) as route:
            response = app_backend_admin_api.route_app_backend_request(
                "agent_chat", "GET", "/v1/threads/chat/events", {"since": ["2"]}, None
            )

        route.assert_called_once_with(
            "GET", "/v1/threads/agent_chat__chat/events", {"since": ["2"]}, None, app_backend_id="agent_chat"
        )
        self.assertEqual(response["events"][0]["seq"], 4)

    def test_app_backend_bulk_thread_list_is_filtered_to_the_calling_app(self) -> None:
        with patch(
            "host.runtime.app_backend_admin_api.admin_api.route",
            return_value={
                "threads": [
                    {"thread_id": "agent_chat__chat", "task_count": 2},
                    {"thread_id": "other_app__notes", "task_count": 1},
                    {"thread_id": "agent_chat__docs", "task_count": 5},
                ]
            },
        ) as route:
            response = app_backend_admin_api.route_app_backend_request(
                "agent_chat", "GET", "/v1/threads", {}, None
            )

        route.assert_called_once_with("GET", "/v1/threads", {}, None, app_backend_id="agent_chat")
        # Only this app's threads survive, and the prefix is stripped from each.
        self.assertEqual(
            response["threads"],
            [{"thread_id": "chat", "task_count": 2}, {"thread_id": "docs", "task_count": 5}],
        )

    def test_app_backend_visible_response_rejects_unscoped_thread_ids(self) -> None:
        with self.assertRaises(admin_api.ApiError) as error:
            app_backend_admin_api._visible_response(
                "agent_chat",
                {"tasks": [{"task_id": "task_2", "thread_id": "other_app__chat"}]},
            )

        self.assertEqual(error.exception.status, HTTPStatus.BAD_GATEWAY)

    def test_app_backend_task_scope_rejects_other_thread_prefixes(self) -> None:
        with (
            patch("host.runtime.app_backend_admin_api.state.get_task", return_value={"thread_id": "other_app__chat"}),
            self.assertRaises(admin_api.ApiError) as error,
        ):
            app_backend_admin_api._require_app_task_scope("agent_chat", "GET", "/v1/tasks/task_1")

        self.assertEqual(error.exception.status, HTTPStatus.NOT_FOUND)

    def test_app_backend_task_routes_scope_raw_task_ids_before_forwarding(self) -> None:
        with (
            patch("host.runtime.app_backend_admin_api.state.get_task", return_value={"thread_id": "agent_chat__chat"}),
            patch(
                "host.runtime.app_backend_admin_api.admin_api.route",
                return_value={"task_id": "task_1", "thread_id": "agent_chat__chat"},
            ) as route,
        ):
            response = app_backend_admin_api.route_app_backend_request(
                "agent_chat", "GET", "/v1/tasks/task_1", {}, None
            )

        route.assert_called_once_with("GET", "/v1/tasks/task_1", {}, None, app_backend_id="agent_chat")
        self.assertEqual(response, {"task_id": "task_1", "thread_id": "chat"})

    def test_host_task_ids_stay_raw_for_app_threads(self) -> None:
        task = {
            "task_id": "task_1",
            "status": "queued",
            "agent_runtime": "codex",
            "model": "gpt-5.6-terra",
            "effort": "high",
            "thread_id": "agent_chat__chat",
            "input_message": "x",
            "created_at": "2026-06-08T00:00:00Z",
            "updated_at": "2026-06-08T00:00:00Z",
        }
        with patch("host.runtime.admin_api.state.get_task", return_value=task) as get_task:
            response = admin_api.get_task("task_1")

        get_task.assert_called_once_with("task_1")
        self.assertEqual(response["task_id"], "task_1")
        self.assertEqual(response["thread_id"], "agent_chat__chat")

    def test_admin_task_create_rejects_app_reserved_thread_prefixes(self) -> None:
        with self.assertRaises(admin_api.ApiError) as error:
            admin_api._validate_thread_id_not_reserved_by_app("agent_chat__chat", None)

        self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

        with self.assertRaises(admin_api.ApiError):
            admin_api._validate_thread_id_not_reserved_by_app("future_app__chat", None)
        admin_api._validate_thread_id_not_reserved_by_app("agent_chat__chat", "agent_chat")
        with self.assertRaises(admin_api.ApiError):
            admin_api._validate_thread_id_not_reserved_by_app("agent_chat__", "agent_chat")

    def test_app_bridge_marker_cannot_leave_the_apps_own_api(self) -> None:
        # A bridge-tagged request is scoped server-side before any dispatch:
        # another app's API, host task routes, and the network policy are all
        # 403 with nothing executed.
        for path in (
            "/v1/apps/agent_chat/api/health",
            "/v1/tasks",
            "/v1/network/policy",
        ):
            with self.subTest(path=path):
                with self.assertRaises(admin_api.ApiError) as error:
                    admin_api.route("GET", path, {}, None, bridge_app_id="other-app")
                self.assertEqual(error.exception.status, HTTPStatus.FORBIDDEN)


class AgentProcessSnapshotTests(unittest.TestCase):
    def test_agent_processes_reads_descendant_cgroup_proc_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cgroup = root / "cgroup" / "run-codex.scope"
            proc = root / "proc"
            cgroup.mkdir(parents=True)
            proc.mkdir()
            (cgroup / "cgroup.procs").write_text("123\nnot-a-pid\n")
            (proc / "uptime").write_text("300.00 1000.00\n")

            proc_123 = proc / "123"
            proc_123.mkdir()
            stat_fields = ["S", "1", *["0"] * 17, "200"]
            (proc_123 / "stat").write_text(f"123 (codex app) {' '.join(stat_fields)}\n")
            (proc_123 / "status").write_text("Name:\tcodex\nUid:\t47743\t47743\t47743\t47743\nVmRSS:\t1234 kB\n")
            (proc_123 / "cmdline").write_bytes(b"codex\0app-server\0--listen\0stdio://\0")

            with (
                patch("host.runtime.admin_api.AGENT_CGROUP_ROOT", root / "cgroup"),
                patch("host.runtime.admin_api.PROC_ROOT", proc),
            ):
                snapshot = admin_api.agent_processes()

        self.assertFalse(snapshot["truncated"])
        self.assertEqual(len(snapshot["processes"]), 1)
        process = snapshot["processes"][0]
        # Exactly the fields the admin UI renders.
        self.assertEqual(process["pid"], 123)
        self.assertEqual(process["state"], "S")
        self.assertEqual(process["name"], "codex")
        self.assertEqual(process["cmdline"], "codex app-server --listen stdio://")
        self.assertEqual(process["rss_bytes"], 1234 * 1024)
        self.assertGreaterEqual(process["elapsed_seconds"], 0)
        self.assertEqual(
            set(process), {"pid", "state", "name", "cmdline", "rss_bytes", "elapsed_seconds"}
        )


class AdminApiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.proxy_temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.proxy_temp_dir.cleanup)
        self.env_patch = patch.dict(
            "os.environ",
            {"TRUSTYCLAW_STATE_DIR": self.temp_dir.name, "TRUSTYCLAW_PROXY_STATE_DIR": self.proxy_temp_dir.name},
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        save_config(
            {
                "agent_name": "trustyclaw-test",
                "admin_password_sha256": hashlib.sha256(b"admin-secret").hexdigest(),
            }
        )
        save_policy(
            {"network_integrations": {"openai": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "active"
        state["agent_runtime_statuses"]["claude_code"]["status"] = "deactivated"
        save_state(state)
        self.reconcile_patch = patch(
            "host.runtime.admin_api.orchestrator.reconcile_runtime_status_after_policy_change"
        )
        self.mock_reconcile = self.reconcile_patch.start()
        self.addCleanup(self.reconcile_patch.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), admin_api.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(self, method: str, path: str, body: object | None = None, auth: bool = True):
        if method == "POST" and path == "/v1/tasks" and isinstance(body, dict):
            body = dict(body)
            if "model" not in body and "effort" not in body:
                if body.get("agent_runtime") == "claude_code":
                    body.update({"model": "opus", "effort": "high"})
                elif body.get("agent_runtime") == "codex":
                    body.update({"model": "gpt-5.6-terra", "effort": "high"})
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        if auth:
            request.add_header("Authorization", "Bearer admin-secret")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def raw_request(self, request: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", self.server.server_address[1]), timeout=5) as sock:
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)

    def health(self, proxy_alive: bool = True):
        with (
            patch("host.runtime.admin_api.host_metrics", return_value={"cpu": {}, "memory": {}, "filesystem": {}, "swap": {}}),
            patch("host.runtime.admin_api.proxy_alive", return_value=proxy_alive),
            patch("host.runtime.admin_api.version_status", return_value={"status": "ok", "runtime": "0.2.0", "state": "0.2.0"}),
            patch(
                "host.runtime.admin_api.upgrade_check.status",
                return_value={"available": True, "latest": "0.3.0"},
            ),
        ):
            return self.request("GET", "/v1/health")

    def runtime(self, body: dict[str, object], runtime_type: str = "codex") -> dict[str, object]:
        runtimes = body["agent_runtime"]["runtimes"]  # type: ignore[index]
        return next(item for item in runtimes if item["type"] == runtime_type)  # type: ignore[union-attr]

    def app_backend_request(
        self,
        method: str,
        path: str,
        body: object | None = None,
    ) -> dict[str, Any]:
        return app_backend_admin_api.route_app_backend_request(
            "agent_chat",
            method,
            path,
            {},
            body,
        )

    def test_health_requires_auth_and_reports_state(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("GET", "/v1/health", auth=False)
        self.assertEqual(error.exception.code, 401)

        status, body = self.health()

        self.assertEqual(status, 200)
        self.assertEqual(body["agent_name"], "trustyclaw-test")
        self.assertEqual(self.runtime(body)["status"], "active")
        self.assertEqual(self.runtime(body, "claude_code")["status"], "deactivated")
        self.assertEqual(body["network_controls"]["status"], "active")
        self.assertEqual(body["version"], {"status": "ok", "runtime": "0.2.0", "state": "0.2.0"})
        self.assertEqual(body["upgrade"], {"available": True, "latest": "0.3.0"})

    def test_apps_endpoint_requires_auth_and_lists_agent_chat(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("GET", "/v1/apps", auth=False)
        self.assertEqual(error.exception.code, 401)

        status, body = self.request("GET", "/v1/apps")

        self.assertEqual(status, 200)
        app = next(item for item in body["apps"] if item["id"] == "agent_chat")
        self.assertEqual(app["title"], "Agent Chat")
        self.assertEqual(app["ui"]["iframe_src"], "/v1/apps/agent_chat/ui/index.html")
        self.assertEqual(app["backend"]["api_route"], "/v1/apps/agent_chat/api/")

    def test_agent_file_content_route_requires_operator_auth(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("GET", "/v1/agent-files/content?path=%2Fworkspace%2Freel.mp4", auth=False)
        self.assertEqual(error.exception.code, 401)

    def test_app_backend_header_does_not_authenticate_tcp_admin_api(self) -> None:
        request = urllib.request.Request(f"{self.base_url}/v1/threads", method="GET")
        request.add_header("X-TrustyClaw-App-Backend", "agent_chat")

        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)

        self.assertEqual(error.exception.code, 401)

    def test_app_backend_task_threads_are_internally_app_prefixed(self) -> None:
        task = self.app_backend_request(
            "POST",
            "/v1/tasks",
            {
                "input_message": "from app",
                "thread_id": "chat",
                "agent_runtime": "codex",
                "model": "gpt-5.6-terra",
                "effort": "high",
            },
        )

        self.assertEqual(task["thread_id"], "chat")
        stored = state.get_task(task["task_id"])
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored["thread_id"], "agent_chat__chat")

        _, host_visible_thread_task = self.request(
            "POST",
            "/v1/tasks",
            {"input_message": "host-visible chat", "thread_id": "chat", "agent_runtime": "codex"},
        )

        listed = self.app_backend_request("GET", "/v1/threads/chat/tasks")
        self.assertEqual(
            [(item["task_id"], item["thread_id"]) for item in listed["tasks"]],
            [(task["task_id"], "chat")],
        )
        self.assertNotIn(
            host_visible_thread_task["task_id"],
            {item["task_id"] for item in listed["tasks"]},
        )

        app_task = self.app_backend_request("GET", f"/v1/tasks/{task['task_id']}")
        self.assertEqual(app_task["task_id"], task["task_id"])
        self.assertEqual(app_task["thread_id"], "chat")

        _, host_listed = self.request("GET", "/v1/threads/agent_chat__chat/tasks")
        self.assertEqual(host_listed["tasks"][0]["task_id"], task["task_id"])
        self.assertEqual(host_listed["tasks"][0]["thread_id"], "agent_chat__chat")

        _, host_task = self.request("GET", f"/v1/tasks/{task['task_id']}")
        self.assertEqual(host_task["task_id"], task["task_id"])
        self.assertEqual(host_task["thread_id"], "agent_chat__chat")

    def test_app_backend_repeated_task_create_makes_another_task(self) -> None:
        request = {
            "input_message": "from app",
            "thread_id": "repeated-create",
            "agent_runtime": "codex",
            "model": "gpt-5.6-terra",
            "effort": "high",
        }
        first = self.app_backend_request("POST", "/v1/tasks", request)
        repeated = self.app_backend_request("POST", "/v1/tasks", request)

        self.assertNotEqual(repeated["task_id"], first["task_id"])
        self.assertEqual(len(state.tasks_for_thread("agent_chat__repeated-create", 10)), 2)

    def test_app_backend_repeated_steer_appends_again(self) -> None:
        task = self.app_backend_request(
            "POST",
            "/v1/tasks",
            {
                "input_message": "from app",
                "thread_id": "durable-steer",
                "agent_runtime": "codex",
                "model": "gpt-5.6-terra",
                "effort": "high",
            },
        )
        with state.mutation() as cur:
            stored = state.get_task(task["task_id"], cur)
            assert stored is not None
            stored["status"] = "running"
            state.save_task(cur, stored)

        first = self.app_backend_request(
            "POST",
            f"/v1/tasks/{task['task_id']}/steer",
            {"steer_message": "nudge"},
        )
        repeated = self.app_backend_request(
            "POST",
            f"/v1/tasks/{task['task_id']}/steer",
            {"steer_message": "nudge"},
        )

        self.assertEqual(first, repeated)
        self.assertEqual(state.task_steers(task["task_id"]), ["nudge", "nudge"])

    def test_app_backend_task_lookup_rejects_unscoped_task_ids(self) -> None:
        _, outside = self.request(
            "POST",
            "/v1/tasks",
            {"input_message": "outside app", "thread_id": "outside", "agent_runtime": "codex"},
        )

        with self.assertRaises(admin_api.ApiError) as error:
            self.app_backend_request("GET", f"/v1/tasks/{outside['task_id']}")

        self.assertEqual(error.exception.status, HTTPStatus.NOT_FOUND)

    def test_app_backend_thread_id_limit_includes_hidden_prefix(self) -> None:
        with self.assertRaises(admin_api.ApiError) as error:
            self.app_backend_request(
                "POST",
                "/v1/tasks",
                {
                    "input_message": "too long",
                    "thread_id": "a" * 64,
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-terra",
                    "effort": "high",
                },
            )

        self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("too long", error.exception.message)

    def test_app_ui_asset_is_frameable_static_content(self) -> None:
        request = urllib.request.Request(f"{self.base_url}/v1/apps/agent_chat/ui/index.html", method="GET")
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode()

        self.assertEqual(response.status, 200)
        self.assertIn("Agent Chat", body)
        self.assertIn('href="agent_chat.css"', body)
        self.assertIn('src="agent_chat.js"', body)
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'self'", csp)
        self.assertIn("frame-src 'none'", csp)
        img_src = next((directive for directive in csp.split("; ") if directive.startswith("img-src ")), "")
        self.assertIn("'self'", img_src)
        self.assertIn("data:", img_src)
        self.assertIn("navigate-to 'self'", csp)
        self.assertNotIn("media-src", csp)
        self.assertIn("sandbox allow-scripts allow-forms allow-modals", csp)
        self.assertNotIn("allow-same-origin", csp)
        script_src = next((directive for directive in csp.split("; ") if directive.startswith("script-src ")), "")
        self.assertIn("'self'", script_src)
        self.assertNotIn("'unsafe-inline'", script_src)
        self.assertIn("style-src 'self' 'unsafe-inline'", csp)
        self.assertNotIn("img-src *", csp)
        self.assertNotIn("style-src *", csp)

        for asset_name, content_type, expected in (
            ("agent_chat.css", "text/css", ".chat-app"),
            ("agent_chat.js", "application/javascript", "trustyclaw-app-api"),
        ):
            request = urllib.request.Request(f"{self.base_url}/v1/apps/agent_chat/ui/{asset_name}", method="GET")
            with urllib.request.urlopen(request, timeout=5) as response:
                asset_body = response.read().decode()

            self.assertEqual(response.status, 200)
            self.assertTrue(response.headers["Content-Type"].startswith(content_type))
            self.assertIn(expected, asset_body)

    def test_app_api_proxy_does_not_forward_admin_bearer_to_app_backend(self) -> None:
        captured: dict[str, Any] = {}

        class FakeResponse:
            status = 200

            def read(self, _limit: int) -> bytes:
                return b'{"status":"ok"}'

        class FakeConnection:
            def __init__(self, host: str, port: int, timeout: int) -> None:
                captured["connect"] = (host, port, timeout)

            def request(
                self,
                method: str,
                target: str,
                body: bytes | None = None,
                headers: dict[str, str] | None = None,
            ) -> None:
                captured["request"] = (method, target, body, headers or {})

            def getresponse(self) -> FakeResponse:
                return FakeResponse()

            def close(self) -> None:
                captured["closed"] = True

        app = admin_api.app_platform.app_by_id("agent_chat")
        if app is None:
            self.fail("agent_chat app should be installed")
        with patch("host.runtime.app_api_proxy.http.client.HTTPConnection", FakeConnection):
            body = app_api_proxy.proxy_app_api(app, "GET", "/health", {}, None)

        self.assertEqual(body, {"status": "ok"})
        self.assertEqual(captured["connect"][1], 7450)
        headers = captured["request"][3]
        self.assertEqual(headers["X-TrustyClaw-App-Proxy"], "agent_chat")
        self.assertNotIn("Authorization", headers)

    def test_filesystem_metrics_reports_root_and_data_mounts(self) -> None:
        class Usage:
            def __init__(self, used: int, total: int) -> None:
                self.used = used
                self.total = total

        def fake_disk_usage(path: str) -> Usage:
            values = {
                "/": Usage(1, 10),
                "/mnt/trustyclaw-admin": Usage(2, 20),
                "/mnt/trustyclaw-agent": Usage(3, 30),
            }
            return values[path]

        with patch("host.runtime.admin_api.shutil.disk_usage", side_effect=fake_disk_usage):
            metrics = admin_api.filesystem_metrics()

        self.assertEqual(metrics["mounts"], {
            "root": {"used_bytes": 1, "total_bytes": 10},
            "admin": {"used_bytes": 2, "total_bytes": 20},
            "agent": {"used_bytes": 3, "total_bytes": 30},
        })

    def test_malformed_or_huge_content_length_returns_4xx(self) -> None:
        invalid = self.raw_request(
            b"POST /v1/tasks HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer admin-secret\r\n"
            b"Content-Length: nope\r\n\r\n"
        )
        huge = self.raw_request(
            b"POST /v1/tasks HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer admin-secret\r\n"
            b"Content-Length: 1048577\r\n\r\n"
        )

        self.assertIn(b"400", invalid)
        self.assertIn(b"malformed Content-Length", invalid)
        self.assertIn(b"413", huge)
        self.assertIn(b"request body too large", huge)

    def test_health_reports_error_when_proxy_is_down(self) -> None:
        _, body = self.health(proxy_alive=False)
        self.assertEqual(body["network_controls"]["status"], "error")
        self.assertEqual(body["status"], "degraded")

    def test_health_never_spawns_codex(self) -> None:
        # The health/status path must read cached state only — a hanging Codex
        # app-server must never be able to block it.
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "loading"
        save_state(state)
        with patch(
            "host.runtime.orchestrator.codex_app_server.account_status",
            side_effect=AssertionError("health must not call Codex"),
        ):
            _, body = self.health()
        self.assertEqual(self.runtime(body)["status"], "loading")

    def test_runtime_status_loop_refreshes_cached_status(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "loading"
        save_state(state)
        with patch(
            "host.runtime.orchestrator.codex_app_server.account_status",
            return_value=("awaiting_login", None, None),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")
        _, body = self.request("GET", "/v1/agent-runtime/status")
        self.assertEqual(self.runtime({"agent_runtime": body})["status"], "awaiting_login")
        self.assertNotIn("error_message", self.runtime({"agent_runtime": body}))

    def test_runtime_status_error_surfaces_error_message(self) -> None:
        with patch(
            "host.runtime.orchestrator.codex_app_server.account_status",
            return_value=("error", "timed out waiting for Codex app-server; app-server stderr: boom", None),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "error")
        _, body = self.request("GET", "/v1/agent-runtime/status")
        self.assertEqual(self.runtime({"agent_runtime": body})["status"], "error")
        self.assertIn("boom", self.runtime({"agent_runtime": body})["error_message"])
        # The error message clears once the runtime recovers.
        with patch(
            "host.runtime.orchestrator.codex_app_server.account_status",
            return_value=("awaiting_login", None, None),
        ):
            orchestrator.refresh_runtime_status("codex")
        _, body = self.request("GET", "/v1/agent-runtime/status")
        self.assertNotIn("error_message", self.runtime({"agent_runtime": body}))

    def test_disabled_provider_runtime_is_deactivated_without_cli_check(self) -> None:
        save_claude_account({"account_id": "acct_smoke", "access_token_sha256": "f" * 64})
        state = load_state()
        state["agent_runtime_statuses"]["claude_code"]["status"] = "active"
        state["agent_runtime_statuses"]["claude_code"]["error_message"] = "old failure"
        state["claude_oauth"] = {
            "status": "awaiting_code",
            "login_url": "https://claude.com/cai/oauth/authorize",
            "expires_at": "2099-06-08T00:10:00Z",
        }
        save_state(state)

        with patch(
            "host.runtime.orchestrator.claude_code.account_status",
            side_effect=AssertionError("disabled Claude runtime must not touch Claude Code"),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "deactivated")

        state = load_state()
        self.assertEqual(state["agent_runtime_statuses"]["claude_code"]["status"], "deactivated")
        self.assertNotIn("error_message", state["agent_runtime_statuses"]["claude_code"])
        self.assertIsNone(state["claude_oauth"])
        self.assertEqual(read_claude_account(), {"account_id": "acct_smoke", "access_token_sha256": "f" * 64})
        _, body = self.request("GET", "/v1/agent-runtime/status")
        self.assertNotIn("error_message", self.runtime({"agent_runtime": body}, "claude_code"))

    def test_ui_page_is_served_without_auth(self) -> None:
        request = urllib.request.Request(f"{self.base_url}/")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])
            self.assert_security_headers(response.headers)
            page = response.read().decode()
        self.assertIn("TrustyClaw", page)
        self.assertIn('/admin_ui.css', page)
        self.assertIn('/admin_ui/app.js', page)

        for path, content_type, expected in (
            ("/admin_ui.css", "text/css", ".shell"),
            ("/admin_ui/app.js", "application/javascript", "setInterval(tick, 5000)"),
            ("/admin_ui/health.js", "application/javascript", "/v1/health"),
            ("/workspace-kit/view_blocks.css", "text/css", ".b-text"),
            ("/workspace-kit/view_blocks.js", "application/javascript", "function renderBlock"),
            ("/favicon.ico", "image/svg+xml", "<svg"),
            ("/favicon.svg", "image/svg+xml", "<svg"),
        ):
            request = urllib.request.Request(f"{self.base_url}{path}")
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(content_type, response.headers["Content-Type"])
                self.assert_security_headers(response.headers)
                body = response.read().decode()
            self.assertIn(expected, body)

        request = urllib.request.Request(f"{self.base_url}/v1/health")
        request.add_header("Authorization", "Bearer admin-secret")
        with (
            patch("host.runtime.admin_api.host_metrics", return_value={"cpu": {}, "memory": {}, "filesystem": {}, "swap": {}}),
            patch("host.runtime.admin_api.proxy_alive", return_value=True),
            patch("host.runtime.admin_api.version_status", return_value={"status": "ok", "runtime": "0.2.0", "state": "0.2.0"}),
            urllib.request.urlopen(request, timeout=5) as response,
        ):
            self.assertEqual(response.status, 200)
            self.assert_security_headers(response.headers)

    def assert_security_headers(self, headers: Any) -> None:
        self.assertEqual(headers["Content-Security-Policy"], admin_api.SECURITY_HEADERS["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_agent_file_routes_use_sudo_helper(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if command[-2] == "list":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"path": "/", "entries": [{"name": ".codex", "path": "/.codex", "type": "directory"}]}),
                    "",
                )
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({
                    "path": "/README.md",
                    "size_bytes": 12,
                    "truncated": False,
                    "encoding": "utf-8-replacement",
                    "content": "hello\n",
                }),
                "",
            )

        with patch("host.runtime.admin_api.subprocess.run", side_effect=fake_run):
            status, listed = self.request("GET", "/v1/agent-files?path=/")
            self.assertEqual(status, 200)
            self.assertEqual(listed["entries"][0]["name"], ".codex")

            status, read = self.request("GET", "/v1/agent-files/read?path=/README.md")
            self.assertEqual(status, 200)
            self.assertEqual(read["content"], "hello\n")

        self.assertEqual(calls[0], [
            "/usr/bin/sudo",
            "-n",
            "/usr/local/lib/trustyclaw-host/read-agent-file",
            "list",
            "/",
        ])
        self.assertEqual(calls[1][-2:], ["read", "/README.md"])

    def test_agent_file_content_streams_authenticated_video(self) -> None:
        payload = b"mock-video-bytes"
        process = MagicMock()
        process.stdout = io.BytesIO(
            json.dumps({
                "path": "/workspace/reel.mp4",
                "size_bytes": len(payload),
                "media_type": "video/mp4",
            }).encode() + b"\n" + payload
        )
        process.stderr = io.BytesIO()
        process.poll.return_value = 0
        process.wait.return_value = 0

        request = urllib.request.Request(
            f"{self.base_url}/v1/agent-files/content?path=%2Fworkspace%2Freel.mp4"
        )
        request.add_header("Authorization", "Bearer admin-secret")
        with (
            patch("host.runtime.admin_api.subprocess.Popen", return_value=process) as popen,
            urllib.request.urlopen(request, timeout=5) as response,
        ):
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "video/mp4")
            self.assertEqual(response.read(), payload)
            for name, value in admin_api.UNTRUSTED_FILE_SECURITY_HEADERS.items():
                self.assertEqual(response.headers[name], value)

        self.assertEqual(
            popen.call_args.args[0][-2:],
            ["stream", "/workspace/reel.mp4"],
        )

    def test_agent_file_content_rejects_non_video_helper_output(self) -> None:
        process = MagicMock()
        process.stdout = io.BytesIO(
            json.dumps({
                "path": "/workspace/payload.mp4",
                "size_bytes": 20,
                "media_type": "text/html",
            }).encode() + b"\n<script>bad()</script>"
        )
        process.stderr = io.BytesIO()
        process.poll.return_value = 0
        process.wait.return_value = 0

        with patch("host.runtime.admin_api.subprocess.Popen", return_value=process):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", "/v1/agent-files/content?path=%2Fworkspace%2Fpayload.mp4")

        self.assertEqual(error.exception.code, 500)
        self.assertIn("invalid metadata", error.exception.read().decode())

    def test_agent_file_content_rejects_non_video_path_before_helper(self) -> None:
        with patch("host.runtime.admin_api.subprocess.Popen") as popen:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", "/v1/agent-files/content?path=%2Fworkspace%2Fpayload.html")

        self.assertEqual(error.exception.code, 400)
        self.assertIn("only MP4 or MOV", error.exception.read().decode())
        popen.assert_not_called()

    def test_agent_file_helper_errors_map_to_http_status(self) -> None:
        def missing(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                command,
                2,
                json.dumps({"error": {"message": "path not found"}}),
                "",
            )

        with patch("host.runtime.admin_api.subprocess.run", side_effect=missing):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", "/v1/agent-files?path=/missing")
        self.assertEqual(error.exception.code, 404)
        self.assertIn("path not found", error.exception.read().decode())

    def test_agent_file_helper_permission_error_during_timeout_returns_504(self) -> None:
        with patch("host.runtime.admin_api.subprocess.run", side_effect=PermissionError("kill denied")):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", "/v1/agent-files?path=/")
        self.assertEqual(error.exception.code, 504)
        self.assertIn("root helper could not be terminated", error.exception.read().decode())

    def test_task_create_list_update_cancel_and_events(self) -> None:
        status, task = self.request("POST", "/v1/tasks", {"input_message": "first task", "thread_id": "t1", "agent_runtime": "codex"})
        self.assertEqual(status, 200)
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["agent_runtime"], "codex")
        task_id = task["task_id"]

        _, listed = self.request("GET", "/v1/tasks")
        self.assertEqual(listed["tasks"][0]["queue_position"], 1)
        self.assertEqual(listed["tasks"][0]["input_message"], "first task")
        self.assertEqual(listed["tasks"][0]["thread_id"], "t1")

        _, updated = self.request("PUT", f"/v1/tasks/{task_id}", {"input_message": "updated task"})
        self.assertEqual(updated["input_message"], "updated task")

        _, events = self.request("GET", f"/v1/tasks/{task_id}/events")
        self.assertEqual(events["events"], [])

        _, cancel = self.request("POST", f"/v1/tasks/{task_id}/cancel")
        self.assertEqual(cancel["status"], "accepted")
        _, cancelled = self.request("GET", f"/v1/tasks/{task_id}")
        self.assertEqual(cancelled["status"], "cancelled")
        _, events = self.request("GET", f"/v1/tasks/{task_id}/events")
        self.assertEqual([event["event_type"] for event in events["events"]], ["task.cancelled"])

    def test_task_create_validates_and_returns_session_options(self) -> None:
        status, codex = self.request(
            "POST",
            "/v1/tasks",
            {
                "input_message": "codex task",
                "thread_id": "codex-options",
                "agent_runtime": "codex",
                "model": "gpt-5.6-luna",
                "effort": "max",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual((codex["model"], codex["effort"]), ("gpt-5.6-luna", "max"))

        status, claude = self.request(
            "POST",
            "/v1/tasks",
            {
                "input_message": "claude task",
                "thread_id": "claude-options",
                "agent_runtime": "claude_code",
                "model": "fable",
                "effort": "ultracode",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual((claude["model"], claude["effort"]), ("fable", "ultracode"))

        invalid = [
            {"model": "gpt-5.6-luna", "effort": "ultra"},
            {"model": "opus", "effort": "high"},
            {"model": "gpt-5.6-terra"},
            {"effort": "high"},
            {"model": None, "effort": None},
        ]
        for index, fields in enumerate(invalid):
            with self.subTest(fields=fields), self.assertRaises(urllib.error.HTTPError) as error:
                self.request(
                    "POST",
                    "/v1/tasks",
                    {
                        "input_message": "invalid",
                        "thread_id": f"invalid-options-{index}",
                        "agent_runtime": "codex",
                        **fields,
                    },
                )
            self.assertEqual(error.exception.code, 400)

    def test_task_follow_up_accepts_omitted_or_matching_session_configuration(self) -> None:
        body = {
            "input_message": "first",
            "thread_id": "fixed-options",
            "agent_runtime": "codex",
            "model": "gpt-5.6-terra",
            "effort": "high",
        }
        self.request("POST", "/v1/tasks", body)

        _, repeated = self.request(
            "POST",
            "/v1/tasks",
            {**body, "input_message": "matching repeat"},
        )
        self.assertEqual(
            (repeated["agent_runtime"], repeated["model"], repeated["effort"]),
            (body["agent_runtime"], body["model"], body["effort"]),
        )

        for index, fields in enumerate(
            (
                {"model": "gpt-5.6-sol", "effort": "high"},
                {"model": "gpt-5.6-terra", "effort": "max"},
            )
        ):
            with self.subTest(fields=fields), self.assertRaises(urllib.error.HTTPError) as error:
                self.request(
                    "POST",
                    "/v1/tasks",
                    {**body, "input_message": "conflict", **fields},
            )
            self.assertEqual(error.exception.code, 400)
            self.assertIn("must match the existing thread configuration", error.exception.read().decode())

        with self.assertRaises(urllib.error.HTTPError) as partial_error:
            self.request(
                "POST",
                "/v1/tasks",
                {
                    "input_message": "partial conflict",
                    "thread_id": body["thread_id"],
                    "model": "gpt-5.6-terra",
                },
            )
        self.assertEqual(partial_error.exception.code, 400)
        self.assertIn(
            "must be provided together",
            partial_error.exception.read().decode(),
        )

        _, follow_up = self.request(
            "POST",
            "/v1/tasks",
            {"input_message": "follow up", "thread_id": body["thread_id"]},
        )
        self.assertEqual(
            (follow_up["agent_runtime"], follow_up["model"], follow_up["effort"]),
            (body["agent_runtime"], body["model"], body["effort"]),
        )

    def test_task_without_session_options_requires_an_existing_thread(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "POST",
                "/v1/tasks",
                {"input_message": "first", "thread_id": "unknown-options"},
            )

        self.assertEqual(error.exception.code, 400)
        self.assertIn("required when starting a new thread", error.exception.read().decode())

    def test_task_session_options_must_be_provided_together(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "POST",
                "/v1/tasks",
                {
                    "input_message": "first",
                    "thread_id": "partial-options",
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-terra",
                },
            )

        self.assertEqual(error.exception.code, 400)
        self.assertIn("must be provided together", error.exception.read().decode())

    def test_thread_list_combines_runtime_sessions_and_current_tasks(self) -> None:
        state = load_state()
        state["tasks"] = [
            {
                "task_id": "task_1",
                "status": "completed",
                "agent_runtime": "codex",
                "thread_id": "t1",
                "input_message": "done 1",
                "steer_messages": [],
                "created_at": "2026-06-08T00:00:00Z",
                "updated_at": "2026-06-08T00:00:01Z",
            },
            {
                "task_id": "task_2",
                "status": "queued",
                "agent_runtime": "codex",
                "thread_id": "t2",
                "input_message": "live",
                "steer_messages": [],
                "created_at": "2026-06-08T00:00:02Z",
                "updated_at": "2026-06-08T00:00:04Z",
            },
        ]
        state["codex_threads"] = {"t1": {"codex_thread_id": "codex-t1", "last_used_at": "2026-06-08T00:00:03Z"}}
        state["claude_sessions"] = {"t3": {"session_id": "claude-t3", "last_used_at": "2026-06-08T00:00:05Z"}}
        save_state(state)

        _, body = self.request("GET", "/v1/threads")

        self.assertEqual(
            [(thread["thread_id"], thread["agent_runtime"]) for thread in body["threads"]],
            [("t3", "claude_code"), ("t2", "codex"), ("t1", "codex")],
        )
        self.assertEqual(body["threads"][0]["task_count"], 0)
        self.assertEqual(body["threads"][1]["active_tasks"], [{"task_id": "task_2", "status": "queued"}])
        self.assertEqual(body["threads"][2]["last_used_at"], "2026-06-08T00:00:03Z")
        self.assertEqual(body["threads"][2]["task_count"], 1)
        self.assertNotIn("retained_task_count", body["threads"][2])

    def test_thread_task_list_returns_retained_tasks_for_selected_thread(self) -> None:
        state = load_state()
        state["tasks"] = [
            {
                "task_id": "task_1",
                "status": "completed",
                "agent_runtime": "codex",
                "thread_id": "shared",
                "input_message": "codex old",
                "steer_messages": [],
                "created_at": "2026-06-08T00:00:00Z",
                "updated_at": "2026-06-08T00:00:07Z",
            },
            {
                "task_id": "task_2",
                "status": "completed",
                "agent_runtime": "codex",
                "thread_id": "shared",
                "input_message": "codex new",
                "output_message": "done",
                "steer_messages": [],
                "created_at": "2026-06-08T00:00:02Z",
                "updated_at": "2026-06-08T00:00:03Z",
            },
            {
                "task_id": "task_3",
                "status": "failed",
                "agent_runtime": "codex",
                "thread_id": "other",
                "input_message": "other",
                "error_message": "failed",
                "steer_messages": [],
                "created_at": "2026-06-08T00:00:04Z",
                "updated_at": "2026-06-08T00:00:05Z",
            },
        ]
        save_state(state)

        _, body = self.request("GET", "/v1/threads/shared/tasks")
        self.assertEqual([task["task_id"] for task in body["tasks"]], ["task_1", "task_2"])
        self.assertEqual(body["tasks"][1]["output_message"], "done")

    def test_create_task_rejects_conflicting_configuration_for_existing_threads(self) -> None:
        state = load_state()
        state["tasks"] = [
            {
                "task_id": "task_1",
                "status": "completed",
                "agent_runtime": "codex",
                "thread_id": "used-by-task",
                "input_message": "done",
                "steer_messages": [],
                "created_at": "2026-06-08T00:00:00Z",
                "updated_at": "2026-06-08T00:00:01Z",
            }
        ]
        state["claude_sessions"] = {
            "used-by-session": {"session_id": "claude-session", "last_used_at": "2026-06-08T00:00:02Z"}
        }
        save_state(state)

        with self.assertRaises(urllib.error.HTTPError) as task_error:
            self.request(
                "POST",
                "/v1/tasks",
                {"input_message": "bad", "thread_id": "used-by-task", "agent_runtime": "claude_code"},
            )
        self.assertEqual(task_error.exception.code, 400)

        with self.assertRaises(urllib.error.HTTPError) as session_error:
            self.request(
                "POST",
                "/v1/tasks",
                {"input_message": "bad", "thread_id": "used-by-session", "agent_runtime": "codex"},
            )
        self.assertEqual(session_error.exception.code, 400)

        _, accepted = self.request(
            "POST",
            "/v1/tasks",
            {"input_message": "ok", "thread_id": "used-by-task"},
        )
        self.assertEqual(accepted["thread_id"], "used-by-task")

        with self.assertRaises(urllib.error.HTTPError) as old_route_error:
            self.request("GET", "/v1/tasks/finished")
        self.assertEqual(old_route_error.exception.code, 404)

    def test_task_event_history_can_be_paged_for_selected_task(self) -> None:
        with state.mutation() as cur:
            state.save_thread_session(
                cur,
                "codex",
                "t1",
                None,
                "2026-06-08T00:00:01Z",
                "gpt-5.6-terra",
                "high",
            )
            state.insert_task(
                cur,
                {
                    "task_id": "task_1",
                    "status": "completed",
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-terra",
                    "effort": "high",
                    "thread_id": "t1",
                    "input_message": "done",
                    "output_message": "ok",
                    "steer_messages": [],
                    "created_at": "2026-06-08T00:00:00Z",
                    "updated_at": "2026-06-08T00:00:01Z",
                },
            )
            state.append_agent_event(cur, "task.started", "task_1", {})
            state.append_agent_event(cur, "task.message", "task_1", {"message": "done", "source": "user"})
            state.append_agent_event(cur, "task.message", "task_1", {"message": "working", "source": "agent"})
            state.append_agent_event(cur, "task.message", "task_1", {"message": "ok", "source": "agent"})
            state.append_agent_event(cur, "task.completed", "task_1", {})

        _, first = self.request("GET", "/v1/tasks/task_1/events")
        self.assertEqual(len(first["events"]), 5)
        self.assertEqual([event["event_type"] for event in first["events"]], [
            "task.started",
            "task.message",
            "task.message",
            "task.message",
            "task.completed",
        ])
        _, second = self.request("GET", f"/v1/tasks/task_1/events?since={first['events'][-1]['seq']}")
        self.assertEqual(second["events"], [])

    def test_admin_ui_has_thread_task_event_smoke_path(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime"
        html = (runtime / "admin_ui.html").read_text()
        ui = "\n".join(
            path.read_text()
            for path in [runtime / "admin_ui.html", runtime / "admin_ui.css",
                         *sorted((runtime / "admin_ui").glob("*.js"))]
        )
        api = (runtime / "admin_api.py").read_text()
        self.assertIn("<h2>Sessions</h2>", html)
        self.assertIn('<link rel="stylesheet" href="/admin_ui.css">', html)
        self.assertIn('<link rel="icon" type="image/svg+xml" href="/favicon.svg">', html)
        self.assertIn('<script type="module" src="/admin_ui/app.js"></script>', html)
        self.assertIn("Cache-Control", api)
        self.assertIn("no-store, max-age=0", api)
        self.assertIn('<img class="brand-mark" width="30" height="30" src="/favicon.svg" alt="">', html)
        self.assertIn('<img class="login-mark" width="44" height="44" src="/favicon.svg" alt="">', html)
        self.assertIn("admin_favicon.svg", api)
        self.assertEqual(html.count('<svg width="19" height="19" viewBox="0 0 20 20"'), 9)
        self.assertIn('id="tab-processes"', html)
        self.assertIn("/v1/agent-processes", ui)
        self.assertIn("refreshAgentProcesses", ui)
        self.assertIn(".tab-button svg { display: block; height: 19px; width: 19px; }", ui)
        self.assertIn('`Host: ${health.agent_name}`', ui)
        self.assertNotIn("animation: panel-in", ui)
        self.assertIn("refreshVisibleTab(name).catch(() => {})", ui)
        self.assertIn('if (name === "agent-log")', ui)
        self.assertIn('await agentLog.showFirstPage();', ui)
        self.assertIn('if (name === "net-log")', ui)
        self.assertIn('await netLog.showFirstPage();', ui)
        self.assertIn('data-action="toggle-net-denied"', html)
        self.assertIn('id="net-event-pager"', html)
        self.assertIn('id="agent-event-pager"', html)
        self.assertIn('"net-page": () => netLog.showPage(button.dataset.page)', ui)
        self.assertIn('"agent-page": () => agentLog.showPage(button.dataset.page)', ui)
        self.assertIn("createPagedLog", ui)
        self.assertIn("EVENT_PAGER_WINDOW", ui)
        self.assertIn("formatNetworkReason", ui)
        self.assertIn("async function refreshOrSkip(work)", ui)
        self.assertIn('location.protocol === "https:" ? "; secure" : ""', ui)
        self.assertIn("adminCookieAttributes(2592000)", ui)
        self.assertIn("adminCookieAttributes(0)", ui)
        self.assertIn("Memory", ui)
        self.assertIn("Admin volume", ui)
        self.assertIn("Agent volume", ui)
        self.assertIn("filesystemMountTile", ui)
        self.assertIn("memorySwapTile", ui)
        self.assertIn('data-action="refresh-provider-usage"', ui)
        self.assertIn('id="runtime-overview"', html)
        self.assertNotIn("Agent runtimes", html)
        self.assertNotIn("Provider usage", html)
        self.assertIn("usageRing", ui)
        self.assertIn("/v1/agent-runtime/refresh", ui)
        self.assertIn("/v1/threads", ui)
        self.assertIn("/v1/threads/${encodeURIComponent(selectedThreadId)}/tasks", ui)
        self.assertIn("/v1/tasks/${encodeURIComponent(taskId)}/events", ui)
        self.assertIn("thread.task_count", ui)
        self.assertIn("TASK_EVENT_PAGE_BATCH", ui)
        self.assertIn("loadTaskEventBatch", ui)
        self.assertIn("loadMoreTaskEvents", ui)
        self.assertIn("refreshTaskEvents", ui)
        self.assertIn("task-events-inline", ui)
        self.assertIn("expandedTaskEvents", ui)
        self.assertIn("renderThreadHistory", ui)
        self.assertIn("function taskEventsHtml(task, eventState)", ui)
        self.assertIn("Agent session log", html)
        self.assertNotIn("Agent thread log", html)
        self.assertLess(html.index("Agent workspace"), html.index("Agent session log"))
        self.assertLess(html.index("Agent session log"), html.index("Agent audit log"))
        self.assertIn('data-action="show-thread"', ui)
        self.assertIn('data-action="show-task-events"', ui)
        self.assertNotIn('data-action="refresh-task"', ui)
        self.assertNotIn('data-action="refresh-task-events"', ui)
        self.assertNotIn('data-action="new-thread"', ui)
        self.assertNotIn('data-action="create-task"', ui)
        self.assertNotIn('data-action="steer-task"', ui)
        self.assertNotIn('data-action="cancel-task"', ui)
        self.assertNotIn('data-action="kill-task"', ui)
        self.assertNotIn('id="new-task"', html)
        self.assertNotIn('id="composer-target"', html)
        self.assertNotIn('$("new-task-thread").value = selectedThreadId', ui)
        self.assertNotIn('$("new-task-runtime").value = selectedThreadRuntime', ui)
        self.assertIn('button[data-action]', ui)
        self.assertNotIn("onclick=", ui)
        self.assertNotIn("oninput=", ui)
        self.assertIn('id="ai-inference-integrations"', html)
        self.assertIn('id="tools"', html)
        self.assertLess(html.index('id="ai-inference-heading"'), html.index('id="tools-heading"'))
        self.assertLess(html.index('id="tools-heading"'), html.index('id="manual-heading"'))
        self.assertIn('id="github-expansion"', html)
        self.assertIn('id="github-repos"', html)
        self.assertIn('id="domain-rules"', html)
        self.assertIn('id="github-repo"', html)
        self.assertIn('data-action="enable-github-require-approval"', html)
        self.assertIn('data-action="disable-github-require-approval"', html)
        self.assertIn('id="github-pending-pushes"', html)
        self.assertIn('id="github-token"', html)
        self.assertIn('id="github-credential-status"', html)
        self.assertIn('id="github-credential-form-label"', html)
        self.assertIn('data-action="add-github-repo"', html)
        self.assertIn('data-action="set-github-credential"', html)
        self.assertIn('data-action="delete-github-credential"', html)
        self.assertIn('data-action="add-domain-rule"', html)
        self.assertIn('data-action="recheck-github-audit"', html)
        self.assertIn("renderGithubAudit", ui)
        self.assertIn("recheckGithubAudit", ui)
        self.assertIn("refreshPendingGithubPushes", ui)
        self.assertIn('activeTab === "network"', ui)
        self.assertIn("audit-banner", ui)
        self.assertIn("repoAuditSummary", ui)
        self.assertIn('data-action="toggle-github-repo-audit"', ui)
        self.assertIn("/v1/network-tools/github-audit", ui)
        self.assertIn("toggleIntegrationInfo", ui)
        self.assertIn("closeIntegrationInfo", ui)
        self.assertIn("positionIntegrationInfo", ui)
        self.assertIn("renderIntegrationInfo", ui)
        self.assertIn('id="preset-info-popover"', html)
        self.assertIn('role="dialog"', html)
        # Per-integration rows publish immediately; there is no proposal state.
        self.assertIn("MANAGED_INTEGRATIONS", ui)
        self.assertIn("integration_catalog.js", ui)
        self.assertIn("publishPolicy", ui)
        self.assertIn("setIntegrationEnabled", ui)
        self.assertIn('data-action="enable-integration"', ui)
        self.assertIn('data-action="disable-integration"', ui)
        self.assertIn('data-action="remove-github-repo"', ui)
        self.assertIn('data-action="remove-domain-rule"', ui)
        self.assertIn("renderManagedIntegrations", ui)
        self.assertIn("renderGithubRepos", ui)
        self.assertIn("renderDomainRules", ui)
        self.assertIn("objectValue", ui)
        self.assertIn("!Array.isArray(value)", ui)
        self.assertNotIn("proposedNetworkPolicy", ui)
        self.assertNotIn("POLICY_PRESETS", ui)
        self.assertNotIn("applyPolicyPreset", ui)
        self.assertNotIn("Proposed policy", ui)
        self.assertNotIn("Managed integrations", html)
        self.assertNotIn("Curated access bundles", html)
        self.assertIn('<section class="integration-row"', ui)
        # Popovers stay high-level and link to the complete guide.
        self.assertIn("Protections", ui)
        self.assertIn("View integration guide", ui)
        self.assertIn("Authenticated traffic for another account is denied", ui)
        self.assertIn("writes work only for the repositories you configure", ui)
        self.assertNotIn("iconTile", ui)
        self.assertNotIn('class="icon-tile"', html)
        self.assertIn('data-provider-status="${esc(name)}"', ui)
        self.assertIn('connected: <span class="chip-label">${esc(identity)}</span>', ui)
        self.assertIn('Start ${esc(runtimeLabel)} login', ui)
        self.assertIn('>Disconnect</button>', ui)
        self.assertIn('aria-haspopup="dialog"', ui)
        self.assertIn('<h2>${esc(meta.label)}</h2>', ui)
        self.assertIn('data-action="toggle-integration-expansion"', ui)
        self.assertIn("toggleCustomDomainAccess", ui)
        self.assertIn("Custom Domain Access", html)
        self.assertIn('class="status disabled">0 domains enabled', html)
        self.assertIn('id="domain-rule-count"', html)
        self.assertIn('id="custom-domain-details"', html)
        self.assertIn('data-action="toggle-custom-domain-access"', html)
        self.assertIn("api.openai.com", ui)
        self.assertIn("pinned-account and external-URL request guards", ui)
        self.assertIn("auth.openai.com", ui)
        self.assertIn("GET and POST", ui)
        self.assertIn("api.anthropic.com", ui)
        self.assertIn("pinned-account, OAuth-token, and server-side web-tool guards", ui)
        self.assertIn("api.github.com", ui)
        self.assertIn("GraphQL denied", ui)
        self.assertIn("LFS uploads denied", ui)
        self.assertIn("pypi.org", ui)
        self.assertIn("GET and HEAD only under /simple and /pypi/<package>/json", ui)
        self.assertIn("registry.npmjs.org", ui)
        self.assertIn("Add domain rule", html)
        self.assertIn("Path guards (optional)", html)
        for domain in (
            "github.com",
            "api.github.com",
            "uploads.github.com",
            "codeload.github.com",
            "objects.githubusercontent.com",
            "github-cloud.githubusercontent.com",
            "raw.githubusercontent.com",
            "release-assets.githubusercontent.com",
            "pypi.org",
            "files.pythonhosted.org",
            "nodejs.org",
            "registry.npmjs.org",
        ):
            self.assertIn(domain, ui)
        self.assertIn("addDomainRule", ui)
        self.assertIn('id="tab-connection-guide"', html)
        self.assertIn('id="panel-connection-guide"', html)
        self.assertIn('id="connection-guide-index"', html)
        self.assertIn('id="connection-guide-select"', html)
        self.assertIn("connection-guide-open", ui)
        self.assertIn("overflow-y: auto", ui)
        self.assertNotIn('id="policy-message"', html)
        self.assertNotIn('id="tools-message"', html)
        self.assertIn('data-integration-message="${esc(name)}"', ui)
        self.assertIn('data-tool-message="${esc(tool.tool_id)}"', ui)
        self.assertIn("refreshConnectionGuide", ui)
        self.assertIn('data-action="open-connection-guide"', ui)
        self.assertIn("guide-protections", ui)
        self.assertIn("Exact network boundary", ui)
        self.assertIn("guide-assets", api)
        self.assertIn("removeDomainRule", ui)
        self.assertNotIn("loadAllTaskEvents", ui)
        self.assertNotIn("/v1/tasks/finished", ui)
        self.assertNotIn("loadFinishedTasks", ui)
        self.assertNotIn("retained_task_count", ui)
        self.assertNotIn("ssh_port_opened", ui)

    def test_task_create_requires_valid_agent_runtime(self) -> None:
        for index, body in enumerate(
            (
                {"input_message": "hello", "thread_id": "t1"},
                {"input_message": "hello", "thread_id": "t1", "agent_runtime": "bad"},
            )
        ):
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as error:
                self.request("POST", "/v1/tasks", body)
            self.assertEqual(error.exception.code, 400)

        _, body = self.request(
            "POST",
            "/v1/tasks",
            {"input_message": "hello", "thread_id": "t1", "agent_runtime": "claude_code"},
        )
        self.assertEqual(body["agent_runtime"], "claude_code")

    def test_network_policy_replace_and_events(self) -> None:
        body = {
            "network_integrations": {
                "openai": {"enabled": True},
                "custom": {"domains": {"api.example.com": {"allow_http_methods": ["GET"], "path_guards": ["^/v1$"]}}},
            },
        }
        _, response = self.request("PUT", "/v1/network/policy", body)

        custom = response["network_controls"]["network_integrations"]["custom"]["domains"]
        self.assertEqual(custom["api.example.com"]["allow_http_methods"], ["GET"])
        # The stored policy keeps the operator-facing shape. Provider hosts are
        # owned by their integration, never listed as custom domains.
        self.assertNotIn("api.openai.com", custom)
        stored = load_policy()
        self.assertEqual(stored, response["network_controls"])
        self.assertNotIn("openai", stored["network_integrations"]["custom"]["domains"])
        self.mock_reconcile.assert_called_once()
        _, current = self.request("GET", "/v1/network/policy")
        self.assertEqual(current["network_controls"], response["network_controls"])

    def test_network_policy_rejects_ssh_port_field(self) -> None:
        body = {"ssh_port_opened": False, "network_integrations": {"openai": {"enabled": True}}}
        with patch("host.runtime.admin_api.subprocess.run") as run:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("PUT", "/v1/network/policy", body)
        self.assertEqual(error.exception.code, 400)
        run.assert_not_called()

    def test_network_policy_replace_succeeds_when_existing_policy_is_error(self) -> None:
        save_policy({"bogus": True}, "2026-06-08T00:00:01Z")
        body = {"network_integrations": {"openai": {"enabled": True}}}
        status, _ = self.request("PUT", "/v1/network/policy", body)
        self.assertEqual(status, 200)
        self.assertEqual(load_policy()["network_integrations"], {"openai": {"enabled": True}})

    def _enable_github_policy(self) -> None:
        save_policy(
            {
                "network_integrations": {
                    "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "trustyclaw"}]}
                },
            },
            "2026-06-08T00:00:01Z",
        )

    def test_github_pending_push_approve_and_reject(self) -> None:
        state.save_proxy_github_token("ghs_working")
        state.enqueue_pending_push(
            "aa11bb22", "infiloop2", "trustyclaw",
            [{"old": "0" * 40, "new": "1" * 40, "ref": "refs/heads/main"}], [".github/workflows/ci.yml"],
        )
        state.enqueue_pending_push(
            "cc33dd44", "infiloop2", "trustyclaw",
            [{"old": "0" * 40, "new": "2" * 40, "ref": "refs/heads/feat"}], [".github/dependabot.yml"],
        )
        status, listing = self.request("GET", "/v1/network-tools/github-pending-pushes")
        self.assertEqual(status, 200)
        self.assertEqual({p["id"] for p in listing["pending_pushes"]}, {"aa11bb22", "cc33dd44"})

        # Approve invokes the replay helper with the working token, then marks
        # the row approved.
        calls: list[dict] = []
        timeouts: list[int | None] = []

        def fake_helper(command, payload, *, timeout=None):  # type: ignore[no-untyped-def]
            calls.append(payload)
            timeouts.append(timeout)
            return {"ok": True}

        with patch("host.network_integrations.github.push_gate.pending._run_helper_json", side_effect=fake_helper):
            status, approved = self.request(
                "POST", "/v1/network-tools/github-pending-pushes/aa11bb22/approve", {}
            )
        self.assertEqual(status, 200)
        self.assertEqual(approved["pending_push"]["status"], "approved")
        self.assertEqual(calls[0]["action"], "approve")
        self.assertEqual(calls[0]["token"], "ghs_working")
        self.assertEqual(calls[0]["ref_updates"][0]["ref"], "refs/heads/main")
        self.assertEqual(timeouts[0], github_pending_push.APPROVE_HELPER_TIMEOUT_SECONDS)

        # Reject invokes the helper in cleanup mode, then marks the row.
        with patch("host.network_integrations.github.push_gate.pending._run_helper_json", side_effect=fake_helper):
            _, rejected = self.request(
                "POST", "/v1/network-tools/github-pending-pushes/cc33dd44/reject", {}
            )
        self.assertEqual(rejected["pending_push"]["status"], "rejected")
        self.assertEqual(calls[1]["action"], "cleanup")
        self.assertNotIn("token", calls[1])
        self.assertNotIn("ref_updates", calls[1])
        self.assertEqual(timeouts[1], github_pending_push.APPROVE_HELPER_TIMEOUT_SECONDS)

        state.enqueue_pending_push(
            "dd55ee66", "infiloop2", "trustyclaw",
            [{"old": "0" * 40, "new": "3" * 40, "ref": "refs/heads/rejected"}], [".github/workflows/fail.yml"],
        )
        failure_calls: list[dict] = []

        def fail_approve_then_cleanup(command, payload, *, timeout=None):  # type: ignore[no-untyped-def]
            failure_calls.append(payload)
            self.assertEqual(timeout, github_pending_push.APPROVE_HELPER_TIMEOUT_SECONDS)
            if payload["action"] == "approve":
                raise github_credential.HelperError("lease rejected")
            return {"ok": True}

        with patch("host.network_integrations.github.push_gate.pending._run_helper_json", side_effect=fail_approve_then_cleanup):
            with self.assertRaises(urllib.error.HTTPError) as failed:
                self.request("POST", "/v1/network-tools/github-pending-pushes/dd55ee66/approve", {})
        self.assertEqual(failed.exception.code, 409)
        failed_row = state.get_pending_push("dd55ee66")
        self.assertIsNotNone(failed_row)
        assert failed_row is not None
        self.assertEqual(failed_row["status"], "failed")
        self.assertEqual([call["action"] for call in failure_calls], ["approve", "cleanup"])
        self.assertNotIn("token", failure_calls[1])

        state.save_proxy_github_token(None)
        state.enqueue_pending_push(
            "0badcafe", "infiloop2", "trustyclaw",
            [{"old": "0" * 40, "new": "8" * 40, "ref": "refs/heads/no-token"}],
            [".github/workflows/no-token.yml"],
        )
        # Approving with no working token resolves the row exactly once: the
        # replay never runs (no token to push with), so the push fails
        # terminally and only the quarantine refs are torn down. Recovery is a
        # fresh push once the credential is fixed.
        with patch("host.network_integrations.github.push_gate.pending._run_helper_json") as no_token_run:
            with self.assertRaises(urllib.error.HTTPError) as no_token:
                self.request("POST", "/v1/network-tools/github-pending-pushes/0badcafe/approve", {})
        self.assertEqual(no_token.exception.code, 409)
        self.assertEqual([call.args[1]["action"] for call in no_token_run.call_args_list], ["cleanup"])
        self.assertNotIn("token", no_token_run.call_args_list[0].args[1])
        no_token_row = state.get_pending_push("0badcafe")
        self.assertIsNotNone(no_token_row)
        assert no_token_row is not None
        self.assertEqual(no_token_row["status"], "failed")
        state.save_proxy_github_token("ghs_working")

        state.enqueue_pending_push(
            "ff99aa00", "infiloop2", "trustyclaw",
            [{"old": "0" * 40, "new": "5" * 40, "ref": "refs/heads/cleanup-lock"}],
            [".github/workflows/cleanup.yml"],
        )
        cleanup_calls: list[dict] = []

        def fail_cleanup(command, payload, *, timeout=None):  # type: ignore[no-untyped-def]
            cleanup_calls.append(payload)
            raise github_credential.HelperError("stale lock")

        with patch("host.network_integrations.github.push_gate.pending._run_helper_json", side_effect=fail_cleanup):
            # Rejecting means the push never leaves the box: a failed ref
            # cleanup (best-effort housekeeping) does not change the outcome.
            status, rejected_with_bad_cleanup = self.request(
                "POST", "/v1/network-tools/github-pending-pushes/ff99aa00/reject", {}
            )
        self.assertEqual(status, 200)
        self.assertEqual(rejected_with_bad_cleanup["pending_push"]["status"], "rejected")
        self.assertEqual([call["action"] for call in cleanup_calls], ["cleanup"])

        # A missing id is 404; an already-resolved one is 409.
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.request("POST", "/v1/network-tools/github-pending-pushes/deadbeef/approve", {})
        self.assertEqual(missing.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as resolved:
            self.request("POST", "/v1/network-tools/github-pending-pushes/cc33dd44/reject", {})
        self.assertEqual(resolved.exception.code, 409)

        state.enqueue_pending_push(
            "ee77ff88", "infiloop2", "trustyclaw",
            [{"old": "0" * 40, "new": "4" * 40, "ref": "refs/heads/racing"}], [".github/workflows/race.yml"],
        )
        # A resolution racing another one gets a crisp conflict: resolutions
        # serialize on RESOLVE_LOCK with a bounded wait.
        self.assertTrue(github_pending_push.RESOLVE_LOCK.acquire(timeout=1))
        self.addCleanup(github_pending_push.RESOLVE_LOCK.release)
        with patch.object(github_pending_push, "RESOLVE_LOCK_TIMEOUT_SECONDS", 0.05):
            with patch("host.network_integrations.github.push_gate.pending._run_helper_json") as run:
                with self.assertRaises(urllib.error.HTTPError) as racing:
                    self.request("POST", "/v1/network-tools/github-pending-pushes/ee77ff88/reject", {})
        self.assertEqual(racing.exception.code, 409)
        run.assert_not_called()

    def test_github_credential_pat_round_trip_publishes_working_token(self) -> None:
        self._enable_github_policy()
        _, empty = self.request("GET", "/v1/network-tools/github-credential")
        self.assertFalse(empty["configured"])
        self.assertEqual(empty["repository_audits"][0]["owner"], "infiloop2")
        self.assertEqual(empty["repository_audits"][0]["repo"], "trustyclaw")
        self.assertEqual(empty["repository_audits"][0]["warnings"][0]["code"], "repository_audit_incomplete")
        self.assertIn("repository audit has not run yet", empty["repository_audits"][0]["warnings"][0]["message"])

        _, saved = self.request(
            "PUT",
            "/v1/network-tools/github-credential",
            {"mode": "pat", "token": "github_pat_test"},
        )
        self.assertTrue(saved["configured"])
        self.assertEqual(saved["mode"], "pat")
        self.assertNotIn("github_pat_test", json.dumps(saved))
        self.assertEqual(state.read_proxy_github_token(), "github_pat_test")

        _, loaded = self.request("GET", "/v1/network-tools/github-credential")
        self.assertTrue(loaded["configured"])
        self.assertNotIn("github_pat_test", json.dumps(loaded))

        _, deleted = self.request("DELETE", "/v1/network-tools/github-credential")
        self.assertFalse(deleted["configured"])
        self.assertEqual(deleted["repository_audits"][0]["warnings"][0]["code"], "repository_audit_incomplete")
        self.assertIsNone(state.read_proxy_github_token())

    def test_github_repo_audit_without_credential_is_returned_as_warning(self) -> None:
        status, _ = self.request(
            "PUT",
            "/v1/network/policy",
            {
                "network_integrations": {
                    "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "trustyclaw"}]}
                },
            },
        )
        self.assertEqual(status, 200)

        _, metadata = self.request("GET", "/v1/network-tools/github-credential")
        self.assertFalse(metadata["configured"])
        warning = metadata["repository_audits"][0]["warnings"][0]
        self.assertEqual(warning["code"], "repository_audit_incomplete")
        self.assertEqual(warning["severity"], "warning")
        self.assertIn("no credential token to audit with", warning["message"])

    def test_github_credential_app_mode_mints_and_publishes(self) -> None:
        self._enable_github_policy()
        mints: list[int] = []

        def fake_helper(command, payload):  # type: ignore[no-untyped-def]
            if command is github_credential.MINT_COMMAND:
                mints.append(1)
                self.assertEqual(payload["app_id"], "12345")
                self.assertEqual(payload["installation_id"], "67890")
                # Installation-wide: the mint request carries no repositories.
                self.assertNotIn("repositories", payload)
                return {"token": f"ghs_minted_{len(mints)}", "expires_at": "2999-01-01T00:00:00Z"}
            raise AssertionError(f"unexpected helper call: {command}")

        with patch("host.runtime.github_credential._run_helper_json", side_effect=fake_helper):
            _, saved = self.request(
                "PUT",
                "/v1/network-tools/github-credential",
                {
                    "mode": "app",
                    "app_id": "12345",
                    "installation_id": "67890",
                    "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                },
            )
            # A fresh token is reused, not re-minted, by plain convergences
            # (the poller path).
            github_credential.reconcile()
            self.assertEqual(len(mints), 1)
            # A policy publish that changes the write list force-mints even
            # though the cached token is fresh: an installation token only
            # covers repositories granted at mint time, so it must postdate the
            # write-repository list.
            self.request(
                "PUT",
                "/v1/network/policy",
                {
                    "network_integrations": {
                        "github": {
                            "enabled": True,
                            "write_repositories": [
                                {"owner": "infiloop2", "repo": "trustyclaw"},
                                {"owner": "infiloop2", "repo": "infibot"},
                            ],
                        }
                    },
                },
            )
        self.assertEqual(len(mints), 2)
        self.assertEqual(saved["mode"], "app")
        self.assertEqual(saved["app_id"], "12345")
        self.assertEqual(saved["app_token_expires_at"], "2999-01-01T00:00:00Z")
        self.assertEqual(saved["validation"]["status"], "ok")
        self.assertNotIn("ghs_minted_1", json.dumps({k: v for k, v in saved.items()}))
        self.assertNotIn("BEGIN RSA", json.dumps(saved))
        # The repository-list publish left the re-minted token published.
        self.assertEqual(state.read_proxy_github_token(), "ghs_minted_2")

    def test_github_credential_app_mode_mint_failure_records_validation(self) -> None:
        self._enable_github_policy()
        # A failing mint keeps the credential configured and lands the
        # failure in the validation status instead of an HTTP error.
        with patch(
            "host.runtime.github_credential._run_helper_json",
            side_effect=github_credential.HelperError("mint upstream down"),
        ):
            _, saved = self.request(
                "PUT",
                "/v1/network-tools/github-credential",
                {
                    "mode": "app",
                    "app_id": "12345",
                    "installation_id": "67890",
                    "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                },
            )
        self.assertEqual(saved["mode"], "app")
        self.assertEqual(saved["validation"]["status"], "error")

    def test_mint_failure_fails_closed_and_recovers_on_retry(self) -> None:
        # A mint failure fails closed — the working token is withdrawn
        # and the error recorded — and the next poller reconcile converges
        # once the mint recovers. Deliberately simple: no fallback token.
        self._enable_github_policy()
        near = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state.save_proxy_github_token("ghs_previous", near)
        save_github_credential(
            {
                "mode": "app",
                "app_id": "12345",
                "installation_id": "67890",
                "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                "updated_at": "2026-06-08T00:00:00Z",
                "validation": {"status": "ok"},
            }
        )
        mint_up = {"up": False}

        def fake_helper(command, payload):  # type: ignore[no-untyped-def]
            if command is github_credential.MINT_COMMAND:
                if not mint_up["up"]:
                    raise github_credential.HelperError("mint upstream 503")
                return {"token": "ghs_recovered", "expires_at": "2999-01-01T00:00:00Z"}
            raise AssertionError(f"unexpected helper call: {command}")

        with patch("host.runtime.github_credential._run_helper_json", side_effect=fake_helper):
            github_credential.reconcile()
            self.assertIsNone(state.read_proxy_github_token())
            _, metadata = self.request("GET", "/v1/network-tools/github-credential")
            self.assertEqual(metadata["validation"]["status"], "error")
            mint_up["up"] = True
            github_credential.reconcile()
        self.assertEqual(state.read_proxy_github_token(), "ghs_recovered")
        _, healthy = self.request("GET", "/v1/network-tools/github-credential")
        self.assertEqual(healthy["validation"]["status"], "ok")

    def test_replacement_mint_failure_retires_the_previous_token(self) -> None:
        # Replacing an installed PAT with an App credential that cannot mint
        # (bad installation id, GitHub outage) must not leave the retired PAT
        # injectable: the credential it belonged to is gone.
        self._enable_github_policy()
        self.request(
            "PUT",
            "/v1/network-tools/github-credential",
            {"mode": "pat", "token": "github_pat_retired"},
        )
        self.assertEqual(state.read_proxy_github_token(), "github_pat_retired")

        def fake_helper(command, payload):  # type: ignore[no-untyped-def]
            if command is github_credential.MINT_COMMAND:
                raise github_credential.HelperError("mint upstream 503")
            raise AssertionError(f"unexpected helper call: {command}")

        with patch("host.runtime.github_credential._run_helper_json", side_effect=fake_helper):
            _, saved = self.request(
                "PUT",
                "/v1/network-tools/github-credential",
                {
                    "mode": "app",
                    "app_id": "12345",
                    "installation_id": "67890",
                    "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                },
            )
        # The new credential is stored with the mint failure recorded, and
        # the retired PAT is withdrawn.
        self.assertEqual(saved["mode"], "app")
        self.assertEqual(saved["validation"]["status"], "error")
        self.assertIsNone(state.read_proxy_github_token())

    def test_enabling_github_mints_a_fresh_token_even_with_a_fresh_published_token(self) -> None:
        # A publish that enables GitHub in App mode always mints fresh: the
        # published repository list may include repositories granted to the
        # installation after the current token was minted, so the installed
        # token must postdate the list — the comfortably fresh published
        # token is deliberately not kept.
        save_github_credential(
            {
                "mode": "app",
                "app_id": "12345",
                "installation_id": "67890",
                "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                "updated_at": "2026-06-08T00:00:00Z",
                "validation": {"status": "not_checked"},
            }
        )
        state.save_proxy_github_token("ghs_pre_grant", "2999-01-01T00:00:00Z")
        mints: list[int] = []

        def fake_helper(command, payload):  # type: ignore[no-untyped-def]
            if command is github_credential.MINT_COMMAND:
                mints.append(1)
                return {"token": "ghs_post_grant", "expires_at": "2999-01-01T00:00:00Z"}
            raise AssertionError(f"unexpected helper call: {command}")

        with patch("host.runtime.github_credential._run_helper_json", side_effect=fake_helper):
            status, _ = self.request(
                "PUT",
                "/v1/network/policy",
                {
                    "network_integrations": {
                        "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "just-granted"}]}
                    },
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(mints, [1])
        self.assertEqual(state.read_proxy_github_token(), "ghs_post_grant")
        # A publish that adds a write repository widens the scope and mints,
        # since an installation token only covers repositories granted at mint
        # time.
        with patch(
            "host.runtime.github_credential._run_helper_json",
            return_value={"token": "ghs_widened", "expires_at": "2999-01-01T00:00:00Z"},
        ):
            self.request(
                "PUT",
                "/v1/network/policy",
                {
                    "network_integrations": {
                        "openai": {"enabled": True},
                        "github": {
                            "enabled": True,
                            "write_repositories": [
                                {"owner": "infiloop2", "repo": "just-granted"},
                                {"owner": "infiloop2", "repo": "trustyclaw"},
                            ],
                        },
                    },
                },
            )
        self.assertEqual(state.read_proxy_github_token(), "ghs_widened")
        # Any GitHub-integration change mints fresh — removals included — one
        # simple rule instead of widening-only bookkeeping.
        with patch(
            "host.runtime.github_credential._run_helper_json",
            return_value={"token": "ghs_narrowed", "expires_at": "2999-01-01T00:00:00Z"},
        ):
            status, _ = self.request(
                "PUT",
                "/v1/network/policy",
                {
                    "network_integrations": {
                        "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "just-granted"}]}
                    },
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(state.read_proxy_github_token(), "ghs_narrowed")

        # A publish that does not touch the GitHub integration keeps the
        # healthy published token: no mint, so a transient mint outage cannot
        # break working access.
        with patch(
            "host.runtime.github_credential._run_helper_json",
            side_effect=AssertionError("a github-untouched publish must not mint"),
        ):
            status, _ = self.request(
                "PUT",
                "/v1/network/policy",
                {
                    "network_integrations": {
                        "openai": {"enabled": True},
                        "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "just-granted"}]},
                    },
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(state.read_proxy_github_token(), "ghs_narrowed")

    def test_enabling_github_with_a_failing_mint_publishes_and_fails_closed(self) -> None:
        # Enablement and credential health are separate concerns: a publish
        # that enables GitHub succeeds even when the App mint is down. The
        # credential fails closed — validation error recorded, no token file
        # installed (git/gh run unauthenticated) — and the next reconcile
        # (poller cycle) converges once the mint recovers.
        save_github_credential(
            {
                "mode": "app",
                "app_id": "12345",
                "installation_id": "67890",
                "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                "updated_at": "2026-06-08T00:00:00Z",
                "validation": {"status": "not_checked"},
            }
        )
        state.save_proxy_github_token("ghs_pre_grant", "2999-01-01T00:00:00Z")
        enabling_policy = {
            "network_integrations": {
                "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "just-granted"}]}
            },
        }
        mint_up = {"up": False}

        def fake_helper(command, payload):  # type: ignore[no-untyped-def]
            if command is github_credential.MINT_COMMAND:
                if not mint_up["up"]:
                    raise github_credential.HelperError("mint upstream 503")
                return {"token": "ghs_post_grant", "expires_at": "2999-01-01T00:00:00Z"}
            raise AssertionError(f"unexpected helper call: {command}")

        with patch("host.runtime.github_credential._run_helper_json", side_effect=fake_helper):
            status, _ = self.request("PUT", "/v1/network/policy", enabling_policy)
            self.assertEqual(status, 200)
            # Enabled, but failed closed: the previously published token is
            # withdrawn (it may not cover the published list), and the mint
            # error is visible in the validation status.
            self.assertTrue(load_policy()["network_integrations"]["github"]["enabled"])
            self.assertIsNone(state.read_proxy_github_token())
            _, metadata = self.request("GET", "/v1/network-tools/github-credential")
            self.assertEqual(metadata["validation"]["status"], "error")
            # Mint recovers: the next poller reconcile converges.
            mint_up["up"] = True
            github_credential.reconcile()
        self.assertEqual(state.read_proxy_github_token(), "ghs_post_grant")
        _, healthy = self.request("GET", "/v1/network-tools/github-credential")
        self.assertEqual(healthy["validation"]["status"], "ok")


    def test_enabling_read_only_github_app_publishes_working_token(self) -> None:
        save_github_credential(
            {
                "mode": "app",
                "app_id": "12345",
                "installation_id": "67890",
                "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                "updated_at": "2026-06-08T00:00:00Z",
                "validation": {"status": "not_checked"},
            }
        )

        def fake_helper(command, payload):  # type: ignore[no-untyped-def]
            if command is github_credential.MINT_COMMAND:
                return {"token": "ghs_read_only", "expires_at": "2999-01-01T00:00:00Z"}
            raise AssertionError(f"unexpected helper call: {command}")

        with patch("host.runtime.github_credential._run_helper_json", side_effect=fake_helper):
            status, _ = self.request(
                "PUT",
                "/v1/network/policy",
                {"network_integrations": {"github": {"enabled": True}}},
            )
        self.assertEqual(status, 200)
        self.assertEqual(state.read_proxy_github_token(), "ghs_read_only")


    def test_github_credential_removed_when_policy_disables_github(self) -> None:
        self._enable_github_policy()
        self.request(
            "PUT",
            "/v1/network-tools/github-credential",
            {"mode": "pat", "token": "github_pat_test"},
        )
        self.assertIsNotNone(state.read_proxy_github_token())
        status, _ = self.request(
            "PUT",
            "/v1/network/policy",
            {"network_integrations": {}},
        )
        self.assertEqual(status, 200)
        self.assertIsNone(state.read_proxy_github_token())

    def test_refresh_mid_mint_cannot_overwrite_a_concurrent_delete(self) -> None:
        self._enable_github_policy()
        mint_started = threading.Event()
        release_mint = threading.Event()

        def fake_helper(command, payload):  # type: ignore[no-untyped-def]
            if command is github_credential.MINT_COMMAND:
                mint_started.set()
                release_mint.wait(timeout=10)
                return {
                    "token": "ghs_raced",
                    "expires_at": "2999-01-01T00:00:00Z",
                }
            raise AssertionError(f"unexpected helper call: {command}")

        with patch("host.runtime.github_credential._run_helper_json", side_effect=fake_helper):
            # Seed an app credential whose token needs minting, then start a
            # refresh that blocks inside the mint helper.
            save_github_credential(
                {
                    "mode": "app",
                    "app_id": "12345",
                    "installation_id": "67890",
                    "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                    "updated_at": "2026-06-08T00:00:00Z",
                    "validation": {"status": "not_checked"},
                }
            )
            refresher = threading.Thread(target=github_credential.reconcile, daemon=True)
            refresher.start()
            self.assertTrue(mint_started.wait(timeout=10))
            # DELETE arrives while the mint is in flight; serialization makes
            # it wait for the refresh instead of interleaving with it.
            deleter_result: list[object] = []

            def run_delete() -> None:
                try:
                    deleter_result.append(self.request("DELETE", "/v1/network-tools/github-credential"))
                except Exception as exc:  # noqa: BLE001 - surfaced in assertions
                    deleter_result.append(exc)

            deleter = threading.Thread(target=run_delete, daemon=True)
            deleter.start()
            release_mint.set()
            refresher.join(timeout=15)
            deleter.join(timeout=15)
        self.assertFalse(refresher.is_alive())
        self.assertFalse(deleter.is_alive())
        # Whatever the interleaving, the end state is consistent: credential
        # gone and no working token left behind.
        _, cleared = self.request("GET", "/v1/network-tools/github-credential")
        self.assertFalse(cleared["configured"])
        self.assertEqual(cleared["repository_audits"][0]["warnings"][0]["code"], "repository_audit_incomplete")
        self.assertIsNone(state.read_proxy_github_token())

    def test_disabled_policy_after_crash_is_converged_by_the_poller(self) -> None:
        # Simulate a crash between committing a GitHub-disabled policy and
        # reconcile() running: the working token is still published and the
        # credential row still reads healthy (status ok).
        state.save_proxy_github_token("github_pat_leftover")
        self.assertIsNotNone(state.read_proxy_github_token())
        save_policy(
            {"network_integrations": {}},
            "2026-06-08T00:00:02Z",
        )
        save_github_credential(
            {
                "mode": "pat",
                "token": "github_pat_leftover",
                "updated_at": "2026-06-08T00:00:00Z",
                "validation": {"status": "ok"},
            }
        )
        # The poller must converge removal even though the status reads ok.
        github_credential.reconcile()
        self.assertIsNone(state.read_proxy_github_token())

    def test_github_credential_stages_while_disabled_and_installs_on_enable(self) -> None:
        # Storing the credential before enabling GitHub is the flow that
        # never leaves the proxy allowing repositories with no token: nothing
        # is published while disabled, and the enabling policy publish
        # publishes the staged token.
        _, saved = self.request(
            "PUT",
            "/v1/network-tools/github-credential",
            {"mode": "pat", "token": "github_pat_staged"},
        )
        self.assertTrue(saved["configured"])
        # Staging while disabled leaves the credential's own health untouched
        # (enablement is not a credential property); nothing is installed.
        self.assertEqual(saved["validation"]["status"], "not_checked")
        self.assertIsNone(state.read_proxy_github_token())
        _, loaded = self.request("GET", "/v1/network-tools/github-credential")
        self.assertTrue(loaded["configured"])
        status, _ = self.request(
            "PUT",
            "/v1/network/policy",
            {
                "network_integrations": {
                    "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "trustyclaw"}]}
                },
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(state.read_proxy_github_token(), "github_pat_staged")
        # Deleting while disabled works too: staging is fully symmetric.
        self.request(
            "PUT",
            "/v1/network/policy",
            {"network_integrations": {}},
        )
        self.assertIsNone(state.read_proxy_github_token())
        _, cleared = self.request("DELETE", "/v1/network-tools/github-credential")
        self.assertFalse(cleared["configured"])
        self.assertNotIn("repository_audits", cleared)

    def test_github_credential_rejects_malformed_bodies(self) -> None:
        self._enable_github_policy()
        for index, body in enumerate(
            (
                {},
                {"mode": "pat"},
                {"mode": "pat", "token": ""},
                {"mode": "pat", "token": "token with spaces"},
                {"mode": "pat", "token": "github_pat_test", "credential_id": "github-primary"},
                {"mode": "pat", "token": "github_pat_test", "extra": True},
                {"token": "github_pat_test"},
                {"mode": "app", "app_id": "12345", "installation_id": "67890"},
                {"mode": "app", "app_id": "abc", "installation_id": "67890", "private_key_pem": "-----BEGIN X-----"},
                {"mode": "app", "app_id": "12345", "installation_id": "67890", "private_key_pem": "not a key"},
            )
        ):
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as error:
                self.request("PUT", "/v1/network-tools/github-credential", body)
            self.assertEqual(error.exception.code, 400)

    def test_concurrent_network_policy_replacements_are_last_writer_wins(self) -> None:
        # No dedicated policy lock: the DB write is atomic under the mutation
        # lock, both requests succeed, and the stored policy is one of the two
        # submitted bodies (never a blend).
        enabled = {"network_integrations": {"openai": {"enabled": True}}}
        disabled = {"network_integrations": {"openai": {"enabled": False}}}
        results: list[dict[str, object]] = []
        threads = [
            threading.Thread(target=lambda body=body: results.append(admin_api.replace_network_policy(body)))
            for body in (enabled, disabled)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 2)
        self.assertIn(load_policy(), [parse_network_controls(body).to_json() for body in (enabled, disabled)])

    def test_reboot_helper_swallows_unkillable_timeout(self) -> None:
        # A timed-out helper may still reboot the host, so neither timeout shape
        # (nor the PermissionError an unkillable root child produces) is an error.
        for effect in (subprocess.TimeoutExpired(cmd="reboot-host", timeout=10), PermissionError("not permitted")):
            with patch("host.runtime.admin_api.subprocess.run", side_effect=effect):
                self.assertEqual(admin_api.reboot_host(), {"status": "accepted"})

    def test_reboot_helper_failure_returns_500(self) -> None:
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="sudo: not allowed")
        with patch("host.runtime.admin_api.subprocess.run", return_value=failed):
            with self.assertRaises(admin_api.ApiError) as error:
                admin_api.reboot_host()
        self.assertEqual(error.exception.status, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(error.exception.message, "sudo: not allowed")

    def test_task_queue_is_capped(self) -> None:
        # Queued tasks are never pruned, so the queue is the one task input
        # that could grow state.json without bound; creates beyond the cap 409.
        with patch.object(admin_api, "QUEUED_TASK_LIMIT", 2):
            self.request("POST", "/v1/tasks", {"input_message": "a", "thread_id": "q1", "agent_runtime": "codex"})
            self.request("POST", "/v1/tasks", {"input_message": "b", "thread_id": "q2", "agent_runtime": "codex"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("POST", "/v1/tasks", {"input_message": "c", "thread_id": "q3", "agent_runtime": "codex"})
            self.assertEqual(error.exception.code, 409)
            # Cancelling a queued task frees a slot.
            self.request("POST", "/v1/tasks/task_1/cancel")
            _, body = self.request("POST", "/v1/tasks", {"input_message": "c", "thread_id": "q3", "agent_runtime": "codex"})
        self.assertEqual(body["status"], "queued")

    def test_pending_steers_are_capped(self) -> None:
        state = load_state()
        state["tasks"] = [{
            "task_id": "task_1", "status": "running", "agent_runtime": "codex", "thread_id": "t1",
            "input_message": "x", "steer_messages": [],
            "created_at": "t", "updated_at": "t",
        }]
        save_state(state)
        with patch.object(admin_api, "PENDING_STEER_LIMIT", 2):
            self.request("POST", "/v1/tasks/task_1/steer", {"steer_message": "s1"})
            self.request("POST", "/v1/tasks/task_1/steer", {"steer_message": "s2"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("POST", "/v1/tasks/task_1/steer", {"steer_message": "s3"})
            self.assertEqual(error.exception.code, 409)
            # The queue drains as the worker delivers; a slot frees up.
            state = load_state()
            state["tasks"][0]["steer_messages"].pop(0)
            save_state(state)
            _, body = self.request("POST", "/v1/tasks/task_1/steer", {"steer_message": "s3"})
        self.assertEqual(body["status"], "accepted")
        self.assertEqual(load_state()["tasks"][0]["steer_messages"], ["s2", "s3"])

    def test_login_completion_clears_device_login_record(self) -> None:
        # Once the account goes active the device code is spent; keeping the
        # record would replay a dead code if the session later expires back to
        # awaiting_login.
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "awaiting_login"
        state["codex_oauth"] = {
            "status": "awaiting_login",
            "device_code": "X",
            "login_id": "l1",
            "login_url": "https://auth.openai.com/device",
            "expires_at": "2099-06-08T00:10:00Z",
        }
        save_state(state)
        with (
            patch(
                "host.runtime.orchestrator.codex_app_server.read_completed_device_login_account_id",
                return_value="acct_smoke",
            ),
            patch(
                "host.runtime.orchestrator.codex_app_server.account_status",
                return_value=("active", None, "acct_smoke"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "active")
        self.assertIsNone(load_state().get("codex_oauth"))
        self.assertEqual(read_openai_account().get("account_id"), "acct_smoke")
        self.assertEqual(read_proxy_openai_account_id(), "acct_smoke")

    def test_runtime_expiry_clears_openai_proxy_pin_only(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "active"
        save_state(state)
        save_approved_openai_account("acct_smoke")

        with patch(
            "host.runtime.orchestrator.codex_app_server.account_status",
            return_value=("awaiting_login", None, None),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(read_openai_account().get("account_id"), "acct_smoke")
        self.assertIsNone(read_proxy_openai_account_id())

    def test_runtime_expiry_clears_claude_proxy_pin_only(self) -> None:
        save_policy(
            {
                "network_integrations": {"claude": {"enabled": True}},
            },
            "2026-06-08T00:00:01Z",
        )
        state = load_state()
        state["agent_runtime_statuses"]["claude_code"]["status"] = "active"
        save_state(state)
        save_claude_account({"account_id": "acct_smoke", "access_token_sha256": "f" * 64})

        with patch(
            "host.runtime.orchestrator.claude_code.account_status",
            return_value=("awaiting_login", None, None),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "awaiting_login")

        self.assertEqual(read_claude_account(), {"account_id": "acct_smoke", "access_token_sha256": "f" * 64})
        self.assertEqual(read_proxy_claude_account(), {})

    def test_active_claude_runtime_refresh_repins_rotated_token(self) -> None:
        # The Claude CLI rotates its OAuth access token on its own schedule;
        # the bearer-token pin follows, because only the account identity is
        # anchored.
        save_policy(
            {
                "network_integrations": {"claude": {"enabled": True}},
            },
            "2026-06-08T00:00:01Z",
        )
        state = load_state()
        state["agent_runtime_statuses"]["claude_code"]["status"] = "active"
        save_state(state)
        save_attested_claude_account(
            "acct_smoke", organization_id="org_smoke", access_token_sha256="0" * 64
        )

        with (
            patch(
                "host.runtime.orchestrator.claude_code.account_status",
                return_value=(
                    "active",
                    None,
                    {"account_id": "acct_smoke", "organization_id": "org_smoke", "access_token_sha256": "1" * 64},
                ),
            ),
            patch(
                "host.runtime.orchestrator.claude_code.read_attested_identity",
                return_value={"access_token_sha256": "1" * 64, "account_uuid": "acct_smoke"},
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "active")

        self.assertEqual(read_claude_account()["access_token_sha256"], "1" * 64)
        self.assertEqual(read_proxy_claude_account()["access_token_sha256"], "1" * 64)
        self.assertEqual(read_proxy_claude_account()["account_id"], "acct_smoke")

    def test_active_claude_runtime_refresh_rejects_rotation_to_another_account(self) -> None:
        save_policy(
            {
                "network_integrations": {"claude": {"enabled": True}},
            },
            "2026-06-08T00:00:01Z",
        )
        state = load_state()
        state["agent_runtime_statuses"]["claude_code"]["status"] = "active"
        save_state(state)
        save_attested_claude_account("acct_operator", access_token_sha256="0" * 64)

        with (
            patch(
                "host.runtime.orchestrator.claude_code.account_status",
                return_value=(
                    "active",
                    None,
                    {"account_id": "acct_operator", "email": "operator@example.com", "access_token_sha256": "f" * 64},
                ),
            ),
            patch(
                "host.runtime.orchestrator.claude_code.read_attested_identity",
                return_value={"access_token_sha256": "f" * 64, "account_uuid": "acct_attacker"},
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "error")

        self.assertEqual(read_claude_account()["account_id"], "acct_operator")
        self.assertEqual(read_proxy_claude_account(), {})
        record = orchestrator.runtime_status_record("claude_code")
        self.assertIn("account changed", record["error_message"])

    def test_agent_accounts_keep_linked_identity_while_not_active(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "error"
        state["agent_runtime_statuses"]["claude_code"]["status"] = "awaiting_login"
        save_state(state)
        save_approved_openai_account("acct_smoke", email="codex@example.com", plan_type="pro")
        save_attested_claude_account("acct_claude", email="claude@example.com", access_token_sha256="0" * 64)

        _, body = self.request("GET", "/v1/agent-runtime/account")

        # The anchor identity stays visible while the runtime is not active;
        # plan and usage metadata are reported only for active runtimes.
        self.assertEqual(
            body,
            {
                "accounts": [
                    {
                        "agent_runtime": "codex",
                        "provider": "openai",
                        "status": "error",
                        "account_id": "acct_smoke",
                        "email": "codex@example.com",
                    },
                    {
                        "agent_runtime": "claude_code",
                        "provider": "claude",
                        "status": "awaiting_login",
                        "account_id": "acct_claude",
                        "email": "claude@example.com",
                    },
                ]
            },
        )

    def test_agent_accounts_hide_legacy_openai_identity_without_operator_approval(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "awaiting_login"
        save_state(state)
        save_openai_account({"account_id": "acct_legacy", "email": "legacy@example.com"})

        _, body = self.request("GET", "/v1/agent-runtime/account")

        self.assertEqual(body["accounts"][0], {"agent_runtime": "codex", "provider": "openai", "status": "awaiting_login"})

    def test_agent_accounts_hide_legacy_claude_identity_without_attestation(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["claude_code"]["status"] = "awaiting_login"
        save_state(state)
        save_claude_account(
            {"account_id": "acct_legacy", "email": "legacy@example.com", "access_token_sha256": "0" * 64}
        )

        _, body = self.request("GET", "/v1/agent-runtime/account")

        self.assertEqual(
            body["accounts"][1], {"agent_runtime": "claude_code", "provider": "claude", "status": "awaiting_login"}
        )

    def test_agent_accounts_return_both_runtime_statuses(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "active"
        state["agent_runtime_statuses"]["claude_code"]["status"] = "awaiting_login"
        save_state(state)
        save_approved_openai_account(
            "acct_smoke",
            email="codex@example.com",
            plan_type="pro",
            type="chatgpt",
            codex_usage={
                "last_checked_at": "2026-06-29T23:10:00Z",
                "rate_limits": {
                    "primary": {"used_percent": 8, "window_duration_mins": 300, "resets_at": 1782788897},
                    "secondary": {"used_percent": 11, "window_duration_mins": 10080, "resets_at": 1783296254},
                    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                },
            },
        )

        _, body = self.request("GET", "/v1/agent-runtime/account")

        self.assertEqual(
            body,
            {
                "accounts": [
                    {
                        "agent_runtime": "codex",
                        "provider": "openai",
                        "status": "active",
                        "account_id": "acct_smoke",
                        "email": "codex@example.com",
                        "plan_type": "pro",
                        "codex_usage": {
                            "last_checked_at": "2026-06-29T23:10:00Z",
                            "rate_limits": {
                                "primary": {
                                    "used_percent": 8,
                                    "window_duration_mins": 300,
                                    "resets_at": 1782788897,
                                },
                                "secondary": {
                                    "used_percent": 11,
                                    "window_duration_mins": 10080,
                                    "resets_at": 1783296254,
                                },
                                "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                            },
                        },
                    },
                    {"agent_runtime": "claude_code", "provider": "claude", "status": "awaiting_login"},
                ]
            },
        )

    def test_agent_accounts_expose_stored_codex_usage(self) -> None:
        # The runtime adapter sanitizes usage at capture and every active
        # refresh rewrites the row, so the API exposes the stored shape as is.
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "active"
        save_state(state)
        save_approved_openai_account(
            "acct_smoke",
            plan_type="pro",
            codex_usage={
                "last_checked_at": "2026-06-29T23:10:00Z",
                "rate_limits": {
                    "primary": {
                        "used_percent": 8,
                        "window_duration_mins": 300,
                        "resets_at": 1782788897,
                    },
                    "secondary": {
                        "used_percent": 11,
                        "window_duration_mins": 10080,
                        "resets_at": 1783296254,
                    },
                    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                },
            },
        )

        _, body = self.request("GET", "/v1/agent-runtime/account")

        self.assertEqual(
            body["accounts"][0],
            {
                "agent_runtime": "codex",
                "provider": "openai",
                "status": "active",
                "account_id": "acct_smoke",
                "plan_type": "pro",
                "codex_usage": {
                    "last_checked_at": "2026-06-29T23:10:00Z",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 8,
                            "window_duration_mins": 300,
                            "resets_at": 1782788897,
                        },
                        "secondary": {
                            "used_percent": 11,
                            "window_duration_mins": 10080,
                            "resets_at": 1783296254,
                        },
                        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                    },
                },
            },
        )

    def test_agent_accounts_return_active_claude_metadata(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "deactivated"
        state["agent_runtime_statuses"]["claude_code"]["status"] = "active"
        save_state(state)
        save_attested_claude_account(
            "acct_smoke",
            organization_id="org_smoke",
            email="smoke@example.com",
            plan_type="pro",
            claude_usage={
                "current_session_used_percent": 0,
                "current_session_resets_at": 1782781800,
                "weekly_used_percent": 0,
                "weekly_resets_at": 1783094340,
                "last_checked_at": "2026-06-29T23:10:00Z",
            },
            access_token_sha256="f" * 64,
        )

        _, body = self.request("GET", "/v1/agent-runtime/account")

        self.assertEqual(
            body,
            {
                "accounts": [
                    {"agent_runtime": "codex", "provider": "openai", "status": "deactivated"},
                    {
                        "agent_runtime": "claude_code",
                        "provider": "claude",
                        "status": "active",
                        "account_id": "acct_smoke",
                        "email": "smoke@example.com",
                        "plan_type": "pro",
                        "claude_usage": {
                            "current_session_used_percent": 0,
                            "current_session_resets_at": 1782781800,
                            "weekly_used_percent": 0,
                            "weekly_resets_at": 1783094340,
                            "last_checked_at": "2026-06-29T23:10:00Z",
                        },
                    },
                ]
            },
        )

    def test_agent_accounts_return_partial_claude_usage_metadata(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["claude_code"]["status"] = "active"
        save_state(state)
        save_attested_claude_account(
            "acct_smoke",
            claude_usage={
                "current_session_used_percent": 0,
                "weekly_used_percent": 0,
                "weekly_resets_at": 1783094340,
            },
            access_token_sha256="f" * 64,
        )

        _, body = self.request("GET", "/v1/agent-runtime/account")

        self.assertEqual(
            body["accounts"][1]["claude_usage"],
            {
                "current_session_used_percent": 0,
                "weekly_used_percent": 0,
                "weekly_resets_at": 1783094340,
            },
        )

    def test_agent_runtime_refresh_endpoint_refreshes_requested_runtime(self) -> None:
        with patch("host.runtime.admin_api.orchestrator.refresh_runtime_status") as refresh:
            _, body = self.request(
                "POST",
                "/v1/agent-runtime/refresh",
                {"agent_runtime": "claude_code"},
            )

        refresh.assert_called_once_with("claude_code", force_provider_probe=True)
        self.assertEqual([account["agent_runtime"] for account in body["accounts"]], ["codex", "claude_code"])

    def test_agent_runtime_refresh_endpoint_refreshes_all_runtimes_by_default(self) -> None:
        with patch("host.runtime.admin_api.orchestrator.refresh_runtime_status") as refresh:
            self.request("POST", "/v1/agent-runtime/refresh", {})

        self.assertEqual(
            [(call.args[0], call.kwargs) for call in refresh.call_args_list],
            [
                ("codex", {"force_provider_probe": True}),
                ("claude_code", {"force_provider_probe": True}),
            ],
        )

    def test_agent_runtime_refresh_endpoint_rejects_unknown_runtime(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "POST",
                "/v1/agent-runtime/refresh",
                {"agent_runtime": "bad"},
            )

        self.assertEqual(error.exception.code, HTTPStatus.BAD_REQUEST)

    def test_agent_account_endpoint_rejects_runtime_filter(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "active"
        save_state(state)
        save_approved_openai_account("acct_smoke")

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("GET", "/v1/agent-runtime/account?agent_runtime=codex")

        self.assertEqual(error.exception.code, HTTPStatus.BAD_REQUEST)

    def test_current_codex_oauth_login_rejects_active_runtime(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "active"
        save_state(state)

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("GET", "/v1/agent-runtime/codex-oauth-login")

        self.assertEqual(error.exception.code, 409)

    def test_oauth_start_rejects_disabled_provider_before_spawning_helper(self) -> None:
        save_policy({"network_integrations": {}}, "t")
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "awaiting_login"
        state["agent_runtime_statuses"]["claude_code"]["status"] = "awaiting_login"
        save_state(state)

        with (
            patch(
                "host.runtime.admin_api.codex_app_server.start_device_login",
                side_effect=AssertionError("disabled Codex provider must not spawn login helper"),
            ),
            patch(
                "host.runtime.admin_api.claude_code.start_oauth_login",
                side_effect=AssertionError("disabled Claude provider must not spawn login helper"),
            ),
        ):
            for path in ("/v1/agent-runtime/codex-oauth-login", "/v1/agent-runtime/claude-oauth-login"):
                with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                    self.request("POST", path)
                self.assertEqual(error.exception.code, 409)

    def test_current_oauth_rejects_disabled_provider_even_with_stale_oauth_state(self) -> None:
        save_policy({"network_integrations": {}}, "t")
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "awaiting_login"
        state["agent_runtime_statuses"]["claude_code"]["status"] = "awaiting_login"
        state["codex_oauth"] = {
            "status": "awaiting_login",
            "device_code": "CODE",
            "login_id": "login-1",
            "login_url": "https://auth.openai.com/device",
            "expires_at": "2099-06-08T00:10:00Z",
        }
        state["claude_oauth"] = {
            "status": "awaiting_code",
            "login_url": "https://claude.com/cai/oauth/authorize",
            "expires_at": "2099-06-08T00:10:00Z",
        }
        save_state(state)

        for path in ("/v1/agent-runtime/codex-oauth-login", "/v1/agent-runtime/claude-oauth-login"):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", path)
            self.assertEqual(error.exception.code, 409)

    def test_codex_oauth_start_closes_helper_if_provider_is_disabled_before_state_save(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "awaiting_login"
        save_state(state)
        login = admin_api.codex_app_server.CodexLogin(
            login_id="login-1",
            verification_url="https://example.com/device",
            user_code="CODE-1",
        )

        with (
            patch("host.runtime.admin_api.orchestrator.runtime_network_enabled", side_effect=[True, False]),
            patch("host.runtime.admin_api.codex_app_server.start_device_login", return_value=login),
            patch("host.runtime.admin_api.codex_app_server.close_login_server") as close_login,
            self.assertRaises(admin_api.ApiError) as error,
        ):
            admin_api.start_codex_oauth_login()

        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)
        close_login.assert_called_once()
        self.assertIsNone(load_state().get("codex_oauth"))

    def test_claude_oauth_complete_rejects_disabled_provider_before_touching_helper(self) -> None:
        save_policy({"network_integrations": {}}, "t")

        with (
            patch(
                "host.runtime.admin_api.claude_code.complete_oauth_login",
                side_effect=AssertionError("disabled Claude provider must not complete OAuth"),
            ),
            self.assertRaises(admin_api.ApiError) as error,
        ):
            admin_api.complete_claude_oauth_login({"code": "browser-code"})

        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)

    def test_claude_oauth_complete_keeps_pending_login_for_trusted_account_capture(self) -> None:
        save_policy(
            {
                "network_integrations": {"claude": {"enabled": True}},
            },
            "2026-06-08T00:00:00Z",
        )
        snapshot = load_state()
        snapshot["agent_runtime_statuses"]["claude_code"]["status"] = "awaiting_login"
        snapshot["claude_oauth"] = {
            "status": "awaiting_code",
            "login_url": "https://claude.com/cai/oauth/authorize",
            "expires_at": "2099-06-08T00:10:00Z",
        }
        save_state(snapshot)

        def refresh(runtime_type: str) -> str:
            self.assertEqual(runtime_type, "claude_code")
            oauth = load_state()["claude_oauth"]
            self.assertIsNotNone(oauth)
            self.assertEqual(oauth["status"], "completed")
            # The approval is bound to the token the login wrote: first
            # capture requires attesting this exact hash.
            self.assertEqual(oauth["access_token_sha256"], "a" * 64)
            with state.mutation() as cur:
                state.set_oauth_login(cur, "claude", None)
            return "active"

        with (
            patch("host.runtime.admin_api.claude_code.complete_oauth_login") as complete,
            patch(
                "host.runtime.admin_api.claude_code.read_claude_account",
                return_value={"access_token_sha256": "a" * 64},
            ),
            patch("host.runtime.admin_api.orchestrator.refresh_runtime_status", side_effect=refresh) as refresh_status,
        ):
            self.assertEqual(admin_api.complete_claude_oauth_login({"code": "browser-code"}), {"status": "accepted"})

        complete.assert_called_once_with("browser-code")
        refresh_status.assert_called_once_with("claude_code")
        self.assertIsNone(load_state()["claude_oauth"])

    def test_claude_oauth_complete_clears_pending_login_after_non_active_refresh(self) -> None:
        save_policy(
            {"network_integrations": {"claude": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        snapshot = load_state()
        snapshot["agent_runtime_statuses"]["claude_code"]["status"] = "awaiting_login"
        snapshot["claude_oauth"] = {
            "status": "awaiting_code",
            "login_url": "https://claude.com/cai/oauth/authorize",
            "expires_at": "2099-06-08T00:10:00Z",
        }
        save_state(snapshot)

        with (
            patch("host.runtime.admin_api.claude_code.complete_oauth_login") as complete,
            patch("host.runtime.admin_api.claude_code.read_claude_account", return_value=None),
            patch(
                "host.runtime.admin_api.orchestrator.refresh_runtime_status",
                return_value="awaiting_login",
            ) as refresh_status,
        ):
            self.assertEqual(admin_api.complete_claude_oauth_login({"code": "browser-code"}), {"status": "accepted"})

        complete.assert_called_once_with("browser-code")
        refresh_status.assert_called_once_with("claude_code")
        self.assertIsNone(load_state()["claude_oauth"])

    def test_reset_linked_account_clears_anchor_pin_and_pending_oauth(self) -> None:
        save_policy(
            {"network_integrations": {"openai": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        snapshot = load_state()
        snapshot["agent_runtime_statuses"]["codex"]["status"] = "error"
        snapshot["codex_oauth"] = {
            "status": "awaiting_login",
            "device_code": "CODE",
            "login_id": "login-1",
            "login_url": "https://auth.openai.com/device",
            "expires_at": "2099-06-08T00:10:00Z",
        }
        save_state(snapshot)
        save_approved_openai_account("acct_old")
        state.save_proxy_openai_account_id("acct_old")

        completed = subprocess.CompletedProcess(
            [*admin_api.AGENT_AUTH_CLEAR_HELPER_COMMAND, "codex"], 0, stdout='{"removed":[]}', stderr=""
        )
        with (
            patch("host.runtime.admin_api.subprocess.run", return_value=completed) as run,
            patch(
                "host.runtime.admin_api.orchestrator.refresh_runtime_status",
                return_value="awaiting_login",
            ) as refresh_status,
        ):
            self.assertEqual(
                admin_api.reset_linked_account({"agent_runtime": "codex"}),
                {"status": "accepted"},
            )

        run.assert_called_once_with(
            [*admin_api.AGENT_AUTH_CLEAR_HELPER_COMMAND, "codex"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=admin_api.AGENT_AUTH_CLEAR_HELPER_TIMEOUT_SECONDS,
        )
        refresh_status.assert_called_once_with("codex")
        self.assertEqual(orchestrator.runtime_status("codex"), "awaiting_login")
        self.assertIsNone(load_state()["codex_oauth"])
        self.assertIsNone(read_openai_account().get("account_id"))
        self.assertIsNone(read_proxy_openai_account_id())

    def test_reset_linked_account_clears_claude_anchor_and_pin(self) -> None:
        save_policy(
            {"network_integrations": {"claude": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        snapshot = load_state()
        snapshot["agent_runtime_statuses"]["claude_code"]["status"] = "active"
        save_state(snapshot)
        save_claude_account({"account_id": "acct_old", "access_token_sha256": "f" * 64})
        state.save_proxy_claude_account({"account_id": "acct_old", "access_token_sha256": "f" * 64})

        completed = subprocess.CompletedProcess(
            [*admin_api.AGENT_AUTH_CLEAR_HELPER_COMMAND, "claude"], 0, stdout='{"removed":[]}', stderr=""
        )
        with (
            patch("host.runtime.admin_api.subprocess.run", return_value=completed) as run,
            patch(
                "host.runtime.admin_api.orchestrator.refresh_runtime_status",
                return_value="awaiting_login",
            ) as refresh_status,
        ):
            self.assertEqual(
                admin_api.reset_linked_account({"agent_runtime": "claude_code"}),
                {"status": "accepted"},
            )

        run.assert_called_once_with(
            [*admin_api.AGENT_AUTH_CLEAR_HELPER_COMMAND, "claude"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=admin_api.AGENT_AUTH_CLEAR_HELPER_TIMEOUT_SECONDS,
        )
        refresh_status.assert_called_once_with("claude_code")
        self.assertEqual(read_claude_account(), {})
        self.assertEqual(read_proxy_claude_account(), {})

    def test_reset_linked_account_rejects_unknown_runtime(self) -> None:
        for body in (None, {}, {"agent_runtime": "cursor"}):
            with self.assertRaises(admin_api.ApiError) as error:
                admin_api.reset_linked_account(body)
            self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_reset_linked_account_kills_running_tasks_and_clears_auth(self) -> None:
        save_policy(
            {"network_integrations": {"openai": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        snapshot = load_state()
        snapshot["agent_runtime_statuses"]["codex"]["status"] = "active"
        snapshot["codex_oauth"] = {
            "status": "awaiting_login",
            "device_code": "CODE",
            "login_id": "login-1",
            "login_url": "https://auth.openai.com/device",
            "expires_at": "2099-06-08T00:10:00Z",
        }
        snapshot["tasks"] = [
            {
                "task_id": "task_1",
                "status": "running",
                "agent_runtime": "codex",
                "thread_id": "chat",
                "input_message": "hello",
                "steer_messages": [],
                "created_at": "2026-06-08T00:00:00Z",
                "updated_at": "2026-06-08T00:00:00Z",
            }
        ]
        snapshot["next_task_number"] = 2
        save_state(snapshot)
        save_approved_openai_account("acct_old")
        state.save_proxy_openai_account_id("acct_old")

        completed = subprocess.CompletedProcess(
            [*admin_api.AGENT_AUTH_CLEAR_HELPER_COMMAND, "codex"], 0, stdout='{"removed":[]}', stderr=""
        )
        with (
            patch("host.runtime.admin_api.subprocess.run", return_value=completed),
            patch(
                "host.runtime.admin_api.orchestrator.refresh_runtime_status",
                return_value="awaiting_login",
            ) as refresh_status,
        ):
            self.assertEqual(admin_api.reset_linked_account({"agent_runtime": "codex"}), {"status": "accepted"})

        refresh_status.assert_called_once_with("codex")
        snapshot = load_state()
        self.assertEqual(snapshot["tasks"][0]["status"], "failed")
        self.assertIsNone(read_openai_account().get("account_id"))
        self.assertIsNone(read_proxy_openai_account_id())
        self.assertIsNone(snapshot["codex_oauth"])

    def test_reset_linked_account_helper_failure_leaves_anchor_cleared_and_refreshes(self) -> None:
        save_policy(
            {"network_integrations": {"openai": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        save_approved_openai_account("acct_old")
        state.save_proxy_openai_account_id("acct_old")
        failed = subprocess.CompletedProcess(
            [*admin_api.AGENT_AUTH_CLEAR_HELPER_COMMAND, "codex"],
            1,
            stdout="",
            stderr="permission denied",
        )

        with (
            patch("host.runtime.admin_api.subprocess.run", return_value=failed),
            patch(
                "host.runtime.admin_api.orchestrator.refresh_runtime_status",
                return_value="awaiting_login",
            ) as refresh_status,
            self.assertRaises(admin_api.ApiError) as error,
        ):
            admin_api.reset_linked_account({"agent_runtime": "codex"})

        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)
        self.assertIn("retry reset", error.exception.message)
        self.assertIn("permission denied", error.exception.message)
        refresh_status.assert_called_once_with("codex")
        self.assertIsNone(read_openai_account().get("account_id"))
        self.assertIsNone(read_proxy_openai_account_id())

    def test_codex_oauth_start_allowed_while_runtime_error(self) -> None:
        # Error states (changed account, malformed local credentials) are
        # recovered by logging in again, so the gate admits them.
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "error"
        save_state(state)
        login = admin_api.codex_app_server.CodexLogin(
            login_id="login-1",
            verification_url="https://example.com/device",
            user_code="CODE-1",
        )

        with patch("host.runtime.admin_api.codex_app_server.start_device_login", return_value=login):
            response = admin_api.start_codex_oauth_login()

        self.assertEqual(response["device_code"], "CODE-1")

    def test_codex_oauth_start_reuses_existing_login(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "awaiting_login"
        save_state(state)
        login = admin_api.codex_app_server.CodexLogin(
            login_id="login-1",
            verification_url="https://example.com/device",
            user_code="CODE-1",
        )

        with patch("host.runtime.admin_api.codex_app_server.start_device_login", return_value=login) as start:
            first = admin_api.start_codex_oauth_login()
            second = admin_api.start_codex_oauth_login()

        self.assertEqual(first, second)
        self.assertEqual(start.call_count, 1)

    def test_claude_oauth_start_reuses_existing_login(self) -> None:
        save_policy(
            {"network_integrations": {"claude": {"enabled": True}}},
            "2026-06-08T00:00:01Z",
        )
        state = load_state()
        state["agent_runtime_statuses"]["claude_code"]["status"] = "awaiting_login"
        save_state(state)
        login = admin_api.claude_code.ClaudeLogin(login_url="https://claude.com/cai/oauth/authorize?code=true")

        with patch("host.runtime.admin_api.claude_code.start_oauth_login", return_value=login) as start:
            first = admin_api.start_claude_oauth_login()
            second = admin_api.start_claude_oauth_login()

        self.assertEqual(first, second)
        self.assertEqual(first["status"], "awaiting_code")
        self.assertEqual(start.call_count, 1)

    def test_prune_state_trims_finished_tasks_and_thread_maps(self) -> None:
        # The production caps are six figures now; the trimming behavior is
        # pinned with small patched limits so the test stays fast.
        state = load_state()
        finished_limit, map_limit = 8, 6
        # One queued task plus many finished ones beyond the history limit.
        finished = [
            {"task_id": f"task_{n}", "status": "completed", "agent_runtime": "codex",
             "thread_id": f"t{n}", "input_message": "x",
             "steer_messages": [], "created_at": "2026-06-07T00:00:00Z",
             "updated_at": "2026-06-07T00:00:00Z"}
            for n in range(1, finished_limit + 6)
        ]
        queued = {"task_id": "task_9000", "status": "queued", "agent_runtime": "codex",
                  "thread_id": "t9000", "input_message": "live",
                  "steer_messages": [], "created_at": "2026-06-07T00:00:00Z",
                  "updated_at": "2026-06-07T00:00:00Z"}
        state["tasks"] = finished + [queued]
        with patch.object(
            admin_api, "FINISHED_TASK_LIMIT", finished_limit
        ), patch.object(admin_api, "THREAD_MAP_LIMIT", map_limit):
            state["codex_threads"] = {
                f"codex-chat-{n}": {"codex_thread_id": f"thread_{n}", "last_used_at": f"2026-06-08T{n // 60:02d}:{n % 60:02d}:00Z"}
                for n in range(map_limit + 5)
            }
            state["claude_sessions"] = {
                f"claude-chat-{n}": {"session_id": f"session_{n}", "last_used_at": f"2026-06-09T{n // 60:02d}:{n % 60:02d}:00Z"}
                for n in range(map_limit + 5)
            }
            save_state(state)

            admin_api.prune_state()

        pruned = load_state()
        # The oldest unreferenced mappings are dropped and the most recently
        # used kept. Canonical rows referenced by retained tasks do not consume
        # that allowance.
        codex_history = {key for key in pruned["codex_threads"] if key.startswith("codex-chat-")}
        self.assertEqual(len(codex_history), map_limit)
        self.assertNotIn("codex-chat-0", codex_history)
        self.assertIn(f"codex-chat-{map_limit + 4}", codex_history)
        claude_history = {
            key for key in pruned["claude_sessions"] if key.startswith("claude-chat-")
        }
        self.assertEqual(len(claude_history), map_limit)
        self.assertNotIn("claude-chat-0", claude_history)
        self.assertIn(f"claude-chat-{map_limit + 4}", claude_history)
        statuses = [t["status"] for t in pruned["tasks"]]
        self.assertIn("queued", statuses)  # active task always kept
        self.assertEqual(statuses.count("completed"), finished_limit)
        # Oldest finished tasks dropped, newest kept.
        kept_ids = {t["task_id"] for t in pruned["tasks"]}
        self.assertNotIn("task_1", kept_ids)
        self.assertIn(f"task_{finished_limit + 5}", kept_ids)

    def test_network_events_are_read_from_the_database_with_cursor_paging(self) -> None:
        # Network events live in the database now (the proxy writes them under
        # its own role); the admin API exposes a single newest-first cursor.
        for index in range(120):
            append_network_event("https", "GET", "example.com", 443, f"/p{index}", "", index % 2 == 0)

        _, body = self.request("GET", "/v1/network/events")
        seqs = [event["seq"] for event in body["events"]]
        self.assertEqual(len(seqs), 100)
        self.assertEqual(seqs, sorted(seqs, reverse=True))
        self.assertNotIn("page", body)
        self.assertNotIn("total_events", body)

        _, older = self.request("GET", f"/v1/network/events?before={seqs[-1]}")
        older_seqs = [event["seq"] for event in older["events"]]
        self.assertEqual(older_seqs, list(range(20, 0, -1)))

        _, limited = self.request("GET", "/v1/network/events?limit=7")
        self.assertEqual(len(limited["events"]), 7)

        _, denied = self.request("GET", "/v1/network/events?decision=denied")
        self.assertEqual(len(denied["events"]), 60)
        self.assertTrue(all(event["decision"] == "denied" for event in denied["events"]))

        with self.assertRaises(urllib.error.HTTPError) as since_error:
            self.request("GET", "/v1/network/events?since=0")
        self.assertEqual(since_error.exception.code, HTTPStatus.BAD_REQUEST)
        rejected = json.loads(since_error.exception.read())
        self.assertEqual(rejected["error"]["message"], "unsupported network event query parameter: since")

        with self.assertRaises(urllib.error.HTTPError) as limit_error:
            self.request("GET", "/v1/network/events?limit=101")
        self.assertEqual(limit_error.exception.code, HTTPStatus.BAD_REQUEST)
        rejected = json.loads(limit_error.exception.read())
        self.assertEqual(rejected["error"]["message"], "limit must be at most 100")

    def test_agent_events_use_the_same_newest_first_cursor_paging(self) -> None:
        # The agent audit log pages exactly like the network audit log: one
        # newest-first cursor with no filter (task-scoped tailing has its own
        # since-based endpoint under /v1/tasks/{id}/events).
        with state.mutation() as cur:
            for index in range(120):
                state.append_agent_event(cur, "task.message", "task_1", {"message": f"m{index}"})

        _, body = self.request("GET", "/v1/events")
        seqs = [event["seq"] for event in body["events"]]
        self.assertEqual(len(seqs), 100)
        self.assertEqual(seqs, sorted(seqs, reverse=True))

        _, older = self.request("GET", f"/v1/events?before={seqs[-1]}")
        older_seqs = [event["seq"] for event in older["events"]]
        self.assertEqual(len(older_seqs), 20)
        self.assertTrue(all(seq < seqs[-1] for seq in older_seqs))

        _, limited = self.request("GET", "/v1/events?limit=7")
        self.assertEqual(len(limited["events"]), 7)

        with self.assertRaises(urllib.error.HTTPError) as since_error:
            self.request("GET", "/v1/events?since=0")
        self.assertEqual(since_error.exception.code, HTTPStatus.BAD_REQUEST)
        rejected = json.loads(since_error.exception.read())
        self.assertEqual(rejected["error"]["message"], "unsupported event query parameter: since")

        with self.assertRaises(urllib.error.HTTPError) as limit_error:
            self.request("GET", "/v1/events?limit=101")
        self.assertEqual(limit_error.exception.code, HTTPStatus.BAD_REQUEST)
        rejected = json.loads(limit_error.exception.read())
        self.assertEqual(rejected["error"]["message"], "limit must be at most 100")

    def test_reboot_uses_privileged_helper(self) -> None:
        succeeded = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("host.runtime.admin_api.subprocess.run", return_value=succeeded) as run:
            _, body = self.request("POST", "/v1/host-runtime/reboot")
        self.assertEqual(body["status"], "accepted")
        run.assert_called_with(
            ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/reboot-host"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=admin_api.REBOOT_HELPER_TIMEOUT_SECONDS,
        )

    def test_get_network_policy_reads_policy_file(self) -> None:
        save_policy(
            parse_network_controls(
                {
                    "network_integrations": {
                        "openai": {"enabled": True},
                        "custom": {"domains": {"api.example.com": {"allow_http_methods": ["GET"]}}},
                    },
                }
            ).to_json(),
            "2026-06-08T00:00:03Z",
        )
        _, body = self.request("GET", "/v1/network/policy")
        self.assertEqual(
            body["network_controls"]["network_integrations"],
            {
                "openai": {"enabled": True},
                "custom": {"domains": {"api.example.com": {"allow_http_methods": ["GET"]}}},
            },
        )
        self.assertEqual(body["updated_at"], "2026-06-08T00:00:03Z")

    def test_kill_cancels_running_task_and_worker_does_not_resurrect_it(self) -> None:
        state = load_state()
        state["tasks"] = [
            {
                "task_id": "task_1",
                "status": "running",
                "agent_runtime": "codex",
                "thread_id": "t1",
                "input_message": "long task",
                "steer_messages": [],
                "created_at": "2026-06-08T00:00:00Z",
                "updated_at": "2026-06-08T00:00:00Z",
            }
        ]
        save_state(state)

        with patch("host.runtime.admin_api.orchestrator.close_task_server") as close:
            _, body = self.request("POST", "/v1/tasks/task_1/kill")
        self.assertEqual(body["status"], "accepted")
        close.assert_called_once_with("task_1")
        _, task = self.request("GET", "/v1/tasks/task_1")
        self.assertEqual(task["status"], "cancelled")

        # The in-flight worker finishing later must not flip the cancelled task.
        orchestrator._finish_task(
            "task_1",
            "completed",
            output="late result",
            runtime_type="codex",
            thread_id="t1",
            provider_session_id="thread_9",
        )
        _, task = self.request("GET", "/v1/tasks/task_1")
        self.assertEqual(task["status"], "cancelled")
        self.assertNotIn("output_message", task)

    def test_kill_rejects_tasks_that_are_not_running(self) -> None:
        _, queued = self.request("POST", "/v1/tasks", {"input_message": "waiting", "thread_id": "t1", "agent_runtime": "codex"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", f"/v1/tasks/{queued['task_id']}/kill")
        self.assertEqual(error.exception.code, 409)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", "/v1/tasks/task_999999/kill")
        self.assertEqual(error.exception.code, 404)

    def test_create_task_requires_a_valid_thread_id(self) -> None:
        for index, bad in enumerate((None, "", "has space", "bad/slash", "x" * 65)):
            body: dict[str, object] = {"input_message": "hello"}
            if bad is not None:
                body["thread_id"] = bad
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("POST", "/v1/tasks", body)
            self.assertEqual(error.exception.code, 400)
        _, task = self.request(
            "POST", "/v1/tasks", {"input_message": "hello", "thread_id": "Chat_01-a", "agent_runtime": "codex"}
        )
        self.assertEqual(task["thread_id"], "Chat_01-a")

    def test_initialize_state_fails_tasks_orphaned_by_a_restart(self) -> None:
        state = load_state()
        state["tasks"] = [
            {
                "task_id": "task_1",
                "status": "running",
                "agent_runtime": "codex",
                "thread_id": "t1",
                "input_message": "interrupted task",
                "steer_messages": [],
                "created_at": "2026-06-08T00:00:00Z",
                "updated_at": "2026-06-08T00:00:00Z",
            }
        ]
        save_state(state)

        admin_api.initialize_state()

        _, task = self.request("GET", "/v1/tasks/task_1")
        self.assertEqual(task["status"], "failed")
        self.assertIn("restarted while the task was running", task["error_message"])

    def test_event_seq_commits_atomically_with_the_event(self) -> None:
        # Event seqs come from a database serial: unique and increasing, and
        # an aborted mutation rolls its event row back (burning the seq), so a
        # seq can never appear twice in the log — duplicate seqs would break
        # cursor-based event pagination.
        with state.mutation() as cur:
            first = state.append_agent_event(cur, "task.message", "task_1", {"message": "hello"})
        with self.assertRaises(RuntimeError):
            with state.mutation() as cur:
                state.append_agent_event(cur, "task.message", "task_1", {"message": "aborted"})
                raise RuntimeError("abort after allocating a seq")
        with state.mutation() as cur:
            second = state.append_agent_event(cur, "task.message", "task_1", {"message": "again"})

        self.assertGreater(second, first)
        _, body = self.request("GET", "/v1/events")
        self.assertEqual([event["seq"] for event in body["events"]], [second, first])

    def test_second_instance_fails_on_bind_before_touching_live_state(self) -> None:
        state = load_state()
        state["tasks"] = [
            {
                "task_id": "task_1",
                "status": "running",
                "agent_runtime": "codex",
                "thread_id": "t1",
                "input_message": "live task",
                "steer_messages": [],
                "created_at": "2026-06-08T00:00:00Z",
                "updated_at": "2026-06-08T00:00:00Z",
            }
        ]
        save_state(state)

        # The port bind is the single-instance gate: a second instance must die
        # there without failing the live instance's running task. The service
        # never runs migrations (that is bootstrap's job), so a stray start
        # also cannot move the schema under the live instance.
        with patch(
            "host.runtime.admin_api.ThreadingHTTPServer",
            side_effect=OSError("address already in use"),
        ):
            with self.assertRaises(OSError):
                admin_api.main()

        persisted = load_state()
        self.assertEqual(persisted["tasks"][0]["status"], "running")



class ToolRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        save_config(
            {
                "agent_name": "trustyclaw-test",
                "admin_password_sha256": hashlib.sha256(b"admin-secret").hexdigest(),
            }
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), admin_api.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        # The operator delegation routes (connect complete/disconnect, approval
        # decide) forward to the trustyclaw-tools service socket, so stand one up
        # in-process (same DB and BUNDLED_TOOLS) and point the admin API at it.
        socket_dir = tempfile.TemporaryDirectory()
        self.addCleanup(socket_dir.cleanup)
        tools_socket = str(Path(socket_dir.name) / "tools.sock")
        previous_socket = tools_admin_api.TOOLS_SOCKET_PATH
        tools_admin_api.TOOLS_SOCKET_PATH = tools_socket
        self.addCleanup(setattr, tools_admin_api, "TOOLS_SOCKET_PATH", previous_socket)
        tools_server = tools_api.ToolsServer(
            tools_socket, frozenset({os.getuid()}), frozenset({os.getuid()})
        )
        threading.Thread(target=tools_server.serve_forever, daemon=True).start()
        self.addCleanup(tools_server.server_close)
        self.addCleanup(tools_server.shutdown)

    def request(self, method: str, path: str, body: object | None = None, auth: bool = True):
        data = json.dumps(body).encode() if body is not None else b"{}" if method != "GET" else None
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        if auth:
            request.add_header("Authorization", "Bearer admin-secret")
        if method != "GET":
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())

    def tool_entry(self, body: dict, tool_id: str) -> dict:
        return next(entry for entry in body["tools"] if entry["tool_id"] == tool_id)

    def test_listing_requires_auth_and_reports_manifest_state(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("GET", "/v1/tools", auth=False)
        self.assertEqual(error.exception.code, 401)

        status, body = self.request("GET", "/v1/tools")
        self.assertEqual(status, 200)
        # New bundled packages should not need an edit here; released ids may
        # not vanish. (test_tools_host uses the same issubset contract.)
        self.assertTrue(
            {
                "brave_search",
                "gmail",
                "google_calendar",
                "ibkr",
                "instagram",
                "instagram_discovery",
                "linkedin",
                "linkedin_discovery",
                "polymarket",
                "runway",
                "twitter",
            }.issubset({entry["tool_id"] for entry in body["tools"]})
        )
        gmail = self.tool_entry(body, "gmail")
        self.assertFalse(gmail["enabled"])
        self.assertEqual(gmail["connection"], "oauth")
        self.assertEqual(gmail["connection_status"], {"connected": False})
        self.assertTrue(any(action["id"] == "send_email" for action in gmail["actions"]))
        self.assertTrue(all("output_schema" in action for action in gmail["actions"]))
        # Data policy is per action.
        self.assertTrue(all(action["data_policy"] for action in gmail["actions"]))
        send = next(action for action in gmail["actions"] if action["id"] == "send_email")
        self.assertIn("approval", send["data_policy"].lower())
        self.assertEqual(send["approval"], "operator")
        read = next(action for action in gmail["actions"] if action["id"] == "read_message")
        self.assertEqual(read["approval"], "direct")
        self.assertTrue(all(gmail["protections"]))
        self.assertEqual(gmail["technical_details"], [])
        self.assertGreaterEqual(len(gmail["setup_steps"]), 5)
        self.assertIn("Google Cloud", gmail["setup_steps"][0]["description"])
        self.assertTrue(any(step["image_path"] for step in gmail["setup_steps"]))
        self.assertEqual(
            [card["title"] for card in gmail["data_summary"]["cards"]],
            ["What leaves this host", "Where it can go", "What Google can do with it", "How long Google retains it"],
        )
        reads_point = gmail["data_summary"]["cards"][0]["points"][0]
        self.assertEqual(reads_point["label"], "Reads")
        self.assertIn("Gmail search query", reads_point["text"])
        self.assertIn("your own Gmail account", gmail["data_summary"]["cards"][1]["description"])
        # The callback URI and config keys render inside the step that needs them.
        self.assertTrue(any(step["show_callback"] for step in gmail["setup_steps"]))
        self.assertTrue(gmail["setup_steps"][-1]["show_config"])
        self.assertTrue(
            all(
                link["url"].startswith("https://")
                for card in gmail["data_summary"]["cards"]
                for link in card["links"]
            )
        )
        config_keys = {entry["key"]: entry for entry in gmail["config"]}
        self.assertFalse(config_keys["GOOGLE_OAUTH_CLIENT_ID"]["set"])
        # All config values are secrets; there is no per-key secret flag.
        self.assertNotIn("secret", config_keys["GOOGLE_OAUTH_CLIENT_ID"])

        discovery = self.tool_entry(body, "instagram_discovery")
        self.assertIn("at most 25 unique items", " ".join(discovery["protections"]))
        self.assertIn("maps vendor responses to fixed fields", " ".join(discovery["technical_details"]))
        for tool_id in ("ibkr", "instagram", "linkedin", "runway", "twitter"):
            self.assertEqual(self.tool_entry(body, tool_id)["technical_details"], [])

    def test_config_and_enable_flow(self) -> None:
        # Config is scoped per tool: a key must be declared by that tool.
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("PUT", "/v1/tools/brave_search/config", {"key": "NOT_DECLARED", "value": "x"})
        self.assertEqual(error.exception.code, 400)

        status, body = self.request("GET", "/v1/tools")
        brave = self.tool_entry(body, "brave_search")
        brave_config = {entry["key"]: entry for entry in brave["config"]}
        self.assertFalse(brave_config["BRAVE_SEARCH_API_KEY"]["set"])
        self.assertNotIn("secret", brave_config["BRAVE_SEARCH_API_KEY"])

        # Enablement is not gated on config: enabling without config succeeds, and
        # the tool is enabled even though its config is not set.
        status, body = self.request("POST", "/v1/tools/brave_search/enable")
        self.assertEqual(body, {"tool_id": "brave_search", "enabled": True})
        status, body = self.request("GET", "/v1/tools")
        entry = self.tool_entry(body, "brave_search")
        self.assertTrue(entry["enabled"])
        self.assertFalse(entry["config"][0]["set"])

        # Setting the config flips its set status; enablement is unchanged.
        status, body = self.request("PUT", "/v1/tools/brave_search/config", {"key": "BRAVE_SEARCH_API_KEY", "value": "key-1"})
        self.assertEqual(body, {"tool_id": "brave_search", "key": "BRAVE_SEARCH_API_KEY", "set": True})
        status, body = self.request("GET", "/v1/tools")
        entry = self.tool_entry(body, "brave_search")
        self.assertTrue(entry["enabled"])
        self.assertTrue(entry["config"][0]["set"])

        status, body = self.request("POST", "/v1/tools/brave_search/disable")
        self.assertEqual(body["enabled"], False)
        status, body = self.request("PUT", "/v1/tools/brave_search/config", {"key": "BRAVE_SEARCH_API_KEY", "value": ""})
        self.assertEqual(body["set"], False)

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", "/v1/tools/unknown_tool/enable")
        self.assertEqual(error.exception.code, 404)

    def test_connect_flow_routing_and_gating(self) -> None:
        # enable_only tools have no connect flow.
        self.request("PUT", "/v1/tools/brave_search/config", {"key": "BRAVE_SEARCH_API_KEY", "value": "key-1"})
        self.request("POST", "/v1/tools/brave_search/enable")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", "/v1/tools/brave_search/oauth_connect/start", {"redirect_uri": "http://x/cb"})
        self.assertEqual(error.exception.code, 409)

        # An enabled OAuth tool with no client config fails as an actionable
        # operator input error; it is not a tools-service gateway failure.
        self.request("POST", "/v1/tools/gmail/enable")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "POST",
                "/v1/tools/gmail/oauth_connect/start",
                {"redirect_uri": "http://localhost:7443/oauth/callback"},
            )
        self.assertEqual(error.exception.code, 400)
        self.request("POST", "/v1/tools/gmail/disable")

        # OAuth tools require enablement before connecting.
        self.request("PUT", "/v1/tools/gmail/config", {"key": "GOOGLE_OAUTH_CLIENT_ID", "value": "client-1"})
        self.request("PUT", "/v1/tools/gmail/config", {"key": "GOOGLE_OAUTH_CLIENT_SECRET", "value": "secret-1"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", "/v1/tools/gmail/oauth_connect/start", {"redirect_uri": "http://x/cb"})
        self.assertEqual(error.exception.code, 409)

        self.request("POST", "/v1/tools/gmail/enable")
        status, body = self.request(
            "POST", "/v1/tools/gmail/oauth_connect/start", {"redirect_uri": "http://localhost:7443/oauth/callback"}
        )
        self.assertEqual(status, 200)
        self.assertIn("accounts.google.com", body["authorization_url"])
        self.assertIn("client-1", body["authorization_url"])
        self.assertTrue(body["state"])

        # A forged callback state is rejected without any third-party call.
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "POST",
                "/v1/tools/gmail/oauth_connect/complete",
                {"code": "code-1", "state": "forged.state", "redirect_uri": "http://localhost:7443/oauth/callback"},
            )
        self.assertEqual(error.exception.code, 400)

        status, body = self.request("POST", "/v1/tools/gmail/oauth_connect/disconnect")
        self.assertEqual(body, {"tool_id": "gmail", "connected": False})

        # Disconnect must stay available after the tool is disabled, or
        # stored OAuth tokens would be stuck with no path to revoke them.
        self.request("POST", "/v1/tools/gmail/disable")
        status, body = self.request("POST", "/v1/tools/gmail/oauth_connect/disconnect")
        self.assertEqual(body, {"tool_id": "gmail", "connected": False})

    def test_approval_decisions(self) -> None:
        from test_tools_host import FakeTool

        with patch.dict(admin_api.tools_host.BUNDLED_TOOLS, {"fake_notes": FakeTool()}):
            self.request("PUT", "/v1/tools/fake_notes/config", {"key": "FAKE_NOTES_TOKEN", "value": "token-1"})
            self.request("POST", "/v1/tools/fake_notes/enable")
            pending = admin_api.tools_host.execute_action("fake_notes", "write_note", {"text": "hello"})
            approval_id = pending["approval_id"]

            status, body = self.request("GET", "/v1/tools/fake_notes/approvals")
            self.assertEqual(body["approvals"][0]["approval_id"], approval_id)
            self.assertEqual(body["approvals"][0]["status"], "pending")
            # The list is summary-only; the payload is fetched on demand.
            self.assertNotIn("payload", body["approvals"][0])
            status, single = self.request("GET", f"/v1/tools/fake_notes/approvals/{approval_id}")
            self.assertEqual(single["approval"]["payload"], {"text": "hello"})
            with self.assertRaises(urllib.error.HTTPError) as missing:
                self.request("GET", "/v1/tools/fake_notes/approvals/approval_9999")
            self.assertEqual(missing.exception.code, 404)
            # An approval is scoped to its tool: another tool's path cannot read
            # or decide it.
            with self.assertRaises(urllib.error.HTTPError) as wrong_tool:
                self.request("GET", f"/v1/tools/gmail/approvals/{approval_id}")
            self.assertEqual(wrong_tool.exception.code, 404)
            with self.assertRaises(urllib.error.HTTPError) as wrong_tool_decide:
                self.request("POST", f"/v1/tools/gmail/approvals/{approval_id}/approve")
            self.assertEqual(wrong_tool_decide.exception.code, 404)

            status, body = self.request("POST", f"/v1/tools/fake_notes/approvals/{approval_id}/approve")
            self.assertEqual(body["approval"]["status"], "executed")
            self.assertEqual(body["result"], {"status": "executed", "message": "Wrote the note (5 chars)."})

            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("POST", f"/v1/tools/fake_notes/approvals/{approval_id}/deny")
            self.assertEqual(error.exception.code, 409)

            denied = admin_api.tools_host.execute_action("fake_notes", "write_note", {"text": "no"})
            status, body = self.request("POST", f"/v1/tools/fake_notes/approvals/{denied['approval_id']}/deny")
            self.assertEqual(body["approval"]["status"], "denied")

    def test_approval_list_limit_matches_pending_cap(self) -> None:
        with patch.object(admin_api.state, "list_tool_approvals", return_value=[]) as listing:
            status, body = self.request("GET", "/v1/tools/fake_notes/approvals")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"approvals": []})
        listing.assert_called_once_with(admin_api.tools_host.PENDING_APPROVAL_LIMIT, tool_id="fake_notes")

    def test_tool_events_endpoint_pages_newest_first(self) -> None:
        from test_tools_host import FakeTool

        with patch.dict(admin_api.tools_host.BUNDLED_TOOLS, {"fake_notes": FakeTool()}):
            self.request("PUT", "/v1/tools/fake_notes/config", {"key": "FAKE_NOTES_TOKEN", "value": "token-1"})
            self.request("POST", "/v1/tools/fake_notes/enable")
            for _ in range(3):
                admin_api.tools_host.execute_action("fake_notes", "read_note", {})

            status, body = self.request("GET", "/v1/tools/events?limit=2")
            self.assertEqual(status, 200)
            self.assertEqual(len(body["events"]), 2)
            seqs = [event["seq"] for event in body["events"]]
            self.assertEqual(seqs, sorted(seqs, reverse=True))
            self.assertEqual(body["events"][0]["tool_id"], "fake_notes")
            self.assertEqual(body["events"][0]["action_id"], "read_note")
            self.assertTrue(body["events"][0]["has_arguments"])
            self.assertNotIn("arguments", body["events"][0])

            status, detail = self.request("GET", f"/v1/tools/events/{seqs[0]}")
            self.assertEqual(status, 200)
            self.assertEqual(detail["event"]["arguments"], {})

            status, older = self.request("GET", f"/v1/tools/events?before={seqs[-1]}")
            self.assertTrue(all(event["seq"] < seqs[-1] for event in older["events"]))

            # Config and enablement changes are audited alongside calls.
            all_events = self.request("GET", "/v1/tools/events")[1]["events"]
            kinds = {(event["action_id"], event["outcome"], event["detail"]) for event in all_events}
            self.assertIn(("config", "set", "FAKE_NOTES_TOKEN"), kinds)
            self.assertIn(("enablement", "enabled", ""), kinds)
            config_event = next(event for event in all_events if event["action_id"] == "config")
            self.assertFalse(config_event["has_arguments"])
            self.request("POST", "/v1/tools/fake_notes/disable")
            after_disable = self.request("GET", "/v1/tools/events")[1]["events"]
            self.assertEqual(after_disable[0]["action_id"], "enablement")
            self.assertEqual(after_disable[0]["outcome"], "disabled")

            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", "/v1/tools/events?bogus=1")
            self.assertEqual(error.exception.code, 400)

            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", "/v1/tools/events/999999")
            self.assertEqual(error.exception.code, 404)

    def test_oauth_callback_serves_the_ui_shell(self) -> None:
        request = urllib.request.Request(f"{self.base_url}/oauth/callback?code=x&state=y")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])
            self.assertIn(b"TrustyClaw", response.read())


if __name__ == "__main__":
    unittest.main()
