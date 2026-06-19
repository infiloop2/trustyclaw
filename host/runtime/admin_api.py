"""Localhost admin API (127.0.0.1:7443), reached through SSH port forwarding.

Route handlers validate the documented protocol and update the JSON state
files; running tasks through the selected agent runtime is delegated to
``orchestrator``, which owns the worker pool and runtime process cache.
Privileged operations
(reboot, network policy replacement) go through narrow root-owned sudo
helpers.

Authentication compares a SHA-256 hash of the presented bearer password
against ``admin_password_sha256`` from config.json, so the cleartext password
never exists on the host.
"""

from __future__ import annotations

import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from host.config import AGENT_RUNTIMES, ConfigError, parse_network_controls
from host.constants import ADMIN_API_PORT, LOOPBACK, PROXY_PORT
from host.runtime import claude_code, codex_app_server, orchestrator, proxy_state_client, task_status
from host.runtime.orchestrator import agent_runtime_status
from host.runtime.state import (
    TASK_LIMIT,
    append_agent_event,
    load_config,
    page_agent_events,
    page_task_events,
    prune_agent_events,
    read_openai_account_id,
    read_claude_account,
    read_state,
    state_update,
    utc_now,
)
from host.runtime.task_status import (
    ACTIVE as ACTIVE_STATUSES,
    CANCELLED,
    FAILED,
    QUEUED,
    RUNNING,
)


