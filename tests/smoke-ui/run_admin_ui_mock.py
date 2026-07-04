#!/usr/bin/env python3
"""Serve the admin UI against a deterministic local mock backend.

This is for browser/UI development only. It does not import the real admin API
handler because the real handler reads host state and invokes privileged helper
paths. The mock keeps just enough in-memory state to exercise the single-page
admin UI at ``host/runtime/admin_ui.html``, and ships with seeded history plus
time-based task progression so the UI looks and behaves like a live host.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from host.config import ConfigError, parse_network_controls

RUNTIME_DIR = REPO_ROOT / "host/runtime"
VERSION = (REPO_ROOT / "VERSION").read_text().strip()
UI_ASSETS = {
    "/": ("admin_ui.html", "text/html; charset=utf-8"),
    "/admin_ui.css": ("admin_ui.css", "text/css; charset=utf-8"),
    "/admin_ui.js": ("admin_ui.js", "application/javascript; charset=utf-8"),
}
PASSWORD = "dev"
TASK_RE = re.compile(r"^/v1/tasks/([^/]+)(?:/(steer|cancel|kill|events))?$")
THREAD_TASKS_RE = re.compile(r"^/v1/threads/([^/]+)/tasks$")
RUNTIMES = ("codex", "claude_code")
PROVIDER_BY_RUNTIME = {"codex": "openai", "claude_code": "claude"}
MAX_RUNNING_PER_RUNTIME = 3
MAX_RUNNING_TOTAL = 6

# Timed progression script for running tasks: (fraction of duration, message).
PROGRESS_SCRIPT = [
    (0.2, "Reading the workspace and planning the change."),
    (0.55, "Applying edits and running the relevant checks."),
    (0.85, "Checks passed; writing up the result."),
]
# Provider traffic emitted alongside progress milestones, keyed by runtime.
PROVIDER_TRAFFIC = {
    "codex": ("api.openai.com", "/v1/responses"),
    "claude_code": ("api.anthropic.com", "/v1/messages"),
}


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class MockState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    next_task_number: int = 1
    next_agent_event_seq: int = 1
    next_network_event_seq: int = 1
    agent_events: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    task_events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    network_events: list[dict[str, Any]] = field(default_factory=list)
    policy: dict[str, Any] = field(
        default_factory=lambda: {
            "managed_ai_provider_network_access": {},
            "allowed_network_access": {},
        }
    )
    logged_in: dict[str, bool] = field(default_factory=lambda: {"codex": False, "claude_code": False})
    codex_oauth: dict[str, str] = field(default_factory=dict)
    claude_oauth: dict[str, str] = field(default_factory=dict)
    reboot_requested: bool = False

    def now(self) -> str:
        return iso(datetime.now(timezone.utc))

    def add_agent_event(
        self, event_type: str, task_id: str | None, payload: dict[str, Any], timestamp: str | None = None
    ) -> None:
        event = {
            "seq": self.next_agent_event_seq,
            "timestamp": timestamp or self.now(),
            "event_type": event_type,
            "task_id": task_id,
            "payload": payload,
        }
        self.next_agent_event_seq += 1
        self.agent_events.append(event)
        if task_id:
            self.task_events.setdefault(task_id, []).append(event)

    def add_network_event(self, method: str, host: str, path: str, decision: str, timestamp: str | None = None) -> None:
        self.network_events.append(
            {
                "seq": self.next_network_event_seq,
                "timestamp": timestamp or self.now(),
                "method": method,
                "protocol": "https",
                "host": host,
                "path": path,
                "decision": decision,
            }
        )
        self.next_network_event_seq += 1

    def public_task(self, task: dict[str, Any], queue_position: int | None = None) -> dict[str, Any]:
        result = {key: value for key, value in task.items() if not key.startswith("_")}
        if queue_position is not None:
            result["queue_position"] = queue_position
        return result

    def runtime_status(self, runtime: str) -> str:
        provider = PROVIDER_BY_RUNTIME[runtime]
        managed = self.policy.get("managed_ai_provider_network_access", {})
        if not isinstance(managed, dict) or managed.get(provider) is not True:
            return "deactivated"
        return "active" if self.logged_in.get(runtime) else "awaiting_login"


STATE = MockState()


def iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ago(minutes: float) -> str:
    return iso(datetime.now(timezone.utc) - timedelta(minutes=minutes))


def seed_state() -> None:
    """Populate history that resembles a host that has been in use for a while.

    The seeded story: the operator ran a few threads earlier today, one deploy
    task failed against the network policy, and provider access was later
    switched off — which is why both runtimes start out deactivated.
    """
    seed_tasks = [
        {
            "task_id": "task_1",
            "thread_id": "main",
            "agent_runtime": "codex",
            "status": "completed",
            "input_message": "Review the failing deploy workflow in acme/infra and summarize the root cause.",
            "output_message": (
                "Root cause: the deploy job pins actions/setup-node to a yanked version, so the runner "
                "falls back to Node 16 and the build script fails on `Array.prototype.toSorted`.\n"
                "Recommend pinning setup-node to the current v4 commit SHA and adding a node --version guard."
            ),
            "created_min": 205,
            "started_min": 204,
            "completed_min": 197,
        },
        {
            "task_id": "task_2",
            "thread_id": "main",
            "agent_runtime": "codex",
            "status": "completed",
            "input_message": "Apply that fix on a branch and open a PR with the workflow change.",
            "output_message": (
                "Opened acme/infra#128 pinning actions/setup-node to the v4 commit SHA with a version guard "
                "step. CI is green on the branch; requested review from @platform-team."
            ),
            "created_min": 191,
            "started_min": 190,
            "completed_min": 178,
        },
        {
            "task_id": "task_3",
            "thread_id": "website-redesign",
            "agent_runtime": "claude_code",
            "status": "completed",
            "input_message": "Audit the marketing site for mobile layout issues and list concrete fixes.",
            "output_message": (
                "Found 6 issues: hero overflows at <390px, nav does not collapse, pricing table needs "
                "horizontal scroll, two tap targets under 40px, CLS from unsized images, and a fixed "
                "footer covering CTAs. Wrote fixes to workspace/acme-web/notes in priority order."
            ),
            "created_min": 96,
            "started_min": 95,
            "completed_min": 84,
        },
        {
            "task_id": "task_4",
            "thread_id": "website-redesign",
            "agent_runtime": "claude_code",
            "status": "failed",
            "input_message": "Push the responsive fixes to staging and verify the deploy.",
            "error_message": "network access to deploy.acme.dev denied by policy (no allowed_network_access rule)",
            "created_min": 81,
            "started_min": 80,
            "completed_min": 78,
        },
        {
            "task_id": "task_5",
            "thread_id": "dependency-audit",
            "agent_runtime": "codex",
            "status": "cancelled",
            "input_message": "Upgrade all npm dependencies in acme-web and note any breaking changes.",
            "created_min": 1510,
            "started_min": None,
            "completed_min": 1490,
        },
    ]
    for spec in seed_tasks:
        task = {
            "task_id": spec["task_id"],
            "status": spec["status"],
            "agent_runtime": spec["agent_runtime"],
            "thread_id": spec["thread_id"],
            "input_message": spec["input_message"],
            "created_at": ago(spec["created_min"]),
            "updated_at": ago(spec["completed_min"]),
        }
        if spec.get("output_message"):
            task["output_message"] = spec["output_message"]
        if spec.get("error_message"):
            task["error_message"] = spec["error_message"]
        if spec["started_min"] is not None:
            task["started_at"] = ago(spec["started_min"])
        if spec["status"] in {"completed", "failed"}:
            task["completed_at"] = ago(spec["completed_min"])
        STATE.tasks.append(task)

        task_id = spec["task_id"]
        STATE.add_agent_event(
            "task.created", task_id, {"message": spec["input_message"], "source": "user"}, ago(spec["created_min"])
        )
        if spec["started_min"] is not None:
            STATE.add_agent_event("task.started", task_id, {}, ago(spec["started_min"]))
            STATE.add_agent_event(
                "task.message",
                task_id,
                {"message": "Reading the workspace and planning the change.", "source": "agent"},
                ago(spec["started_min"] - 1),
            )
        if spec["status"] == "completed":
            STATE.add_agent_event(
                "task.message", task_id, {"message": spec["output_message"], "source": "agent"}, ago(spec["completed_min"])
            )
            STATE.add_agent_event("task.completed", task_id, {}, ago(spec["completed_min"]))
        elif spec["status"] == "failed":
            STATE.add_agent_event(
                "task.failed", task_id, {"error_message": spec["error_message"]}, ago(spec["completed_min"])
            )
        elif spec["status"] == "cancelled":
            STATE.add_agent_event("task.cancelled", task_id, {}, ago(spec["completed_min"]))
    STATE.next_task_number = len(seed_tasks) + 1

    # Providers were switched off ~70 minutes ago; both runtimes deactivated.
    for runtime in RUNTIMES:
        STATE.add_agent_event("agent_runtime.deactivated", None, {"agent_runtime": runtime}, ago(70))

    for minutes, method, host, path, decision in [
        (204, "POST", "api.openai.com", "/v1/responses", "allowed"),
        (203, "GET", "api.github.com", "/repos/acme/infra/actions/runs?status=failure", "allowed"),
        (201, "GET", "raw.githubusercontent.com", "/acme/infra/main/.github/workflows/deploy.yml", "allowed"),
        (198, "POST", "api.openai.com", "/v1/responses", "allowed"),
        (190, "GET", "api.github.com", "/repos/acme/infra/git/ref/heads/main", "allowed"),
        (186, "POST", "api.github.com", "/repos/acme/infra/pulls", "allowed"),
        (184, "POST", "api.openai.com", "/v1/responses", "allowed"),
        (95, "POST", "api.anthropic.com", "/v1/messages", "allowed"),
        (92, "GET", "registry.npmjs.org", "/postcss", "allowed"),
        (90, "GET", "telemetry.acme-analytics.io", "/v2/collect", "denied"),
        (86, "POST", "api.anthropic.com", "/v1/messages", "allowed"),
        (80, "POST", "deploy.acme.dev", "/api/releases", "denied"),
        (79, "POST", "deploy.acme.dev", "/api/releases", "denied"),
        (78, "POST", "api.anthropic.com", "/v1/messages", "allowed"),
    ]:
        STATE.add_network_event(method, host, path, decision, ago(minutes))


class Handler(BaseHTTPRequestHandler):
    server_version = "TrustyClawMock/0.2"

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path in UI_ASSETS:
                filename, content_type = UI_ASSETS[parsed.path]
                data = (RUNTIME_DIR / filename).read_bytes()
                self._send(HTTPStatus.OK, data, content_type)
                return
            if method == "GET" and parsed.path == "/favicon.ico":
                self._send(HTTPStatus.NO_CONTENT, b"", "text/plain")
                return
            self._authenticate()
            response = route(method, parsed.path, parse_qs(parsed.query), self._read_body())
            self._send_json(HTTPStatus.OK, response)
        except ApiError as exc:
            self._send_json(exc.status, {"error": {"message": exc.message}})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": str(exc)}})

    def _authenticate(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {PASSWORD}":
            raise ApiError(HTTPStatus.UNAUTHORIZED, "missing or invalid admin password")

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return None
        return json.loads(self.rfile.read(length))

    def _send_json(self, status: HTTPStatus, data: dict[str, Any]) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _send(self, status: HTTPStatus, data: bytes, content_type: str) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def route(method: str, path: str, query: dict[str, list[str]], body: Any) -> dict[str, Any]:
    if method == "GET" and path == "/v1/health":
        return health()
    if method == "GET" and path == "/v1/agent-runtime/status":
        return agent_runtime_status()
    if method == "GET" and path == "/v1/agent-runtime/account":
        return agent_accounts()
    if path == "/v1/agent-runtime/codex-oauth-login":
        return oauth("codex", method)
    if path == "/v1/agent-runtime/claude-oauth-login":
        return oauth("claude_code", method)
    if path == "/v1/agent-runtime/claude-oauth-login/complete" and method == "POST":
        return complete_claude_oauth(body)
    if path == "/v1/tasks":
        if method == "GET":
            return list_tasks()
        if method == "POST":
            return create_task(body)
    task_match = TASK_RE.fullmatch(path)
    if task_match:
        return task_route(method, task_match.group(1), task_match.group(2), query, body)
    if method == "GET" and path == "/v1/threads":
        return list_threads()
    thread_match = THREAD_TASKS_RE.fullmatch(path)
    if method == "GET" and thread_match:
        return list_thread_tasks(unquote(thread_match.group(1)))
    if method == "GET" and path == "/v1/events":
        return {"events": agent_events(since(query))}
    if path == "/v1/network/policy":
        if method == "GET":
            return {"network_controls": STATE.policy}
        if method == "PUT":
            return replace_policy(body)
    if method == "GET" and path == "/v1/network/events":
        return {"events": network_events(since(query))}
    if method == "GET" and path == "/v1/agent-files":
        return list_agent_files(one(query, "path") or "/")
    if method == "GET" and path == "/v1/agent-files/read":
        return read_agent_file(one(query, "path") or "/")
    if method == "POST" and path == "/v1/host-runtime/reboot":
        STATE.reboot_requested = True
        return {"status": "accepted"}
    raise ApiError(HTTPStatus.NOT_FOUND, "route not found")


def health() -> dict[str, Any]:
    with STATE.lock:
        progress_running_tasks_locked()
        start_queued_tasks_locked()
        runtime = agent_runtime_status_locked()
        running = sum(1 for task in STATE.tasks if task["status"] == "running")
    # Gentle drift so the dashboard feels alive; busier while tasks run.
    wave = math.sin(time.time() / 47.0)
    cpu = round(6.5 + 3.5 * wave + 24.0 * min(running, 2), 1)
    memory_used = int((0.92 + 0.05 * wave + 0.35 * min(running, 2)) * 1024**3)
    gib = 1024**3
    return {
        "status": "ok",
        "agent_name": "trustyclaw-mock",
        "version": {"status": "ok", "runtime": VERSION, "state": VERSION},
        "agent_runtime": runtime,
        "network_controls": {"status": "active"},
        "host_runtime": {
            "cpu": {"usage_percent": cpu},
            "memory": {"used_bytes": memory_used, "total_bytes": 2 * gib},
            "filesystem": {
                "used_bytes": int(11.2 * gib),
                "total_bytes": 30 * gib,
                "mounts": {
                    "root": {"used_bytes": int(11.2 * gib), "total_bytes": 30 * gib},
                    "admin": {"used_bytes": int(0.4 * gib), "total_bytes": 8 * gib},
                    "agent": {"used_bytes": int(2.6 * gib), "total_bytes": 8 * gib},
                },
            },
            "swap": {"used_bytes": int(0.5 * gib), "allocated_bytes": 6 * gib},
        },
    }


def agent_runtime_status() -> dict[str, Any]:
    with STATE.lock:
        progress_running_tasks_locked()
        start_queued_tasks_locked()
        return agent_runtime_status_locked()


def agent_runtime_status_locked() -> dict[str, Any]:
    active = {runtime: [] for runtime in RUNTIMES}
    for task in STATE.tasks:
        if task["status"] == "running":
            active[task["agent_runtime"]].append(task["task_id"])
    return {
        "runtimes": [
            {"type": runtime, "status": STATE.runtime_status(runtime), "active_task_ids": active[runtime]}
            for runtime in RUNTIMES
        ]
    }


def agent_accounts() -> dict[str, Any]:
    with STATE.lock:
        checked_at = STATE.now()
        accounts: list[dict[str, Any]] = []
        for runtime in RUNTIMES:
            status = STATE.runtime_status(runtime)
            if runtime == "codex":
                account = {"agent_runtime": runtime, "provider": "openai", "status": status}
                if status == "active":
                    account.update(
                        {
                            "account_id": "acct_mock_openai",
                            "email": "akshay@infiloop.io",
                            "plan_type": "pro",
                            "codex_usage": {
                                "last_checked_at": checked_at,
                                "rate_limits": {
                                    "primary": {
                                        "used_percent": 60,
                                        "window_duration_mins": 300,
                                        "resets_at": 1782788896,
                                    },
                                    "secondary": {
                                        "used_percent": 20,
                                        "window_duration_mins": 10080,
                                        "resets_at": 1783296254,
                                    },
                                    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                                },
                            },
                        }
                    )
            else:
                account = {"agent_runtime": runtime, "provider": "claude", "status": status}
                if status == "active":
                    account.update(
                        {
                            "account_id": "acct_mock_claude",
                            "email": "claude@example.invalid",
                            "plan_type": "max",
                            "claude_usage": {
                                "current_session_used_percent": 14,
                                "weekly_used_percent": 31,
                                "weekly_resets_at_text": "Jul 7, 3:59pm (UTC)",
                                "last_checked_at": checked_at,
                            },
                        }
                    )
            accounts.append(account)
        return {"accounts": accounts}


def oauth(runtime: str, method: str) -> dict[str, str]:
    now = STATE.now()
    expires = iso(datetime.now(timezone.utc) + timedelta(minutes=15))
    with STATE.lock:
        status = STATE.runtime_status(runtime)
        if status == "deactivated":
            provider = "OpenAI" if runtime == "codex" else "Claude"
            raise ApiError(HTTPStatus.CONFLICT, f"{runtime_label(runtime)} OAuth login is unavailable while {provider} provider access is disabled")
        if status != "awaiting_login":
            raise ApiError(HTTPStatus.CONFLICT, f"{runtime_label(runtime)} OAuth login is only available while awaiting_login")
    if runtime == "codex":
        if method not in {"GET", "POST"}:
            raise ApiError(HTTPStatus.NOT_FOUND, "route not found")
        with STATE.lock:
            if method == "GET" and not STATE.codex_oauth:
                raise ApiError(HTTPStatus.NOT_FOUND, "Codex OAuth login has not been started")
            if not STATE.codex_oauth:
                STATE.codex_oauth = {
                    "status": "awaiting_login",
                    "device_code": "MOCK-CODEX",
                    "login_url": "https://auth.openai.com/activate",
                    "expires_at": expires,
                }
            if method == "POST":
                # The real Codex device flow completes out of band after the
                # operator enters the code. The mock flips active here so local
                # UI smoke can inspect the active account surface. A background
                # GET may already have created the pending device-code record.
                STATE.logged_in["codex"] = True
                STATE.add_agent_event("agent_runtime.login_completed", None, {"agent_runtime": "codex"})
                STATE.add_agent_event("agent_runtime.active", None, {"agent_runtime": "codex"})
            return dict(STATE.codex_oauth)
    if method not in {"GET", "POST"}:
        raise ApiError(HTTPStatus.NOT_FOUND, "route not found")
    with STATE.lock:
        if method == "GET" and not STATE.claude_oauth:
            raise ApiError(HTTPStatus.NOT_FOUND, "Claude OAuth login has not been started")
        if not STATE.claude_oauth:
            STATE.claude_oauth = {
                "status": "awaiting_code",
                "login_url": "https://claude.com/cai/oauth/authorize?client=trustyclaw-mock",
                "expires_at": expires,
            }
        return dict(STATE.claude_oauth)


def complete_claude_oauth(body: Any) -> dict[str, str]:
    if not isinstance(body, dict) or not isinstance(body.get("code"), str) or not body["code"].strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "code must be a non-empty string")
    with STATE.lock:
        if STATE.runtime_status("claude_code") != "awaiting_login":
            raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is only available while awaiting_login")
        STATE.logged_in["claude_code"] = True
        STATE.claude_oauth = {}
        STATE.add_agent_event("agent_runtime.login_completed", None, {"agent_runtime": "claude_code"})
        STATE.add_agent_event("agent_runtime.active", None, {"agent_runtime": "claude_code"})
        start_queued_tasks_locked()
    return {"status": "accepted"}


def runtime_label(runtime: str) -> str:
    return "Codex" if runtime == "codex" else "Claude"


def create_task(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be an object")
    input_message = str(body.get("input_message", "")).strip()
    thread_id = str(body.get("thread_id", "")).strip()
    agent_runtime = str(body.get("agent_runtime", "codex"))
    if not input_message or not thread_id or agent_runtime not in {"codex", "claude_code"}:
        raise ApiError(HTTPStatus.BAD_REQUEST, "invalid task")
    with STATE.lock:
        for existing in STATE.tasks:
            if existing["thread_id"] == thread_id and existing["agent_runtime"] != agent_runtime:
                raise ApiError(HTTPStatus.CONFLICT, "thread already belongs to another agent runtime")
        task_id = f"task_{STATE.next_task_number}"
        STATE.next_task_number += 1
        now = STATE.now()
        task = {
            "task_id": task_id,
            "status": "queued",
            "agent_runtime": agent_runtime,
            "thread_id": thread_id,
            "input_message": input_message,
            "created_at": now,
            "updated_at": now,
        }
        STATE.tasks.append(task)
        STATE.add_agent_event("task.created", task_id, {"message": input_message, "source": "user"})
        start_queued_tasks_locked()
        return STATE.public_task(task, queue_position=queue_position_locked(task))


def active_tasks() -> list[dict[str, Any]]:
    return [task for task in STATE.tasks if task["status"] in {"queued", "running"}]


def queue_position_locked(task: dict[str, Any]) -> int:
    """Mirror AdminAPI.md: running tasks report 0; queued tasks count from 1."""
    if task["status"] == "running":
        return 0
    queued = [candidate for candidate in STATE.tasks if candidate["status"] == "queued"]
    return queued.index(task) + 1


def list_tasks() -> dict[str, Any]:
    with STATE.lock:
        progress_running_tasks_locked()
        start_queued_tasks_locked()
        return {"tasks": [STATE.public_task(task, queue_position_locked(task)) for task in active_tasks()]}


def task_route(
    method: str, task_id: str, action: str | None, query: dict[str, list[str]], body: Any
) -> dict[str, Any]:
    with STATE.lock:
        progress_running_tasks_locked()
        task = find_task(task_id)
        if action is None and method == "GET":
            return STATE.public_task(task)
        if action == "events" and method == "GET":
            return {"events": [event for event in STATE.task_events.get(task_id, []) if event["seq"] > since(query)]}
        if action == "cancel" and method == "POST":
            if task["status"] != "queued":
                raise ApiError(HTTPStatus.CONFLICT, "only queued tasks can be cancelled")
            task["status"] = "cancelled"
            task["updated_at"] = STATE.now()
            STATE.add_agent_event("task.cancelled", task_id, {})
            return {"status": "accepted"}
        if action == "kill" and method == "POST":
            if task["status"] != "running":
                raise ApiError(HTTPStatus.CONFLICT, "only running tasks can be killed")
            task["status"] = "cancelled"
            task["updated_at"] = STATE.now()
            STATE.add_agent_event("task.cancelled", task_id, {"message": "runtime process terminated by operator"})
            return {"status": "accepted"}
        if action == "steer" and method == "POST":
            if task["status"] != "running":
                raise ApiError(HTTPStatus.CONFLICT, "only running tasks can be steered")
            message = str((body or {}).get("steer_message", ""))
            STATE.add_agent_event("task.message", task_id, {"message": message, "source": "user"})
            return {"status": "accepted"}
    raise ApiError(HTTPStatus.NOT_FOUND, "task route not found")


def find_task(task_id: str) -> dict[str, Any]:
    for task in STATE.tasks:
        if task["task_id"] == task_id:
            return task
    raise ApiError(HTTPStatus.NOT_FOUND, "task not found")


def list_threads() -> dict[str, Any]:
    with STATE.lock:
        progress_running_tasks_locked()
        start_queued_tasks_locked()
        threads: dict[tuple[str, str], dict[str, Any]] = {}
        for task in STATE.tasks:
            key = (task["thread_id"], task["agent_runtime"])
            entry = threads.setdefault(
                key,
                {
                    "thread_id": task["thread_id"],
                    "agent_runtime": task["agent_runtime"],
                    "last_used_at": task["updated_at"],
                    "task_count": 0,
                    "active_tasks": [],
                },
            )
            entry["last_used_at"] = max(entry["last_used_at"], task["updated_at"])
            entry["task_count"] += 1
            if task["status"] in {"queued", "running"}:
                entry["active_tasks"].append({"task_id": task["task_id"], "status": task["status"]})
        return {"threads": sorted(threads.values(), key=lambda item: item["last_used_at"], reverse=True)}


def list_thread_tasks(thread_id: str) -> dict[str, Any]:
    with STATE.lock:
        progress_running_tasks_locked()
        tasks = [STATE.public_task(task) for task in STATE.tasks if task["thread_id"] == thread_id]
        return {"tasks": list(reversed(tasks))}


def agent_events(min_seq: int) -> list[dict[str, Any]]:
    with STATE.lock:
        progress_running_tasks_locked()
        start_queued_tasks_locked()
        return [event for event in STATE.agent_events if event["seq"] > min_seq]


def network_events(min_seq: int) -> list[dict[str, Any]]:
    with STATE.lock:
        progress_running_tasks_locked()
        return [event for event in STATE.network_events if event["seq"] > min_seq]


def list_agent_files(path: str) -> dict[str, Any]:
    files = {
        "/": [
            {"name": ".claude", "path": "/.claude", "type": "directory", "modified_at": ago(180)},
            {"name": ".codex", "path": "/.codex", "type": "directory", "modified_at": ago(180)},
            {"name": ".gitconfig", "path": "/.gitconfig", "type": "file", "size_bytes": 143, "modified_at": ago(2880)},
            {"name": "AGENTS.md", "path": "/AGENTS.md", "type": "file", "size_bytes": 486, "modified_at": ago(2880)},
            {"name": "workspace", "path": "/workspace", "type": "directory", "modified_at": ago(84)},
        ],
        "/.claude": [
            {"name": "settings.json", "path": "/.claude/settings.json", "type": "file", "size_bytes": 96, "modified_at": ago(180)},
        ],
        "/.codex": [
            {"name": "auth.json", "path": "/.codex/auth.json", "type": "file", "size_bytes": 18, "modified_at": ago(180)},
            {"name": "config.toml", "path": "/.codex/config.toml", "type": "file", "size_bytes": 74, "modified_at": ago(180)},
        ],
        "/workspace": [
            {"name": "acme-web", "path": "/workspace/acme-web", "type": "directory", "modified_at": ago(84)},
            {
                "name": 'bad" onclick="window.__xss=1" x=".txt',
                "path": '/workspace/bad" onclick="window.__xss=1" x=".txt',
                "type": "file",
                "size_bytes": 24,
                "modified_at": ago(300),
            },
            {
                "name": '<img src=x onerror="window.__fileNameXss=1">.txt',
                "path": '/workspace/<img src=x onerror="window.__fileNameXss=1">.txt',
                "type": 'file"><img src=x onerror="window.__fileTypeXss=1">',
                "size_bytes": 72,
                "modified_at": ago(300),
            },
            {"name": "notes.txt", "path": "/workspace/notes.txt", "type": "file", "size_bytes": 512, "modified_at": ago(84)},
        ],
        "/workspace/acme-web": [
            {"name": ".git", "path": "/workspace/acme-web/.git", "type": "directory", "modified_at": ago(84)},
            {"name": "README.md", "path": "/workspace/acme-web/README.md", "type": "file", "size_bytes": 208, "modified_at": ago(2880)},
            {"name": "package.json", "path": "/workspace/acme-web/package.json", "type": "file", "size_bytes": 389, "modified_at": ago(96)},
            {"name": "src", "path": "/workspace/acme-web/src", "type": "directory", "modified_at": ago(84)},
        ],
        "/workspace/acme-web/.git": [
            {"name": "HEAD", "path": "/workspace/acme-web/.git/HEAD", "type": "file", "size_bytes": 30, "modified_at": ago(84)},
        ],
        "/workspace/acme-web/src": [
            {"name": "app.css", "path": "/workspace/acme-web/src/app.css", "type": "file", "size_bytes": 301, "modified_at": ago(84)},
            {"name": "index.ts", "path": "/workspace/acme-web/src/index.ts", "type": "file", "size_bytes": 264, "modified_at": ago(96)},
        ],
    }
    if path not in files:
        raise ApiError(HTTPStatus.NOT_FOUND, "path not found")
    return {"path": path, "entries": files[path], "truncated": False}


def read_agent_file(path: str) -> dict[str, Any]:
    contents = {
        "/.gitconfig": "[user]\n\tname = TrustyClaw Agent\n\temail = agent@trustyclaw.invalid\n[init]\n\tdefaultBranch = main\n",
        "/AGENTS.md": (
            "# Agent instructions\n\n"
            "You are running inside a TrustyClaw sandbox. All network access goes\n"
            "through the policy proxy; request additional domains from the operator\n"
            "instead of retrying blocked calls.\n\n"
            "Work under /workspace and keep commits small.\n"
        ),
        "/.claude/settings.json": '{\n  "permissions": {\n    "defaultMode": "acceptEdits"\n  }\n}\n',
        "/.codex/auth.json": '{"mock": "redacted"}\n',
        "/.codex/config.toml": 'model = "gpt-5-codex"\napproval_policy = "never"\nsandbox_mode = "danger-full-access"\n',
        '/workspace/bad" onclick="window.__xss=1" x=".txt': "quote-bearing mock file\n",
        '/workspace/<img src=x onerror="window.__fileNameXss=1">.txt': (
            '<script>window.__fileContentXss=1</script>'
            '<img src=x onerror="window.__fileContentImageXss=1">'
            "Mock unsafe-looking file contents\n"
        ),
        "/workspace/notes.txt": (
            "Mobile audit fixes, in priority order:\n"
            "1. Wrap pricing table in an overflow-x container\n"
            "2. Collapse nav below 768px\n"
            "3. Set width/height on hero images (CLS)\n"
            "4. Bump tap targets to 44px\n"
            "5. Unfix footer on small screens\n"
            "6. Clamp hero heading with fluid type\n"
        ),
        "/workspace/acme-web/README.md": (
            "# acme-web\n\nMarketing site for Acme. `npm install && npm run dev`, then open\n"
            "http://localhost:5173. Deploys to staging from the main branch.\n"
        ),
        "/workspace/acme-web/package.json": (
            '{\n  "name": "acme-web",\n  "private": true,\n  "version": "1.4.2",\n'
            '  "scripts": {\n    "dev": "vite",\n    "build": "vite build",\n    "lint": "eslint src"\n  },\n'
            '  "dependencies": {\n    "postcss": "^8.4.38",\n    "vite": "^5.2.0"\n  }\n}\n'
        ),
        "/workspace/acme-web/.git/HEAD": "ref: refs/heads/mobile-fixes\n",
        "/workspace/acme-web/src/index.ts": (
            'const nav = document.querySelector(".nav");\n'
            'document.querySelector(".nav-toggle")?.addEventListener("click", () => {\n'
            '  nav?.classList.toggle("open");\n'
            "});\n"
        ),
        "/workspace/acme-web/src/app.css": (
            ":root { --brand: #4f46e5; }\n\n"
            ".pricing { overflow-x: auto; }\n\n"
            "@media (max-width: 768px) {\n  .nav { display: none; }\n  .nav.open { display: flex; }\n}\n"
        ),
    }
    if path not in contents:
        raise ApiError(HTTPStatus.BAD_REQUEST, "path is not a regular file")
    content = contents[path]
    return {
        "path": path,
        "size_bytes": len(content.encode()),
        "truncated": False,
        "encoding": "utf-8-replacement",
        "content": content,
    }


def replace_policy(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "network policy must be an object")
    try:
        parsed = parse_network_controls(body).to_json()
    except ConfigError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
    with STATE.lock:
        previous_statuses = {runtime: STATE.runtime_status(runtime) for runtime in RUNTIMES}
        STATE.policy = parsed
        STATE.codex_oauth = {}
        STATE.claude_oauth = {}
        for runtime in RUNTIMES:
            status = STATE.runtime_status(runtime)
            previous = previous_statuses[runtime]
            if status == "deactivated" and previous != "deactivated":
                STATE.add_agent_event("agent_runtime.deactivated", None, {"agent_runtime": runtime})
                fail_running_tasks_locked(runtime, "agent runtime deactivated because its managed network provider is disabled")
            elif previous == "deactivated" and status != "deactivated":
                STATE.add_agent_event("agent_runtime.awaiting_login", None, {"agent_runtime": runtime})
        start_queued_tasks_locked()
        return {"network_controls": STATE.policy}


def fail_running_tasks_locked(runtime: str, error_message: str) -> None:
    now = STATE.now()
    for task in STATE.tasks:
        if task["agent_runtime"] == runtime and task["status"] == "running":
            task["status"] = "failed"
            task["error_message"] = error_message
            task["updated_at"] = now
            STATE.add_agent_event("task.failed", task["task_id"], {"error_message": error_message})


def task_duration_seconds(task_id: str) -> float:
    number = int(task_id.rsplit("_", 1)[-1]) if task_id.rsplit("_", 1)[-1].isdigit() else 0
    return 8.0 + (number * 3) % 7


def start_queued_tasks_locked() -> None:
    """Claim queued tasks the way the real orchestrator does.

    One task per thread at a time, oldest first, capped per runtime and in
    total. Claimed tasks run for several seconds; ``progress_running_tasks_locked``
    moves them along on subsequent polls.
    """
    running_threads = {task["thread_id"] for task in STATE.tasks if task["status"] == "running"}
    running_by_runtime = {runtime: 0 for runtime in RUNTIMES}
    for task in STATE.tasks:
        if task["status"] == "running":
            running_by_runtime[task["agent_runtime"]] += 1
    for task in STATE.tasks:
        if task["status"] != "queued" or STATE.runtime_status(task["agent_runtime"]) != "active":
            continue
        if task["thread_id"] in running_threads:
            continue
        if running_by_runtime[task["agent_runtime"]] >= MAX_RUNNING_PER_RUNTIME:
            continue
        if sum(running_by_runtime.values()) >= MAX_RUNNING_TOTAL:
            break
        now = STATE.now()
        task["status"] = "running"
        task["started_at"] = now
        task["updated_at"] = now
        task["_started_monotonic"] = time.monotonic()
        task["_progress_emitted"] = 0
        running_threads.add(task["thread_id"])
        running_by_runtime[task["agent_runtime"]] += 1
        STATE.add_agent_event("task.started", task["task_id"], {})


def progress_running_tasks_locked() -> None:
    """Advance running tasks based on elapsed wall time.

    Emits the scripted progress messages (plus matching provider network
    events) as their milestones pass, then completes the task with an output
    that echoes the request.
    """
    for task in STATE.tasks:
        if task["status"] != "running" or "_started_monotonic" not in task:
            continue
        duration = task_duration_seconds(task["task_id"])
        fraction = (time.monotonic() - task["_started_monotonic"]) / duration
        emitted = task["_progress_emitted"]
        for milestone, message in PROGRESS_SCRIPT[emitted:]:
            if fraction < milestone:
                break
            STATE.add_agent_event("task.message", task["task_id"], {"message": message, "source": "agent"})
            host, api_path = PROVIDER_TRAFFIC[task["agent_runtime"]]
            STATE.add_network_event("POST", host, api_path, "allowed")
            task["_progress_emitted"] += 1
        if fraction >= 1.0:
            now = STATE.now()
            summary = task["input_message"].strip().splitlines()[0][:80].rstrip(".")
            task["status"] = "completed"
            task["output_message"] = (
                f"Done: {summary}.\nChecks passed; see the thread events for the step-by-step log."
            )
            task["completed_at"] = now
            task["updated_at"] = now
            task.pop("_started_monotonic", None)
            task.pop("_progress_emitted", None)
            STATE.add_agent_event("task.completed", task["task_id"], {})


def since(query: dict[str, list[str]]) -> int:
    values = query.get("since", ["0"])
    try:
        return int(values[0])
    except ValueError:
        return 0


def one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    return values[0]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True, help="Local port to bind, for example 3100.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    seed_state()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    actual_host, actual_port = server.server_address
    print(f"TrustyClaw mock admin UI: http://{actual_host}:{actual_port}/")
    print(f"Admin password: {PASSWORD}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
