#!/usr/bin/env python3
"""Serve the admin UI against a deterministic local mock backend.

This is for browser/UI development only. It does not import the real admin API
handler because the real handler reads host state and invokes privileged helper
paths. The mock keeps just enough in-memory state to exercise the single-page
admin UI at ``host/runtime/admin_ui.html``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
import threading
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from host.config import ConfigError, parse_network_controls

RUNTIME_DIR = REPO_ROOT / "host/runtime"
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
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def add_agent_event(self, event_type: str, task_id: str | None, payload: dict[str, Any]) -> None:
        event = {
            "seq": self.next_agent_event_seq,
            "timestamp": self.now(),
            "event_type": event_type,
            "task_id": task_id,
            "payload": payload,
        }
        self.next_agent_event_seq += 1
        self.agent_events.append(event)
        if task_id:
            self.task_events.setdefault(task_id, []).append(event)

    def public_task(self, task: dict[str, Any], queue_position: int | None = None) -> dict[str, Any]:
        result = dict(task)
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


class Handler(BaseHTTPRequestHandler):
    server_version = "TrustyClawMock/0.1"

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
        return oauth("claude", method)
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
    if method == "POST" and path == "/v1/host-runtime/reboot":
        STATE.reboot_requested = True
        return {"status": "accepted"}
    raise ApiError(HTTPStatus.NOT_FOUND, "route not found")


def health() -> dict[str, Any]:
    with STATE.lock:
        progress_queued_tasks_locked()
        runtime = agent_runtime_status_locked()
    return {
        "status": "ok",
        "agent_name": "trustyclaw-mock",
        "agent_runtime": runtime,
        "network_controls": {"status": "active"},
        "host_runtime": {
            "cpu": {"usage_percent": 7.5},
            "memory": {"used_bytes": 256 * 1024 * 1024, "total_bytes": 1024 * 1024 * 1024},
            "filesystem": {"used_bytes": 4 * 1024**3, "total_bytes": 16 * 1024**3},
            "swap": {"used_bytes": 0, "allocated_bytes": 6 * 1024**3},
        },
    }


def agent_runtime_status() -> dict[str, Any]:
    with STATE.lock:
        progress_queued_tasks_locked()
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
        accounts: list[dict[str, Any]] = []
        for runtime in RUNTIMES:
            status = STATE.runtime_status(runtime)
            if runtime == "codex":
                account = {"agent_runtime": runtime, "provider": "openai", "status": status}
                if status == "active":
                    account["account_id"] = "mock-openai-account"
            else:
                account = {"agent_runtime": runtime, "provider": "claude", "status": status}
                if status == "active":
                    account.update(
                        {
                            "account_id": "mock-claude-account",
                            "organization_id": "mock-claude-org",
                            "email": "claude@example.invalid",
                        }
                    )
            accounts.append(account)
        return {"accounts": accounts}


def oauth(runtime: str, method: str) -> dict[str, str]:
    now = STATE.now()
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
                    "login_url": "https://example.invalid/codex-login",
                    "expires_at": now,
                }
            return dict(STATE.codex_oauth)
    if method not in {"GET", "POST"}:
        raise ApiError(HTTPStatus.NOT_FOUND, "route not found")
    with STATE.lock:
        if method == "GET" and not STATE.claude_oauth:
            raise ApiError(HTTPStatus.NOT_FOUND, "Claude OAuth login has not been started")
        if not STATE.claude_oauth:
            STATE.claude_oauth = {
                "status": "awaiting_code",
                "login_url": "https://example.invalid/claude-login",
                "expires_at": now,
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
        progress_queued_tasks_locked()
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
        progress_queued_tasks_locked()
        return STATE.public_task(task, queue_position=len(active_tasks()))


def active_tasks() -> list[dict[str, Any]]:
    return [task for task in STATE.tasks if task["status"] in {"queued", "running"}]


def list_tasks() -> dict[str, Any]:
    with STATE.lock:
        progress_queued_tasks_locked()
        return {"tasks": [STATE.public_task(task, index + 1) for index, task in enumerate(active_tasks())]}


def task_route(
    method: str, task_id: str, action: str | None, query: dict[str, list[str]], body: Any
) -> dict[str, Any]:
    with STATE.lock:
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
            STATE.add_agent_event("task.cancelled", task_id, {"message": "killed by mock"})
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
        tasks = [STATE.public_task(task) for task in STATE.tasks if task["thread_id"] == thread_id]
        return {"tasks": list(reversed(tasks))}


def agent_events(min_seq: int) -> list[dict[str, Any]]:
    with STATE.lock:
        return [event for event in STATE.agent_events if event["seq"] > min_seq]


def network_events(min_seq: int) -> list[dict[str, Any]]:
    with STATE.lock:
        if not STATE.network_events:
            event = {
                "seq": STATE.next_network_event_seq,
                "timestamp": STATE.now(),
                "method": "GET",
                "protocol": "https",
                "host": "api.github.com",
                "path": "/zen",
                "decision": "allowed",
            }
            STATE.next_network_event_seq += 1
            STATE.network_events.append(event)
        return [event for event in STATE.network_events if event["seq"] > min_seq]


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
            elif status == "active" and previous != "active":
                STATE.add_agent_event("agent_runtime.active", None, {"agent_runtime": runtime})
        progress_queued_tasks_locked()
        return {"network_controls": STATE.policy}


def fail_running_tasks_locked(runtime: str, error_message: str) -> None:
    now = STATE.now()
    for task in STATE.tasks:
        if task["agent_runtime"] == runtime and task["status"] == "running":
            task["status"] = "failed"
            task["error_message"] = error_message
            task["updated_at"] = now
            STATE.add_agent_event("task.failed", task["task_id"], {"error_message": error_message})


def progress_queued_tasks_locked() -> None:
    now = STATE.now()
    for task in STATE.tasks:
        if task["status"] != "queued" or STATE.runtime_status(task["agent_runtime"]) != "active":
            continue
        task["status"] = "running"
        task["started_at"] = now
        task["updated_at"] = now
        STATE.add_agent_event("task.started", task["task_id"], {})
        STATE.add_agent_event(
            "task.message",
            task["task_id"],
            {"message": f"Mock {task['agent_runtime']} received: {task['input_message']}", "source": "agent"},
        )
        task["status"] = "completed"
        task["output_message"] = f"Mock {task['agent_runtime']} completed: {task['input_message']}"
        task["completed_at"] = now
        task["updated_at"] = now
        STATE.add_agent_event("task.completed", task["task_id"], {})


def since(query: dict[str, list[str]]) -> int:
    values = query.get("since", ["0"])
    try:
        return int(values[0])
    except ValueError:
        return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True, help="Local port to bind, for example 3100.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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