HOST = LOOPBACK
PORT = ADMIN_API_PORT
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MESSAGE_LIMIT = 50_000
MAX_REQUEST_BODY_BYTES = 1024 * 1024
IDEMPOTENCY_RETENTION_SECONDS = 24 * 3600
IDEMPOTENCY_ENTRY_LIMIT = 10_000
MAINTENANCE_INTERVAL_SECONDS = 3600  # scheduled state cleanup cadence (not per-request)
FINISHED_TASK_LIMIT = 1000  # finished tasks kept as history before the oldest are pruned
THREAD_TASK_LIMIT = 1000
THREAD_MAP_LIMIT = 1000  # user thread -> runtime session mappings kept before LRU pruning
# Queued tasks and undelivered steers are the two operator-driven inputs that
# would otherwise grow state.json without bound (active tasks are never
# pruned; steers queue until the worker delivers them). Both caps return 409.
QUEUED_TASK_LIMIT = 1000
PENDING_STEER_LIMIT = 20
NETWORK_POLICY_LOCK_TIMEOUT_SECONDS = 5
NETWORK_POLICY_HELPER_TIMEOUT_SECONDS = 30
OAUTH_LOGIN_LOCK_TIMEOUT_SECONDS = 5
REBOOT_HELPER_TIMEOUT_SECONDS = 10
# Lock inventory for this module (each request runs on its own handler
# thread, so every handler is concurrent with every other and with the
# orchestrator's workers):
# - The state lock (private to state.py, entered through state_update() and
#   read_state()): every state.json access. Held briefly; slow work (runtime
#   spawns, helper subprocesses, process closes) always runs outside the
#   transaction so reads and /v1/health never stall behind a mutation.
# - NETWORK_POLICY_LOCK: serializes policy replacements end-to-end (validate +
#   root helper run). Acquired with a timeout so a stuck helper returns 409 to
#   later callers instead of piling up threads.
# - OAUTH_LOGIN_LOCK: serializes device-login starts so two clicks cannot mint
#   two device codes. Also timeout-guarded.
# - Idempotency keys act as a per-key mutex across retries: the in-flight
#   reservation in _mutate_idempotently is claimed in one state transaction,
#   the request body executes outside it, and the reservation is released on
#   failure (or by initialize_state after a crash).
NETWORK_POLICY_LOCK = threading.Lock()
OAUTH_LOGIN_LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = "TrustyClaw/0.1"

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _handle(self, method: str) -> None:
        try:
            path = urlparse(self.path)
            if method == "GET" and path.path == "/":
                self._send_ui()
                return
            self._authenticate()
            if method in {"POST", "PUT", "DELETE"}:
                response = self._mutate_idempotently(method, path)
            else:
                response = route(method, path.path, parse_qs(path.query), self._read_body())
            self._send_json(HTTPStatus.OK, response)
        except ApiError as exc:
            self._send_json(exc.status, {"error": {"message": exc.message}})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": str(exc)}})

    def _mutate_idempotently(self, method: str, path: Any) -> Any:
        """Replays of the same Idempotency-Key for the same method and path
        return the original response without re-executing the request."""
        key = self.headers.get("Idempotency-Key", "")
        if not IDEMPOTENCY_RE.fullmatch(key):
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing or invalid Idempotency-Key")
        body = self._read_body()
        # Atomically claim the key: in one transaction, either return a
        # completed response, reject an in-flight duplicate, or reserve the
        # key. Without the reservation, two concurrent retries with the same
        # key both pass a check-then-act gap and execute twice — exactly what
        # idempotency must prevent.
        with state_update() as state:
            entries = state.setdefault("idempotency", {})
            now = time.time()
            prune_idempotency(entries, now=now)
            stored = entries.get(key)
            if stored is not None:
                if stored["method"] != method or stored["path"] != path.path:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "Idempotency-Key was already used for a different request")
                if stored.get("in_flight"):
                    raise ApiError(HTTPStatus.CONFLICT, "a request with this Idempotency-Key is already in progress")
                # Honor the 24h retention on the read path too, not only when a
                # new key is stored.
                if now - stored["stored_at"] <= IDEMPOTENCY_RETENTION_SECONDS:
                    return stored["response"]
            entries[key] = {"method": method, "path": path.path, "in_flight": True, "stored_at": now}
            prune_idempotency(entries, now=now, preserve_key=key)
        # Execute outside the state transaction so a slow mutation (runtime
        # spawn, policy subprocess, process shutdown) never blocks reads or
        # /v1/health.
        try:
            response = route(method, path.path, parse_qs(path.query), body)
        except BaseException:
            # Release the reservation so a retry can proceed after a failure.
            with state_update() as state:
                entry = state.get("idempotency", {}).get(key)
                if entry is not None and entry.get("in_flight"):
                    del state["idempotency"][key]
            raise
        with state_update() as state:
            entries = state.setdefault("idempotency", {})
            now = time.time()
            entries[key] = {
                "method": method, "path": path.path, "response": response, "stored_at": now,
            }
            prune_idempotency(entries, now=now, preserve_key=key)
        return response

    def _send_ui(self) -> None:
        data = (Path(__file__).parent / "admin_ui.html").read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authenticate(self) -> None:
        expected = load_config().get("admin_password_sha256", "")
        auth = self.headers.get("Authorization", "")
        presented = hashlib.sha256(auth.removeprefix("Bearer ").encode()).hexdigest()
        if not expected or not auth.startswith("Bearer ") or not hmac.compare_digest(presented, expected):
            raise ApiError(HTTPStatus.UNAUTHORIZED, "missing or invalid admin password")

    def _read_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "malformed Content-Length") from exc
        if length < 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "malformed Content-Length")
        if length > MAX_REQUEST_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        if length == 0:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}") from exc

    def _send_json(self, status: HTTPStatus, body: Any) -> None:
        data = json.dumps(body, sort_keys=True).encode()
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def route(method: str, path: str, query: dict[str, list[str]], body: Any) -> Any:
    if method == "GET" and path == "/v1/health":
        return health()
    if method == "GET" and path == "/v1/agent-runtime/status":
        return agent_runtime_status()
    if method == "GET" and path == "/v1/agent-runtime/account":
        if query:
            raise ApiError(HTTPStatus.BAD_REQUEST, "agent-runtime account endpoint does not accept query parameters")
        return current_agent_accounts()
    if path == "/v1/agent-runtime/codex-oauth-login":
        if method == "POST":
            return start_codex_oauth_login()
        if method == "GET":
            return current_codex_oauth_login()
    if path == "/v1/agent-runtime/claude-oauth-login":
        if method == "POST":
            return start_claude_oauth_login()
        if method == "GET":
            return current_claude_oauth_login()
    if path == "/v1/agent-runtime/claude-oauth-login/complete" and method == "POST":
        return complete_claude_oauth_login(body)
    if path == "/v1/tasks":
        if method == "POST":
            return create_task(body)
        if method == "GET":
            return list_tasks(_one(query, "last_seen_task_id"))
    if path.startswith("/v1/tasks/"):
        return task_route(method, path, query, body)
    if path == "/v1/threads" and method == "GET":
        return list_threads()
    if path.startswith("/v1/threads/"):
        return thread_route(method, path, query)
    if path == "/v1/events" and method == "GET":
        return {"events": page_agent_events(_since(query)).items}
    if path == "/v1/network/policy":
        if method == "GET":
            return proxy_state_client.network_policy_response()
        if method == "PUT":
            return replace_network_policy(body)
    if path == "/v1/network/events" and method == "GET":
        return {"events": proxy_state_client.network_events(_since(query))}
    if path == "/v1/host-runtime/reboot" and method == "POST":
        return reboot_host()
    raise ApiError(HTTPStatus.NOT_FOUND, "route not found")


