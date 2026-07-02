from __future__ import annotations

import hashlib
from http import HTTPStatus
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import subprocess
import urllib.error
import urllib.request

import pg_harness

from host.config import parse_network_controls
from host.runtime import admin_api, orchestrator, proxy_state_client
from host.runtime.network_policy import load_policy, save_policy
from host.runtime import state
from host.runtime.state import (
    append_network_event,
    read_claude_account,
    read_openai_account,
    read_proxy_claude_account,
    read_proxy_openai_account_id,
    save_claude_account,
    save_config,
    save_openai_account,
)
from state_seed import load_state, save_state


class AdminApiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        admin_api.IDEMPOTENCY_ENTRIES.clear()
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
            {"managed_ai_provider_network_access": {"openai": True}, "allowed_network_access": {}},
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

    def request(self, method: str, path: str, body: object | None = None, auth: bool = True, idem: str | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        if auth:
            request.add_header("Authorization", "Bearer admin-secret")
        if idem:
            request.add_header("Idempotency-Key", idem)
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
        ):
            return self.request("GET", "/v1/health")

    def runtime(self, body: dict[str, object], runtime_type: str = "codex") -> dict[str, object]:
        runtimes = body["agent_runtime"]["runtimes"]  # type: ignore[index]
        return next(item for item in runtimes if item["type"] == runtime_type)  # type: ignore[union-attr]

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

        self.assertEqual(metrics["used_bytes"], 1)
        self.assertEqual(metrics["total_bytes"], 10)
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
            b"Idempotency-Key: raw-invalid-length\r\n"
            b"Content-Length: nope\r\n\r\n"
        )
        huge = self.raw_request(
            b"POST /v1/tasks HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer admin-secret\r\n"
            b"Idempotency-Key: raw-huge-length\r\n"
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
            "expires_at": "2026-06-08T00:10:00Z",
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
        self.assertEqual(read_claude_account(), {})
        _, body = self.request("GET", "/v1/agent-runtime/status")
        self.assertNotIn("error_message", self.runtime({"agent_runtime": body}, "claude_code"))

    def test_ui_page_is_served_without_auth(self) -> None:
        request = urllib.request.Request(f"{self.base_url}/")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])
            page = response.read().decode()
        self.assertIn("TrustyClaw", page)
        self.assertIn('/admin_ui.css', page)
        self.assertIn('/admin_ui.js', page)

        for path, content_type, expected in (
            ("/admin_ui.css", "text/css", ".shell"),
            ("/admin_ui.js", "application/javascript", "/v1/health"),
        ):
            request = urllib.request.Request(f"{self.base_url}{path}")
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(content_type, response.headers["Content-Type"])
                body = response.read().decode()
            self.assertIn(expected, body)

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

    def test_idempotency_key_replay_returns_original_response(self) -> None:
        _, first = self.request("POST", "/v1/tasks", {"input_message": "do it", "thread_id": "t1", "agent_runtime": "codex"}, idem="same-key")
        _, replay = self.request("POST", "/v1/tasks", {"input_message": "do it", "thread_id": "t1", "agent_runtime": "codex"}, idem="same-key")

        self.assertEqual(first["task_id"], replay["task_id"])
        _, listed = self.request("GET", "/v1/tasks")
        self.assertEqual(len(listed["tasks"]), 1)

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", f"/v1/tasks/{first['task_id']}/cancel", idem="same-key")
        self.assertEqual(error.exception.code, 400)

    def test_mutations_require_valid_idempotency_key(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", "/v1/tasks", {"input_message": "hello", "thread_id": "t1", "agent_runtime": "codex"})
        self.assertEqual(error.exception.code, 400)

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", "/v1/tasks", {"input_message": "hello", "thread_id": "t1", "agent_runtime": "codex"}, idem="bad key")
        self.assertEqual(error.exception.code, 400)

    def test_task_create_list_update_cancel_and_events(self) -> None:
        status, task = self.request("POST", "/v1/tasks", {"input_message": "first task", "thread_id": "t1", "agent_runtime": "codex"}, idem="task-1")
        self.assertEqual(status, 200)
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["agent_runtime"], "codex")
        task_id = task["task_id"]

        _, listed = self.request("GET", "/v1/tasks")
        self.assertEqual(listed["tasks"][0]["queue_position"], 1)
        self.assertEqual(listed["tasks"][0]["input_message"], "first task")
        self.assertEqual(listed["tasks"][0]["thread_id"], "t1")

        _, updated = self.request("PUT", f"/v1/tasks/{task_id}", {"input_message": "updated task"}, idem="task-2")
        self.assertEqual(updated["input_message"], "updated task")

        _, events = self.request("GET", f"/v1/tasks/{task_id}/events")
        self.assertEqual(events["events"], [])

        _, cancel = self.request("POST", f"/v1/tasks/{task_id}/cancel", idem="task-3")
        self.assertEqual(cancel["status"], "accepted")
        _, cancelled = self.request("GET", f"/v1/tasks/{task_id}")
        self.assertEqual(cancelled["status"], "cancelled")
        _, events = self.request("GET", f"/v1/tasks/{task_id}/events")
        self.assertEqual([event["event_type"] for event in events["events"]], ["task.cancelled"])

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

    def test_create_task_rejects_thread_runtime_conflicts(self) -> None:
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
                idem="conflict-task",
            )
        self.assertEqual(task_error.exception.code, 409)

        with self.assertRaises(urllib.error.HTTPError) as session_error:
            self.request(
                "POST",
                "/v1/tasks",
                {"input_message": "bad", "thread_id": "used-by-session", "agent_runtime": "codex"},
                idem="conflict-session",
            )
        self.assertEqual(session_error.exception.code, 409)

        _, accepted = self.request(
            "POST",
            "/v1/tasks",
            {"input_message": "ok", "thread_id": "used-by-task", "agent_runtime": "codex"},
            idem="conflict-ok",
        )
        self.assertEqual(accepted["thread_id"], "used-by-task")

        with self.assertRaises(urllib.error.HTTPError) as old_route_error:
            self.request("GET", "/v1/tasks/finished")
        self.assertEqual(old_route_error.exception.code, 404)

    def test_task_event_history_can_be_paged_for_selected_task(self) -> None:
        with state.mutation() as cur:
            state.insert_task(
                cur,
                {
                    "task_id": "task_1",
                    "status": "completed",
                    "agent_runtime": "codex",
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
            (runtime / filename).read_text()
            for filename in ("admin_ui.html", "admin_ui.css", "admin_ui.js")
        )
        self.assertIn("<h2>Threads</h2>", html)
        self.assertIn('<link rel="stylesheet" href="/admin_ui.css">', html)
        self.assertIn('<script src="/admin_ui.js"></script>', html)
        self.assertIn("/v1/threads", ui)
        self.assertIn("/v1/threads/${encodeURIComponent(threadId)}/tasks", ui)
        self.assertIn("/v1/tasks/${encodeURIComponent(taskId)}/events", ui)
        self.assertIn("thread.task_count", ui)
        self.assertIn("TASK_EVENT_PAGE_BATCH", ui)
        self.assertIn("loadTaskEventBatch", ui)
        self.assertIn("loadMoreTaskEvents", ui)
        self.assertIn('data-action="show-thread"', ui)
        self.assertIn('data-action="show-task-events"', ui)
        self.assertIn('$("new-task-thread").value = threadId', ui)
        self.assertIn('$("new-task-runtime").value = agentRuntime', ui)
        self.assertIn("await loadThreads();", ui)
        self.assertIn('button[data-action]', ui)
        self.assertNotIn("onclick=", ui)
        self.assertNotIn("oninput=", ui)
        self.assertIn('id="policy-preset-openai"', html)
        self.assertIn('id="policy-preset-claude"', html)
        for label in (
            "OpenAI preset domains",
            "Claude preset domains",
            "GitHub preset domains",
            "Python packages preset domains",
            "npm packages preset domains",
        ):
            self.assertIn(f'aria-label="{label}"', html)
        self.assertIn("togglePresetInfo", ui)
        self.assertIn("renderPresetInfo", ui)
        self.assertIn('id="preset-info-popover"', html)
        self.assertIn("OpenAI expands internally", ui)
        self.assertIn("Claude expands internally", ui)
        self.assertIn("GitHub expands", ui)
        self.assertIn("api.openai.com", ui)
        self.assertIn("POST; account guard; live web search disabled", ui)
        self.assertIn("auth.openai.com", ui)
        self.assertIn("GET, POST", ui)
        self.assertIn("api.anthropic.com", ui)
        self.assertIn("GET, POST; account guard", ui)
        self.assertIn("api.github.com", ui)
        self.assertIn("GET, POST, PATCH, PUT, DELETE", ui)
        self.assertIn("pypi.org", ui)
        self.assertIn("GET, HEAD; only /simple and /pypi/<package>/json paths", ui)
        self.assertIn("registry.npmjs.org", ui)
        self.assertIn("manual-domain", ui)
        self.assertIn("Add manual domain", html)
        self.assertIn("POLICY_PRESETS", ui)
        self.assertIn("POLICY_PRESET_BUTTONS", ui)
        self.assertIn("objectValue", ui)
        self.assertIn("!Array.isArray(value)", ui)
        self.assertIn("policyPresetState", ui)
        self.assertIn("renderPolicyPresets", ui)
        self.assertIn("rulesEqual", ui)
        self.assertIn("wildcardCoversDomain", ui)
        self.assertIn("hasWildcardOverlap", ui)
        self.assertIn('pattern.startsWith("*.")', ui)
        self.assertIn("domain.endsWith(pattern.slice(1))", ui)
        self.assertIn("domain !== pattern.slice(2)", ui)
        self.assertIn("covered by a wildcard", ui)
        self.assertIn('button.disabled = state === "partial"', ui)
        self.assertIn("Remove ${copy.label}", ui)
        self.assertIn("removePolicyPreset", ui)
        self.assertIn("preset-active", ui)
        self.assertIn("preset-partial", ui)
        self.assertIn("applyPolicyPreset(preset)", ui)
        self.assertIn('data-preset="openai"', ui)
        self.assertIn('data-preset="claude"', ui)
        self.assertIn('data-preset="github"', ui)
        self.assertIn('data-preset="python"', ui)
        self.assertIn('data-preset="npm"', ui)
        for domain in (
            "github.com",
            "api.github.com",
            "codeload.github.com",
            "objects.githubusercontent.com",
            "raw.githubusercontent.com",
            "release-assets.githubusercontent.com",
            "pypi.org",
            "files.pythonhosted.org",
            "nodejs.org",
            "registry.npmjs.org",
        ):
            self.assertIn(domain, ui)
        self.assertIn("saveWebsiteRule", ui)
        self.assertNotIn("editWebsiteRule", ui)
        self.assertNotIn("removeWebsiteRule", ui)
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
                self.request("POST", "/v1/tasks", body, idem=f"runtime-bad-{index}")
            self.assertEqual(error.exception.code, 400)

        _, body = self.request(
            "POST",
            "/v1/tasks",
            {"input_message": "hello", "thread_id": "t1", "agent_runtime": "claude_code"},
            idem="runtime-claude",
        )
        self.assertEqual(body["agent_runtime"], "claude_code")

    def test_network_policy_replace_and_events(self) -> None:
        body = {
            "managed_ai_provider_network_access": {"openai": True},
            "allowed_network_access": {
                "api.example.com": {"allow_http_methods": ["GET"], "path_guards": ["^/v1$"]}
            },
        }
        _, response = self.request("PUT", "/v1/network/policy", body, idem="network-1")

        self.assertEqual(response["network_controls"]["allowed_network_access"]["api.example.com"]["allow_http_methods"], ["GET"])
        # The stored policy keeps the operator-facing shape: managed provider
        # domains are expanded only inside the proxy process.
        self.assertNotIn("api.openai.com", response["network_controls"]["allowed_network_access"])
        stored = load_policy()
        self.assertEqual(stored, response["network_controls"])
        self.assertNotIn("api.openai.com", stored["allowed_network_access"])
        self.mock_reconcile.assert_called_once()
        _, current = self.request("GET", "/v1/network/policy")
        self.assertEqual(current["network_controls"], response["network_controls"])

    def test_network_policy_rejects_ssh_port_field(self) -> None:
        body = {"ssh_port_opened": False, "managed_ai_provider_network_access": {"openai": True}, "allowed_network_access": {}}
        with patch("host.runtime.admin_api.subprocess.run") as run:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("PUT", "/v1/network/policy", body, idem="ssh-1")
        self.assertEqual(error.exception.code, 400)
        run.assert_not_called()

    def test_network_policy_replace_succeeds_when_existing_policy_is_error(self) -> None:
        save_policy({"bogus": True}, "2026-06-08T00:00:01Z")
        body = {"managed_ai_provider_network_access": {"openai": True}, "allowed_network_access": {}}
        status, _ = self.request("PUT", "/v1/network/policy", body, idem="reload-recover")
        self.assertEqual(status, 200)
        self.assertEqual(load_policy()["managed_ai_provider_network_access"], {"openai": True})

    def test_network_policy_replacements_are_serialized(self) -> None:
        body = {"managed_ai_provider_network_access": {"openai": True}, "allowed_network_access": {}}
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_save(policy, updated_at):  # type: ignore[no-untyped-def]
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1

        results: list[dict[str, object]] = []
        with patch("host.runtime.admin_api.state.save_network_policy", side_effect=fake_save):
            threads = [threading.Thread(target=lambda: results.append(admin_api.replace_network_policy(body))) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(results), 2)
        self.assertEqual(max_active, 1)

    def test_network_policy_replace_fails_fast_when_update_is_in_progress(self) -> None:
        body = {"managed_ai_provider_network_access": {"openai": True}, "allowed_network_access": {}}
        self.assertTrue(admin_api.NETWORK_POLICY_LOCK.acquire(blocking=False))
        self.addCleanup(admin_api.NETWORK_POLICY_LOCK.release)

        with patch("host.runtime.admin_api.NETWORK_POLICY_LOCK_TIMEOUT_SECONDS", 0):
            with self.assertRaises(admin_api.ApiError) as error:
                admin_api.replace_network_policy(body)

        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)

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
            self.request("POST", "/v1/tasks", {"input_message": "a", "thread_id": "q1", "agent_runtime": "codex"}, idem="cap-1")
            self.request("POST", "/v1/tasks", {"input_message": "b", "thread_id": "q2", "agent_runtime": "codex"}, idem="cap-2")
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("POST", "/v1/tasks", {"input_message": "c", "thread_id": "q3", "agent_runtime": "codex"}, idem="cap-3")
            self.assertEqual(error.exception.code, 409)
            # Cancelling a queued task frees a slot.
            self.request("POST", "/v1/tasks/task_1/cancel", idem="cap-cancel")
            _, body = self.request("POST", "/v1/tasks", {"input_message": "c", "thread_id": "q3", "agent_runtime": "codex"}, idem="cap-4")
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
            self.request("POST", "/v1/tasks/task_1/steer", {"steer_message": "s1"}, idem="steer-1")
            self.request("POST", "/v1/tasks/task_1/steer", {"steer_message": "s2"}, idem="steer-2")
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("POST", "/v1/tasks/task_1/steer", {"steer_message": "s3"}, idem="steer-3")
            self.assertEqual(error.exception.code, 409)
            # The queue drains as the worker delivers; a slot frees up.
            state = load_state()
            state["tasks"][0]["steer_messages"].pop(0)
            save_state(state)
            _, body = self.request("POST", "/v1/tasks/task_1/steer", {"steer_message": "s3"}, idem="steer-4")
        self.assertEqual(body["status"], "accepted")
        self.assertEqual(load_state()["tasks"][0]["steer_messages"], ["s2", "s3"])

    def test_idempotency_replay_does_not_re_execute_completed_request(self) -> None:
        _, first = self.request("POST", "/v1/tasks", {"input_message": "once", "thread_id": "t1", "agent_runtime": "codex"}, idem="dup")
        _, replay = self.request("POST", "/v1/tasks", {"input_message": "once", "thread_id": "t1", "agent_runtime": "codex"}, idem="dup")
        self.assertEqual(first["task_id"], replay["task_id"])
        _, listed = self.request("GET", "/v1/tasks")
        self.assertEqual(len(listed["tasks"]), 1)

    def test_idempotency_expired_key_re_executes(self) -> None:
        _, first = self.request("POST", "/v1/tasks", {"input_message": "stale", "thread_id": "t1", "agent_runtime": "codex"}, idem="aged")
        # Age the stored entry beyond retention.
        admin_api.IDEMPOTENCY_ENTRIES["aged"]["stored_at"] -= admin_api.IDEMPOTENCY_RETENTION_SECONDS + 1
        _, second = self.request("POST", "/v1/tasks", {"input_message": "stale", "thread_id": "t1", "agent_runtime": "codex"}, idem="aged")
        self.assertNotEqual(first["task_id"], second["task_id"])

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
            "expires_at": "2026-06-08T00:10:00Z",
        }
        save_state(state)
        with patch(
            "host.runtime.orchestrator.codex_app_server.account_status",
            return_value=("active", None, "acct_smoke"),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "active")
        self.assertIsNone(load_state().get("codex_oauth"))
        self.assertEqual(read_openai_account().get("account_id"), "acct_smoke")
        self.assertEqual(read_proxy_openai_account_id(), "acct_smoke")

    def test_runtime_expiry_clears_openai_account_id(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "active"
        save_state(state)
        save_openai_account({"account_id": "acct_smoke"})

        with patch(
            "host.runtime.orchestrator.codex_app_server.account_status",
            return_value=("awaiting_login", None, None),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertIsNone(read_openai_account().get("account_id"))
        self.assertIsNone(read_proxy_openai_account_id())

    def test_runtime_expiry_clears_claude_account(self) -> None:
        save_policy(
            {
                "managed_ai_provider_network_access": {"claude": True},
                "allowed_network_access": {},
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

        self.assertEqual(read_claude_account(), {})
        self.assertEqual(read_proxy_claude_account(), {})

    def test_active_claude_runtime_refresh_updates_token_hash(self) -> None:
        save_policy(
            {
                "managed_ai_provider_network_access": {"claude": True},
                "allowed_network_access": {},
            },
            "2026-06-08T00:00:01Z",
        )
        state = load_state()
        state["agent_runtime_statuses"]["claude_code"]["status"] = "active"
        save_state(state)
        save_claude_account({"account_id": "acct_smoke", "organization_id": "org_smoke", "access_token_sha256": "0" * 64})

        with patch(
            "host.runtime.orchestrator.claude_code.account_status",
            return_value=(
                "active",
                None,
                {"account_id": "acct_smoke", "organization_id": "org_smoke", "access_token_sha256": "1" * 64},
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "active")

        self.assertEqual(read_claude_account()["access_token_sha256"], "1" * 64)
        self.assertEqual(read_proxy_claude_account()["access_token_sha256"], "1" * 64)

    def test_agent_accounts_return_both_runtime_statuses(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "active"
        state["agent_runtime_statuses"]["claude_code"]["status"] = "awaiting_login"
        save_state(state)
        save_openai_account(
            {
                "account_id": "acct_smoke",
                "email": "codex@example.com",
                "planType": "pro",
                "type": "chatgpt",
                "codex_usage": {
                    "last_checked_at": "2026-06-29T23:10:00Z",
                    "rate_limits": {
                        "primary": {"used_percent": 8, "window_duration_mins": 300, "resets_at": 1782788897},
                        "secondary": {"used_percent": 11, "window_duration_mins": 10080, "resets_at": 1783296254},
                        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                    },
                    "access_token": "hidden",
                },
            }
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

    def test_agent_accounts_normalize_exact_codex_usage_fields(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "active"
        save_state(state)
        save_openai_account(
            {
                "account_id": "acct_smoke",
                "planType": "pro",
                "codex_usage": {
                    "last_checked_at": "2026-06-29T23:10:00Z",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 8,
                            "window_duration_mins": 300,
                            "resets_at": 1782788897,
                            "unknown": "dropped",
                        },
                        "secondary": {
                            "used_percent": 11,
                            "window_duration_mins": 10080,
                            "resets_at": 1783296254,
                        },
                        "credits": {"has_credits": False, "unlimited": False, "balance": "0", "unknown": "dropped"},
                        "unknown": "dropped",
                    },
                    "unknown": "dropped",
                },
            }
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
        save_claude_account(
            {
                "account_id": "acct_smoke",
                "organization_id": "org_smoke",
                "email": "smoke@example.com",
                "plan_type": "pro",
                "claude_usage": {
                    "current_session_used_percent": 0,
                    "weekly_used_percent": 0,
                    "weekly_resets_at_text": "Jul 3, 3:59pm (UTC)",
                    "last_checked_at": "2026-06-29T23:10:00Z",
                },
                "access_token_sha256": "f" * 64,
            }
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
                            "weekly_used_percent": 0,
                            "weekly_resets_at_text": "Jul 3, 3:59pm (UTC)",
                            "last_checked_at": "2026-06-29T23:10:00Z",
                        },
                    },
                ]
            },
        )

    def test_agent_account_endpoint_rejects_runtime_filter(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "active"
        save_state(state)
        save_openai_account({"account_id": "acct_smoke"})

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
        save_policy({"managed_ai_provider_network_access": {}, "allowed_network_access": {}}, "t")
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
                    self.request("POST", path, idem=f"disabled-{path.rsplit('/', 1)[-1]}")
                self.assertEqual(error.exception.code, 409)

    def test_current_oauth_rejects_disabled_provider_even_with_stale_oauth_state(self) -> None:
        save_policy({"managed_ai_provider_network_access": {}, "allowed_network_access": {}}, "t")
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "awaiting_login"
        state["agent_runtime_statuses"]["claude_code"]["status"] = "awaiting_login"
        state["codex_oauth"] = {
            "status": "awaiting_login",
            "device_code": "CODE",
            "login_id": "login-1",
            "login_url": "https://auth.openai.com/device",
            "expires_at": "2026-06-08T00:10:00Z",
        }
        state["claude_oauth"] = {
            "status": "awaiting_code",
            "login_url": "https://claude.com/cai/oauth/authorize",
            "expires_at": "2026-06-08T00:10:00Z",
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
        save_policy({"managed_ai_provider_network_access": {}, "allowed_network_access": {}}, "t")

        with (
            patch(
                "host.runtime.admin_api.claude_code.complete_oauth_login",
                side_effect=AssertionError("disabled Claude provider must not complete OAuth"),
            ),
            self.assertRaises(admin_api.ApiError) as error,
        ):
            admin_api.complete_claude_oauth_login({"code": "browser-code"})

        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)

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
            {"managed_ai_provider_network_access": {"claude": True}, "allowed_network_access": {}},
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

    def test_prune_state_trims_finished_tasks_and_idempotency(self) -> None:
        # The production caps are six figures now; the trimming behavior is
        # pinned with small patched limits so the test stays fast.
        state = load_state()
        finished_limit, map_limit = 8, 6
        # One queued task plus many finished ones beyond the history limit.
        finished = [
            {"task_id": f"task_{n}", "status": "completed", "agent_runtime": "codex",
             "thread_id": f"t{n}", "input_message": "x",
             "steer_messages": [], "created_at": "t", "updated_at": "t"}
            for n in range(1, finished_limit + 6)
        ]
        queued = {"task_id": "task_9000", "status": "queued", "agent_runtime": "codex",
                  "thread_id": "t9000", "input_message": "live",
                  "steer_messages": [], "created_at": "t", "updated_at": "t"}
        state["tasks"] = finished + [queued]
        with patch.object(admin_api, "IDEMPOTENCY_ENTRY_LIMIT", 2), patch.object(
            admin_api, "FINISHED_TASK_LIMIT", finished_limit
        ), patch.object(admin_api, "THREAD_MAP_LIMIT", map_limit):
            admin_api.IDEMPOTENCY_ENTRIES.update({
                "old": {"method": "POST", "path": "/v1/tasks", "response": {}, "stored_at": time.time() - 20},
                "fresh": {"method": "POST", "path": "/v1/tasks", "response": {}, "stored_at": time.time()},
                "newer": {"method": "POST", "path": "/v1/tasks", "response": {}, "stored_at": time.time() + 1},
                "stale": {"method": "POST", "path": "/v1/tasks", "response": {},
                          "stored_at": time.time() - admin_api.IDEMPOTENCY_RETENTION_SECONDS - 1},
            })
            state["codex_threads"] = {
                f"chat-{n}": {"codex_thread_id": f"thread_{n}", "last_used_at": f"2026-06-08T{n // 60:02d}:{n % 60:02d}:00Z"}
                for n in range(map_limit + 5)
            }
            state["claude_sessions"] = {
                f"chat-{n}": {"session_id": f"session_{n}", "last_used_at": f"2026-06-09T{n // 60:02d}:{n % 60:02d}:00Z"}
                for n in range(map_limit + 5)
            }
            save_state(state)

            admin_api.prune_state()

        pruned = load_state()
        self.assertEqual(set(admin_api.IDEMPOTENCY_ENTRIES), {"fresh", "newer"})
        # The oldest thread mappings are dropped, the most recently used kept.
        self.assertEqual(len(pruned["codex_threads"]), map_limit)
        self.assertNotIn("chat-0", pruned["codex_threads"])
        self.assertIn(f"chat-{map_limit + 4}", pruned["codex_threads"])
        self.assertEqual(len(pruned["claude_sessions"]), map_limit)
        self.assertNotIn("chat-0", pruned["claude_sessions"])
        self.assertIn(f"chat-{map_limit + 4}", pruned["claude_sessions"])
        statuses = [t["status"] for t in pruned["tasks"]]
        self.assertIn("queued", statuses)  # active task always kept
        self.assertEqual(statuses.count("completed"), finished_limit)
        # Oldest finished tasks dropped, newest kept.
        kept_ids = {t["task_id"] for t in pruned["tasks"]}
        self.assertNotIn("task_1", kept_ids)
        self.assertIn(f"task_{finished_limit + 5}", kept_ids)

    def test_idempotency_entries_are_capped_on_mutation_path(self) -> None:
        admin_api.IDEMPOTENCY_ENTRIES.update({
            "old": {"method": "POST", "path": "/v1/tasks", "response": {}, "stored_at": time.time() - 20},
            "middle": {"method": "POST", "path": "/v1/tasks", "response": {}, "stored_at": time.time() - 10},
        })

        with patch.object(admin_api, "IDEMPOTENCY_ENTRY_LIMIT", 2):
            _, body = self.request("POST", "/v1/tasks", {"input_message": "new", "thread_id": "t1", "agent_runtime": "codex"}, idem="new")

        self.assertEqual(body["status"], "queued")
        self.assertEqual(set(admin_api.IDEMPOTENCY_ENTRIES), {"middle", "new"})

    def test_network_events_are_read_from_the_database_with_since_paging(self) -> None:
        # Network events live in the database now (the proxy writes them under
        # its own role); the admin API pages them by seq.
        for index in range(7):
            append_network_event("https", "GET", "example.com", 443, f"/p{index}", "", index % 2 == 0)

        _, body = self.request("GET", "/v1/network/events")
        seqs = [event["seq"] for event in body["events"]]
        self.assertEqual(len(seqs), 5)
        self.assertEqual(seqs, sorted(seqs))
        _, rest = self.request("GET", f"/v1/network/events?since={seqs[-1]}")
        self.assertEqual(len(rest["events"]), 2)
        self.assertTrue(all(event["seq"] > seqs[-1] for event in rest["events"]))
        self.assertEqual({event["decision"] for event in body["events"] + rest["events"]}, {"allowed", "denied"})

    def test_reboot_uses_privileged_helper(self) -> None:
        succeeded = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("host.runtime.admin_api.subprocess.run", return_value=succeeded) as run:
            _, body = self.request("POST", "/v1/host-runtime/reboot", idem="reboot-1")
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
                    "managed_ai_provider_network_access": {"openai": True},
                    "allowed_network_access": {
                        "api.example.com": {"allow_http_methods": ["GET"]},
                    },
                }
            ).to_json(),
            "2026-06-08T00:00:03Z",
        )
        _, body = self.request("GET", "/v1/network/policy")
        self.assertEqual(body["network_controls"]["managed_ai_provider_network_access"], {"openai": True})
        self.assertEqual(
            body["network_controls"]["allowed_network_access"],
            {"api.example.com": {"allow_http_methods": ["GET"]}},
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
            _, body = self.request("POST", "/v1/tasks/task_1/kill", idem="kill-1")
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
        _, queued = self.request("POST", "/v1/tasks", {"input_message": "waiting", "thread_id": "t1", "agent_runtime": "codex"}, idem="kill-q")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", f"/v1/tasks/{queued['task_id']}/kill", idem="kill-q2")
        self.assertEqual(error.exception.code, 409)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", "/v1/tasks/task_999999/kill", idem="kill-404")
        self.assertEqual(error.exception.code, 404)

    def test_create_task_requires_a_valid_thread_id(self) -> None:
        for index, bad in enumerate((None, "", "has space", "bad/slash", "x" * 65)):
            body: dict[str, object] = {"input_message": "hello"}
            if bad is not None:
                body["thread_id"] = bad
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("POST", "/v1/tasks", body, idem=f"thread-bad-{index}")
            self.assertEqual(error.exception.code, 400)
        _, task = self.request(
            "POST", "/v1/tasks", {"input_message": "hello", "thread_id": "Chat_01-a", "agent_runtime": "codex"}, idem="thread-ok"
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
        # since-based event pagination.
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
        self.assertEqual([event["seq"] for event in body["events"]], [first, second])

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
        admin_api.IDEMPOTENCY_ENTRIES["key-1"] = {
            "method": "POST", "path": "/v1/tasks", "in_flight": True, "stored_at": time.time()
        }

        # The port bind is the single-instance gate: a second instance must die
        # there without failing the live instance's running task or dropping
        # its in-flight idempotency reservations (which live in the live
        # process's memory, out of any other instance's reach). The service
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
        self.assertIn("key-1", admin_api.IDEMPOTENCY_ENTRIES)



if __name__ == "__main__":
    unittest.main()