def reboot_host() -> dict[str, str]:
    """Run the reboot helper synchronously. ``systemctl reboot`` only schedules
    the reboot and returns, so this stays fast — and a helper that fails to even
    schedule it (e.g. a broken sudoers entry) surfaces as a 500 instead of a
    silent "accepted" for a reboot that will never happen. The host goes down
    moments after the response is sent."""
    try:
        proc = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/reboot-host"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=REBOOT_HELPER_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, PermissionError):
        # On timeout, subprocess.run kills the child — but the helper runs as
        # root via sudo, so the unprivileged service user's kill raises
        # PermissionError in place of TimeoutExpired. Either way the reboot may
        # already be in flight; report accepted rather than a false failure.
        return {"status": "accepted"}
    if proc.returncode != 0:
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, proc.stderr.strip() or "reboot helper failed")
    return {"status": "accepted"}


def task_route(method: str, path: str, query: dict[str, list[str]], body: Any) -> Any:
    parts = path.strip("/").split("/")
    if len(parts) < 3:
        raise ApiError(HTTPStatus.NOT_FOUND, "task route not found")
    task_id = parts[2]
    if len(parts) == 3:
        if method == "GET":
            return get_task(task_id)
        if method == "PUT":
            return update_task(task_id, body)
    if len(parts) == 4 and parts[3] == "steer" and method == "POST":
        return steer_task(task_id, body)
    if len(parts) == 4 and parts[3] == "cancel" and method == "POST":
        return cancel_task(task_id)
    if len(parts) == 4 and parts[3] == "kill" and method == "POST":
        return kill_task(task_id)
    if len(parts) == 4 and parts[3] == "events" and method == "GET":
        return {"events": page_task_events(task_id, _since(query)).items}
    raise ApiError(HTTPStatus.NOT_FOUND, "task route not found")


def thread_route(method: str, path: str, query: dict[str, list[str]]) -> Any:
    parts = path.strip("/").split("/")
    if len(parts) == 4 and parts[3] == "tasks" and method == "GET":
        thread_id = parts[2]
        if not THREAD_ID_RE.fullmatch(thread_id):
            raise ApiError(HTTPStatus.NOT_FOUND, "thread route not found")
        return list_thread_tasks(thread_id)
    raise ApiError(HTTPStatus.NOT_FOUND, "thread route not found")


def health() -> dict[str, Any]:
    runtime = agent_runtime_status()
    network_status = proxy_state_client.network_status()
    if network_status == "active" and not proxy_alive():
        network_status = "error"  # policy says active but nothing is enforcing it
    degraded = any(item["status"] == "error" for item in runtime["runtimes"]) or network_status == "error"
    return {
        "status": "degraded" if degraded else "ok",
        "agent_name": load_config().get("agent_name"),
        "agent_runtime": runtime,
        "network_controls": {"status": network_status},
        "host_runtime": host_metrics(),
    }


def proxy_alive() -> bool:
    """The proxy binds loopback, which nftables always allows, so a TCP
    connect is a meaningful liveness probe even while the network is locked
    down. If the proxy is down the agent has no network path at all."""
    try:
        with socket.create_connection((HOST, PROXY_PORT), timeout=1):
            return True
    except OSError:
        return False


def _task_number(task: dict[str, Any]) -> int:
    try:
        return int(str(task["task_id"]).rsplit("_", 1)[-1])
    except (KeyError, ValueError):
        return 0


def _task_history_key(task: dict[str, Any]) -> tuple[str, int]:
    return (str(task.get("updated_at", "")), _task_number(task))


def prune_idempotency(entries: dict[str, Any], *, now: float, preserve_key: str | None = None) -> None:
    for old_key in [
        k
        for k, v in entries.items()
        if k != preserve_key and now - v.get("stored_at", now) > IDEMPOTENCY_RETENTION_SECONDS
    ]:
        del entries[old_key]
    if len(entries) <= IDEMPOTENCY_ENTRY_LIMIT:
        return
    ordered = sorted(
        ((k, v) for k, v in entries.items() if k != preserve_key),
        key=lambda item: item[1].get("stored_at", 0.0),
    )
    for old_key, _ in ordered[: len(entries) - IDEMPOTENCY_ENTRY_LIMIT]:
        del entries[old_key]


def prune_state() -> None:
    """Bound state.json growth: keep all active tasks plus the most recent
    finished ones, and drop expired/excess idempotency entries. Runs on a
    schedule (maintenance_loop), never on the request path, so reads stay
    cheap."""
    with state_update() as state:
        tasks = state.get("tasks", [])
        active = [t for t in tasks if t["status"] in ACTIVE_STATUSES]
        finished = sorted(
            (t for t in tasks if t["status"] not in ACTIVE_STATUSES),
            key=_task_history_key,
        )
        kept_finished = finished[-FINISHED_TASK_LIMIT:]
        pruned_tasks = {_task_number(t) for t in finished[:-FINISHED_TASK_LIMIT]}

        entries = state.get("idempotency", {})
        now = time.time()
        before_idempotency = dict(entries)
        prune_idempotency(entries, now=now)

        # Bound the user-thread -> runtime session maps: drop the least recently
        # used mappings. A dropped thread is not an error — a later task on it
        # just starts a fresh runtime conversation.
        threads = state.get("codex_threads", {})
        stale_codex_threads = len(threads) > THREAD_MAP_LIMIT
        claude_sessions = state.get("claude_sessions", {})
        stale_claude_sessions = len(claude_sessions) > THREAD_MAP_LIMIT

        if not pruned_tasks and entries == before_idempotency and not stale_codex_threads and not stale_claude_sessions:
            return
        if stale_codex_threads:
            kept = sorted(threads.items(), key=lambda item: item[1].get("last_used_at", ""))[-THREAD_MAP_LIMIT:]
            state["codex_threads"] = dict(kept)
        if stale_claude_sessions:
            kept = sorted(claude_sessions.items(), key=lambda item: item[1].get("last_used_at", ""))[-THREAD_MAP_LIMIT:]
            state["claude_sessions"] = dict(kept)
        # Preserve original task order, minus the pruned finished ones.
        kept_numbers = {_task_number(t) for t in active} | {_task_number(t) for t in kept_finished}
        state["tasks"] = [t for t in tasks if _task_number(t) in kept_numbers]
        # Bound the agent-event log while still inside the state transaction,
        # so event sequence allocation and log pruning share one critical
        # section.
        prune_agent_events()


def maintenance_loop() -> None:
    while True:
        time.sleep(MAINTENANCE_INTERVAL_SECONDS)
        try:
            prune_state()
        except Exception:
            pass  # maintenance is best-effort; never crash the service


def start_codex_oauth_login() -> dict[str, str]:
    if not OAUTH_LOGIN_LOCK.acquire(timeout=OAUTH_LOGIN_LOCK_TIMEOUT_SECONDS):
        raise ApiError(HTTPStatus.CONFLICT, "Codex OAuth login is already starting")
    try:
        if not orchestrator.runtime_network_enabled("codex"):
            raise ApiError(HTTPStatus.CONFLICT, "Codex OAuth login is unavailable while OpenAI provider access is disabled")
        state = read_state()
        if _runtime_status(state, "codex") != "awaiting_login":
            raise ApiError(HTTPStatus.CONFLICT, "Codex OAuth login is only available while awaiting_login")
        oauth = state.get("codex_oauth")
        if oauth:
            return {key: oauth[key] for key in ("status", "device_code", "login_url", "expires_at")}
        login = codex_app_server.start_device_login()
        response = {
            "status": "awaiting_login",
            "device_code": login.user_code,
            "login_url": login.verification_url,
            "expires_at": _minutes_from_now(10),
        }
        with state_update() as state:
            if not orchestrator.runtime_network_enabled("codex") or _runtime_status(state, "codex") != "awaiting_login":
                codex_app_server.close_login_server()
                raise ApiError(HTTPStatus.CONFLICT, "Codex OAuth login is only available while awaiting_login")
            state["codex_oauth"] = response | {"login_id": login.login_id}
        return response
    finally:
        OAUTH_LOGIN_LOCK.release()


def current_codex_oauth_login() -> dict[str, str]:
    if not orchestrator.runtime_network_enabled("codex"):
        raise ApiError(HTTPStatus.CONFLICT, "Codex OAuth login is unavailable while OpenAI provider access is disabled")
    state = read_state()
    if _runtime_status(state, "codex") != "awaiting_login":
        raise ApiError(HTTPStatus.CONFLICT, "Codex OAuth login is only available while awaiting_login")
    oauth = state.get("codex_oauth")
    if not oauth:
        raise ApiError(HTTPStatus.NOT_FOUND, "Codex OAuth login has not been started")
    return {key: oauth[key] for key in ("status", "device_code", "login_url", "expires_at")}


def start_claude_oauth_login() -> dict[str, str]:
    if not OAUTH_LOGIN_LOCK.acquire(timeout=OAUTH_LOGIN_LOCK_TIMEOUT_SECONDS):
        raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is already starting")
    try:
        if not orchestrator.runtime_network_enabled("claude_code"):
            raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is unavailable while Claude provider access is disabled")
        state = read_state()
        if _runtime_status(state, "claude_code") != "awaiting_login":
            raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is only available while awaiting_login")
        oauth = state.get("claude_oauth")
        if isinstance(oauth, dict) and oauth.get("provider") == "claude":
            return {key: oauth[key] for key in ("status", "login_url", "expires_at")}
        login = claude_code.start_oauth_login()
        response = {
            "status": "awaiting_code",
            "login_url": login.login_url,
            "expires_at": _minutes_from_now(10),
        }
        with state_update() as state:
            if not orchestrator.runtime_network_enabled("claude_code") or _runtime_status(state, "claude_code") != "awaiting_login":
                claude_code.close_login_process()
                raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is only available while awaiting_login")
            state["claude_oauth"] = response | {"provider": "claude"}
        return response
    finally:
        OAUTH_LOGIN_LOCK.release()


def current_claude_oauth_login() -> dict[str, str]:
    if not orchestrator.runtime_network_enabled("claude_code"):
        raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is unavailable while Claude provider access is disabled")
    state = read_state()
    if _runtime_status(state, "claude_code") != "awaiting_login":
        raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is only available while awaiting_login")
    oauth = state.get("claude_oauth")
    if not isinstance(oauth, dict) or oauth.get("provider") != "claude":
        raise ApiError(HTTPStatus.NOT_FOUND, "Claude OAuth login has not been started")
    return {key: oauth[key] for key in ("status", "login_url", "expires_at")}


def complete_claude_oauth_login(body: Any) -> dict[str, str]:
    if not isinstance(body, dict) or not isinstance(body.get("code"), str) or not body["code"].strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "code must be a non-empty string")
    if not orchestrator.runtime_network_enabled("claude_code"):
        raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is unavailable while Claude provider access is disabled")
    try:
        claude_code.complete_oauth_login(body["code"])
    except claude_code.ClaudeCodeError as exc:
        raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
    with state_update() as state:
        state["claude_oauth"] = None
    orchestrator.refresh_runtime_status("claude_code")
    return {"status": "accepted"}


def current_agent_accounts() -> dict[str, Any]:
    state = read_state()
    return {"accounts": [_current_agent_account(state, "codex"), _current_agent_account(state, "claude_code")]}


def _current_agent_account(state: dict[str, Any], runtime_type: str) -> dict[str, Any]:
    status = _runtime_status(state, runtime_type)
    if runtime_type == "claude_code":
        response = {"agent_runtime": "claude_code", "provider": "claude", "status": status}
        if status != "active":
            return response
        account = read_claude_account()
        account_id = account.get("account_id")
        organization_id = account.get("organization_id")
        email = account.get("email")
        if isinstance(account_id, str) and account_id:
            response["account_id"] = account_id
        if isinstance(organization_id, str) and organization_id:
            response["organization_id"] = organization_id
        if isinstance(email, str) and email:
            response["email"] = email
        return response
    response = {"agent_runtime": "codex", "provider": "openai", "status": status}
    if status != "active":
        return response
    account_id = read_openai_account_id()
    if account_id:
        response["account_id"] = account_id
    return response


def create_task(body: Any) -> dict[str, Any]:
    input_message = _message(body, "input_message")
    thread_id = _thread_id(body)
    agent_runtime = _agent_runtime(body)
    with state_update() as state:
        existing_runtime = thread_runtime_in_state(state, thread_id)
        if existing_runtime is not None and existing_runtime != agent_runtime:
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"thread_id is already used by {existing_runtime}; use that runtime or choose a new thread_id",
            )
        if sum(1 for t in state["tasks"] if t["status"] == QUEUED) >= QUEUED_TASK_LIMIT:
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"task queue is full ({QUEUED_TASK_LIMIT} queued tasks); cancel queued tasks or wait",
            )
        task_id = f"task_{state['next_task_number']}"
        state["next_task_number"] = int(state["next_task_number"]) + 1
        now = utc_now()
        task = {
            "task_id": task_id,
            "status": QUEUED,
            "agent_runtime": agent_runtime,
            "thread_id": thread_id,
            "input_message": input_message,
            "steer_messages": [],
            "created_at": now,
            "updated_at": now,
        }
        state["tasks"].append(task)
    orchestrator.WORKER_WAKE.set()
    return public_task(task)


def list_tasks(last_seen_task_id: str | None) -> dict[str, Any]:
    tasks = [task for task in read_state()["tasks"] if task["status"] in ACTIVE_STATUSES]
    ordered = _queue_order(tasks)
    start = 0
    if last_seen_task_id:
        for index, task in enumerate(ordered):
            if task[1]["task_id"] == last_seen_task_id:
                start = index + 1
                break
    return {"tasks": [public_task(task, queue_position=position) for position, task in ordered[start : start + TASK_LIMIT]]}


def list_threads() -> dict[str, Any]:
    state = read_state()
    threads: dict[str, dict[str, Any]] = {}
    for task in state["tasks"]:
        thread_id = task["thread_id"]
        runtime = task["agent_runtime"]
        entry = threads.setdefault(
            thread_id,
            {
                "thread_id": thread_id,
                "agent_runtime": runtime,
                "last_used_at": task["updated_at"],
                "active_tasks": [],
                "task_count": 0,
            },
        )
        entry["last_used_at"] = max(str(entry["last_used_at"]), str(task["updated_at"]))
        entry["task_count"] = int(entry["task_count"]) + 1
        if task["status"] in ACTIVE_STATUSES:
            entry["active_tasks"].append({"task_id": task["task_id"], "status": task["status"]})
    for thread_id, mapping in state.get("codex_threads", {}).items():
        _merge_thread_mapping(threads, thread_id, "codex", mapping)
    for thread_id, mapping in state.get("claude_sessions", {}).items():
        _merge_thread_mapping(threads, thread_id, "claude_code", mapping)
    ordered = sorted(
        threads.values(),
        key=lambda item: (str(item["last_used_at"]), item["agent_runtime"], item["thread_id"]),
        reverse=True,
    )
    return {"threads": ordered}


def _merge_thread_mapping(
    threads: dict[str, dict[str, Any]], thread_id: str, runtime: str, mapping: Any
) -> None:
    if not isinstance(mapping, dict):
        return
    last_used_at = str(mapping.get("last_used_at", ""))
    entry = threads.setdefault(
        thread_id,
        {
            "thread_id": thread_id,
            "agent_runtime": runtime,
            "last_used_at": last_used_at,
            "active_tasks": [],
            "task_count": 0,
        },
    )
    entry["last_used_at"] = max(str(entry["last_used_at"]), last_used_at)


def list_thread_tasks(thread_id: str) -> dict[str, Any]:
    tasks = [task for task in read_state()["tasks"] if task["thread_id"] == thread_id]
    ordered = sorted(tasks, key=_task_history_key, reverse=True)
    return {"tasks": [public_task(task) for task in ordered[:THREAD_TASK_LIMIT]]}


def thread_runtime_in_state(state: dict[str, Any], thread_id: str) -> str | None:
    for task in state["tasks"]:
        if task["thread_id"] == thread_id:
            return str(task["agent_runtime"])
    if thread_id in state.get("codex_threads", {}):
        return "codex"
    if thread_id in state.get("claude_sessions", {}):
        return "claude_code"
    return None


def get_task(task_id: str) -> dict[str, Any]:
    return public_task(_find_task_in_state(read_state(), task_id))


def update_task(task_id: str, body: Any) -> dict[str, Any]:
    input_message = _message(body, "input_message")
    with state_update() as state:
        task = _find_task_in_state(state, task_id)
        if task["status"] != QUEUED:
            raise ApiError(HTTPStatus.CONFLICT, "only queued tasks can be updated")
        task["input_message"] = input_message
        task["updated_at"] = utc_now()
        return public_task(task)


def steer_task(task_id: str, body: Any) -> dict[str, str]:
    steer_message = _message(body, "steer_message")
    with state_update() as state:
        task = _find_task_in_state(state, task_id)
        if task["status"] != RUNNING:
            raise ApiError(HTTPStatus.CONFLICT, "only running tasks can be steered")
        pending = task.setdefault("steer_messages", [])
        if len(pending) >= PENDING_STEER_LIMIT:
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"task already has {PENDING_STEER_LIMIT} undelivered steer messages; wait for delivery",
            )
        pending.append(steer_message)
        task["updated_at"] = utc_now()
        append_agent_event(state, "task.message", task_id, {"message": steer_message, "source": "user"})
    return {"status": "accepted"}


def cancel_task(task_id: str) -> dict[str, str]:
    with state_update() as state:
        task = _find_task_in_state(state, task_id)
        if task["status"] != QUEUED:
            raise ApiError(HTTPStatus.CONFLICT, "only queued tasks can be cancelled")
        task_status.set_status(task, CANCELLED, now=utc_now())
        append_agent_event(state, "task.cancelled", task_id, {})
    return {"status": "accepted"}


def kill_task(task_id: str) -> dict[str, str]:
    with state_update() as state:
        task = _find_task_in_state(state, task_id)
        if task["status"] != RUNNING:
            raise ApiError(HTTPStatus.CONFLICT, "only running tasks can be killed")
        task_status.set_status(task, CANCELLED, now=utc_now())
        append_agent_event(state, "task.cancelled", task_id, {})
    # Kill the task's runtime process outside the state transaction (closing can be
    # slow). The worker blocked in run_turn sees the dead server as an error
    # and finds the task already cancelled, so the cancellation sticks.
    orchestrator.close_task_server(task_id)
    orchestrator.WORKER_WAKE.set()  # a queued task can take the freed slot
    return {"status": "accepted"}


def replace_network_policy(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be the replacement network_controls object")
    try:
        parsed = parse_network_controls(body)
    except ConfigError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
    if not NETWORK_POLICY_LOCK.acquire(timeout=NETWORK_POLICY_LOCK_TIMEOUT_SECONDS):
        raise ApiError(HTTPStatus.CONFLICT, "network policy update already in progress")
    try:
        if not parsed.ssh_port_opened:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "network_controls.ssh_port_opened must be true because SSH is currently "
                "the only supported way to access the host",
            )
        # ssh_port_opened is enforced by nftables and the security group, both
        # set once at deploy time; the runtime cannot change them. Reject a
        # changed value rather than silently accepting it and reporting a false
        # success.
        current = proxy_state_client.network_policy()
        if parsed.ssh_port_opened != current.get("ssh_port_opened", False):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "ssh_port_opened can only be set at deploy time; it cannot be changed through the API",
            )
        # Allow recovery from a stuck "reloading" (crash mid-update) or "error":
        # only "loading" (no policy applied yet) blocks a replace.
        if proxy_state_client.network_status() == "loading":
            raise ApiError(HTTPStatus.CONFLICT, "network policy is not initialized yet")
        result = apply_network_policy_as_root(parsed.to_json())
        orchestrator.reconcile_runtime_status_after_policy_change()
        return result
    finally:
        NETWORK_POLICY_LOCK.release()


def apply_network_policy_as_root(policy: dict[str, Any]) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/update-network-policy"],
            input=json.dumps(policy),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=NETWORK_POLICY_HELPER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ApiError(HTTPStatus.GATEWAY_TIMEOUT, "network policy update timed out") from exc
    except PermissionError as exc:
        # The timeout path kills the child, but the helper starts as root via
        # sudo before demoting to the proxy user, so the unprivileged service
        # user's kill can raise PermissionError in place of TimeoutExpired.
        # The helper keeps running; its own file lock and "reloading" status
        # keep the policy files consistent.
        raise ApiError(
            HTTPStatus.GATEWAY_TIMEOUT,
            "network policy update timed out (the root helper could not be terminated)",
        ) from exc
    if proc.returncode != 0:
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, proc.stderr.strip() or "network policy update failed")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "network policy update returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "network policy update returned invalid JSON")
    return result


def host_metrics() -> dict[str, Any]:
    return {
        "cpu": {"usage_percent": cpu_usage_percent()},
        "memory": memory_metrics(),
        "filesystem": filesystem_metrics(),
        "swap": swap_metrics(),
    }


def cpu_usage_percent() -> float:
    # Deliberately samples /proc/stat 50ms apart on the calling thread: health
    # requests each run on their own handler thread, so the brief block delays
    # only that response, and it keeps the metric stateless.
    first = _cpu_times()
    time.sleep(0.05)
    second = _cpu_times()
    idle_delta = second["idle"] - first["idle"]
    total_delta = second["total"] - first["total"]
    if total_delta <= 0:
        return 0.0
    return round(100.0 * (1.0 - idle_delta / total_delta), 1)


def _cpu_times() -> dict[str, int]:
    values = [int(part) for part in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = values[3] + values[4]
    return {"idle": idle, "total": sum(values)}


def memory_metrics() -> dict[str, int]:
    mem = _proc_meminfo()
    total = mem["MemTotal"] * 1024
    available = mem.get("MemAvailable", 0) * 1024
    return {"used_bytes": total - available, "total_bytes": total}


def _filesystem_usage(path: str) -> dict[str, int] | None:
    try:
        usage = shutil.disk_usage(path)
    except FileNotFoundError:
        return None
    return {"used_bytes": usage.used, "total_bytes": usage.total}


def filesystem_metrics() -> dict[str, Any]:
    root = _filesystem_usage("/") or {"used_bytes": 0, "total_bytes": 0}
    mounts = {"root": root}
    for name, path in (
        ("admin", "/mnt/trustyclaw-admin"),
        ("agent", "/mnt/trustyclaw-agent"),
    ):
        usage = _filesystem_usage(path)
        if usage is not None:
            mounts[name] = usage
    return {**root, "mounts": mounts}


def swap_metrics() -> dict[str, int]:
    mem = _proc_meminfo()
    total = mem.get("SwapTotal", 0) * 1024
    free = mem.get("SwapFree", 0) * 1024
    return {"allocated_bytes": total, "used_bytes": total - free}


def _proc_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.strip().split()[0])
    return values


def public_task(task: dict[str, Any], queue_position: int | None = None) -> dict[str, Any]:
    value = {
        "task_id": task["task_id"],
        "status": task["status"],
        "agent_runtime": task["agent_runtime"],
        "thread_id": task["thread_id"],
        "input_message": task["input_message"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
    }
    if task.get("output_message") is not None:
        value["output_message"] = task["output_message"]
    if task.get("error_message") is not None:
        value["error_message"] = task["error_message"]
    if queue_position is not None:
        value["queue_position"] = queue_position
    return value


def _queue_order(tasks: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    running = [task for task in tasks if task["status"] == RUNNING]
    queued = [task for task in tasks if task["status"] == QUEUED]
    ordered = [(0, task) for task in running]
    ordered.extend((index, task) for index, task in enumerate(queued, start=1))
    return ordered


def _find_task_in_state(state: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in state["tasks"]:
        if task["task_id"] == task_id:
            return task
    raise ApiError(HTTPStatus.NOT_FOUND, "task not found")


def _message(body: Any, key: str) -> str:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be a non-empty string")
    if len(value) > MESSAGE_LIMIT:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be at most {MESSAGE_LIMIT} characters")
    return value


def _thread_id(body: Any) -> str:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
    value = body.get("thread_id")
    if not isinstance(value, str) or not THREAD_ID_RE.fullmatch(value):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "thread_id must be 1 to 64 characters of A-Z, a-z, 0-9, '-', or '_'",
        )
    return value


def _agent_runtime(body: Any) -> str:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
    value = body.get("agent_runtime")
    if not isinstance(value, str):
        raise ApiError(HTTPStatus.BAD_REQUEST, "agent_runtime must be 'codex' or 'claude_code'")
    return _agent_runtime_value(value)


def _agent_runtime_value(value: str) -> str:
    if value not in AGENT_RUNTIMES:
        raise ApiError(HTTPStatus.BAD_REQUEST, "agent_runtime must be 'codex' or 'claude_code'")
    return value


def _runtime_status(state: dict[str, Any], runtime_type: str) -> str:
    statuses = state.get("agent_runtime_statuses", {})
    runtime_state = statuses.get(runtime_type, {}) if isinstance(statuses, dict) else {}
    if isinstance(runtime_state, dict):
        return str(runtime_state.get("status", "loading"))
    return "loading"


def _one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    if len(values) != 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must appear once")
    return values[0]


def _since(query: dict[str, list[str]]) -> int | None:
    value = _one(query, "since")
    if value is None:
        return None
    try:
        since = int(value)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "since must be an integer") from exc
    if since < 0:
        raise ApiError(HTTPStatus.BAD_REQUEST, "since must be non-negative")
    return since


def _minutes_from_now(minutes: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + minutes * 60))


def initialize_state() -> None:
    """Recover after a restart or reboot: a task that was mid-turn has no
    worker attached anymore, so fail it rather than leave it running forever."""
    with state_update() as state:
        now = utc_now()
        for task in state["tasks"]:
            if task["status"] == RUNNING:
                task_status.set_status(task, FAILED, now=now)
                task["error_message"] = "host runtime restarted while the task was running"
                append_agent_event(state, "task.failed", task["task_id"], {"error_message": task["error_message"]})
        # Drop idempotency reservations whose request died with the process;
        # otherwise a retry of that key would get 409 forever.
        entries = state.get("idempotency", {})
        for key in [k for k, v in entries.items() if v.get("in_flight")]:
            del entries[key]


def main() -> int:
    # Bind the port before touching state: the state lock is in-process only,
    # so the bind is the single-instance gate. A second instance must fail here
    # rather than fail the live instance's running task and drop its in-flight
    # idempotency reservations first.
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    initialize_state()
    orchestrator.start_workers()
    threading.Thread(target=maintenance_loop, daemon=True).start()
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
