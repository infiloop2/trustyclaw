"""Localhost admin API (127.0.0.1:7443), reached through SSH port forwarding.

Route handlers validate the documented protocol and update admin state in the
local Postgres database (through the storage accessors in ``state``);
running tasks through the selected agent runtime is delegated to
``orchestrator``, which owns the worker pool and runtime process cache.
Privileged operations
(reboot, network policy replacement) go through narrow root-owned sudo
helpers.

Authentication compares a SHA-256 hash of the presented bearer password
against ``admin_password_sha256`` from the config table, so the cleartext
password never exists on the host.
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
from host.runtime import claude_code, codex_app_server, orchestrator, proxy_state_client, state, task_status
from host.runtime.orchestrator import agent_runtime_status
from host.runtime.state import (
    TASK_LIMIT,
    load_config,
    page_agent_events,
    page_task_events,
    read_claude_account,
    read_openai_account,
    utc_now,
)
from host.runtime.task_status import (
    ACTIVE as ACTIVE_STATUSES,
    CANCELLED,
    FAILED,
    QUEUED,
    RUNNING,
)
from host.version import version_status


HOST = LOOPBACK
PORT = ADMIN_API_PORT
UI_ASSETS = {
    "/": ("admin_ui.html", "text/html; charset=utf-8"),
    "/admin_ui.css": ("admin_ui.css", "text/css; charset=utf-8"),
    "/admin_ui.js": ("admin_ui.js", "application/javascript; charset=utf-8"),
}
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MESSAGE_LIMIT = 50_000
MAX_REQUEST_BODY_BYTES = 1024 * 1024
IDEMPOTENCY_RETENTION_SECONDS = 24 * 3600
IDEMPOTENCY_ENTRY_LIMIT = 10_000
MAINTENANCE_INTERVAL_SECONDS = 3600  # scheduled state cleanup cadence (not per-request)
FINISHED_TASK_LIMIT = 100_000  # finished tasks kept as history before the oldest are pruned
THREAD_TASK_LIMIT = 1000
THREAD_MAP_LIMIT = 100_000  # user thread -> runtime session mappings kept before LRU pruning
# Queued tasks and undelivered steers are the two operator-driven inputs that
# would otherwise grow admin state without bound (active tasks are never
# pruned; steers queue until the worker delivers them). Both caps return 409.
QUEUED_TASK_LIMIT = 1000
PENDING_STEER_LIMIT = 20
NETWORK_POLICY_LOCK_TIMEOUT_SECONDS = 5
OAUTH_LOGIN_LOCK_TIMEOUT_SECONDS = 5
REBOOT_HELPER_TIMEOUT_SECONDS = 10
AGENT_FILE_HELPER_TIMEOUT_SECONDS = 10
AGENT_FILE_HELPER_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/read-agent-file"]
# Lock inventory for this module (each request runs on its own handler
# thread, so every handler is concurrent with every other and with the
# orchestrator's workers):
# - The mutation lock (private to state.py, entered through state.mutation()):
#   every admin-state write cycle. Held briefly; slow work (runtime spawns,
#   helper subprocesses, process closes) always runs outside the mutation so
#   reads and /v1/health never stall behind it. Reads are lock-free queries.
# - NETWORK_POLICY_LOCK: serializes policy replacements end-to-end (validate +
#   root helper run). Acquired with a timeout so a stuck helper returns 409 to
#   later callers instead of piling up threads.
# - OAUTH_LOGIN_LOCK: serializes device-login starts so two clicks cannot mint
#   two device codes. Also timeout-guarded.
# - IDEMPOTENCY_LOCK guards the in-memory idempotency store below. Keys act as
#   a per-key mutex across retries: the in-flight reservation in
#   _mutate_idempotently is claimed under the lock, the request body executes
#   outside it, and the reservation is released on failure.
NETWORK_POLICY_LOCK = threading.Lock()
OAUTH_LOGIN_LOCK = threading.Lock()
# Idempotency replay records live in process memory, not the database: they
# are a retry convenience with a 24-hour horizon, and a service restart —
# which already fails in-flight tasks — simply resets them, so a retried key
# re-executes at most once per process lifetime.
IDEMPOTENCY_ENTRIES: dict[str, dict[str, Any]] = {}
IDEMPOTENCY_LOCK = threading.Lock()


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

    def log_message(self, format: str, *args: object) -> None:
        return

    def _handle(self, method: str) -> None:
        try:
            path = urlparse(self.path)
            if method == "GET" and path.path in UI_ASSETS:
                self._send_ui_asset(path.path)
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
        # Atomically claim the key: under the lock, either return a completed
        # response, reject an in-flight duplicate, or reserve the key. Without
        # the reservation, two concurrent retries with the same key both pass
        # a check-then-act gap and execute twice — exactly what idempotency
        # must prevent.
        with IDEMPOTENCY_LOCK:
            now = time.time()
            prune_idempotency(IDEMPOTENCY_ENTRIES, now=now)
            stored = IDEMPOTENCY_ENTRIES.get(key)
            if stored is not None:
                if stored["method"] != method or stored["path"] != path.path:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "Idempotency-Key was already used for a different request")
                if stored.get("in_flight"):
                    raise ApiError(HTTPStatus.CONFLICT, "a request with this Idempotency-Key is already in progress")
                # Honor the 24h retention on the read path too, not only when a
                # new key is stored.
                if now - stored["stored_at"] <= IDEMPOTENCY_RETENTION_SECONDS:
                    return stored["response"]
            IDEMPOTENCY_ENTRIES[key] = {"method": method, "path": path.path, "in_flight": True, "stored_at": now}
            prune_idempotency(IDEMPOTENCY_ENTRIES, now=now, preserve_key=key)
        # Execute outside the lock so a slow mutation (runtime spawn, policy
        # subprocess, process shutdown) never blocks other requests.
        try:
            response = route(method, path.path, parse_qs(path.query), body)
        except BaseException:
            # Release the reservation so a retry can proceed after a failure.
            with IDEMPOTENCY_LOCK:
                entry = IDEMPOTENCY_ENTRIES.get(key)
                if entry is not None and entry.get("in_flight"):
                    del IDEMPOTENCY_ENTRIES[key]
            raise
        with IDEMPOTENCY_LOCK:
            now = time.time()
            IDEMPOTENCY_ENTRIES[key] = {
                "method": method, "path": path.path, "response": response, "stored_at": now,
            }
            prune_idempotency(IDEMPOTENCY_ENTRIES, now=now, preserve_key=key)
        return response

    def _send_ui_asset(self, path: str) -> None:
        filename, content_type = UI_ASSETS[path]
        data = (Path(__file__).parent / filename).read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
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
    if path == "/v1/agent-files" and method == "GET":
        return agent_file_list(_agent_file_path(query))
    if path == "/v1/agent-files/read" and method == "GET":
        return agent_file_read(_agent_file_path(query))
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


def agent_file_list(path: str) -> dict[str, Any]:
    return _run_agent_file_helper("list", path)


def agent_file_read(path: str) -> dict[str, Any]:
    return _run_agent_file_helper("read", path)


def _run_agent_file_helper(action: str, path: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [*AGENT_FILE_HELPER_COMMAND, action, path],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=AGENT_FILE_HELPER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ApiError(HTTPStatus.GATEWAY_TIMEOUT, "agent file helper timed out") from exc
    except PermissionError as exc:
        # The timeout path kills the child, but the helper starts as root via
        # sudo before demoting to the agent user, so the unprivileged service
        # user's kill can raise PermissionError in place of TimeoutExpired.
        raise ApiError(
            HTTPStatus.GATEWAY_TIMEOUT,
            "agent file helper timed out (the root helper could not be terminated)",
        ) from exc
    if proc.returncode != 0:
        message = _helper_error_message(proc.stdout, proc.stderr)
        status = {
            2: HTTPStatus.NOT_FOUND,
            3: HTTPStatus.BAD_REQUEST,
            4: HTTPStatus.BAD_REQUEST,
        }.get(proc.returncode, HTTPStatus.INTERNAL_SERVER_ERROR)
        raise ApiError(status, message or "agent file helper failed")
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "agent file helper returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "agent file helper returned invalid JSON")
    return value


def _helper_error_message(stdout: str, stderr: str) -> str:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return stderr.strip()
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
    return stderr.strip()


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
    version = version_status()
    if network_status == "active" and not proxy_alive():
        network_status = "error"  # policy says active but nothing is enforcing it
    degraded = (
        any(item["status"] == "error" for item in runtime["runtimes"])
        or network_status == "error"
        or version["status"] != "ok"
    )
    return {
        "status": "degraded" if degraded else "ok",
        "agent_name": load_config().get("agent_name"),
        "version": version,
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
    """Bound admin-state growth: keep all active tasks plus the most recent
    finished ones, cap the thread->session maps and the event log, and drop
    expired/excess idempotency entries. Runs on a schedule (maintenance_loop),
    never on the request path; the deletes are indexed and touch only rows
    beyond the caps."""
    with state.mutation() as cur:
        state.prune_finished_tasks(cur, FINISHED_TASK_LIMIT)
        # A dropped thread mapping is not an error — a later task on it just
        # starts a fresh runtime conversation.
        state.prune_thread_sessions(cur, "codex", THREAD_MAP_LIMIT)
        state.prune_thread_sessions(cur, "claude_code", THREAD_MAP_LIMIT)
        state.prune_agent_events(cur)
    with IDEMPOTENCY_LOCK:
        prune_idempotency(IDEMPOTENCY_ENTRIES, now=time.time())


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
        if orchestrator.runtime_status("codex") != "awaiting_login":
            raise ApiError(HTTPStatus.CONFLICT, "Codex OAuth login is only available while awaiting_login")
        oauth = state.oauth_login("codex")
        if oauth:
            return {key: oauth[key] for key in ("status", "device_code", "login_url", "expires_at")}
        login = codex_app_server.start_device_login()
        response = {
            "status": "awaiting_login",
            "device_code": login.user_code,
            "login_url": login.verification_url,
            "expires_at": _minutes_from_now(10),
        }
        with state.mutation() as cur:
            if not orchestrator.runtime_network_enabled("codex") or orchestrator.runtime_status("codex") != "awaiting_login":
                codex_app_server.close_login_server()
                raise ApiError(HTTPStatus.CONFLICT, "Codex OAuth login is only available while awaiting_login")
            state.set_oauth_login(cur, "codex", response | {"login_id": login.login_id})
        return response
    finally:
        OAUTH_LOGIN_LOCK.release()


def current_codex_oauth_login() -> dict[str, str]:
    if not orchestrator.runtime_network_enabled("codex"):
        raise ApiError(HTTPStatus.CONFLICT, "Codex OAuth login is unavailable while OpenAI provider access is disabled")
    if orchestrator.runtime_status("codex") != "awaiting_login":
        raise ApiError(HTTPStatus.CONFLICT, "Codex OAuth login is only available while awaiting_login")
    oauth = state.oauth_login("codex")
    if not oauth:
        raise ApiError(HTTPStatus.NOT_FOUND, "Codex OAuth login has not been started")
    return {key: oauth[key] for key in ("status", "device_code", "login_url", "expires_at")}


def start_claude_oauth_login() -> dict[str, str]:
    if not OAUTH_LOGIN_LOCK.acquire(timeout=OAUTH_LOGIN_LOCK_TIMEOUT_SECONDS):
        raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is already starting")
    try:
        if not orchestrator.runtime_network_enabled("claude_code"):
            raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is unavailable while Claude provider access is disabled")
        if orchestrator.runtime_status("claude_code") != "awaiting_login":
            raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is only available while awaiting_login")
        oauth = state.oauth_login("claude")
        if oauth is not None:
            return {key: oauth[key] for key in ("status", "login_url", "expires_at")}
        login = claude_code.start_oauth_login()
        response = {
            "status": "awaiting_code",
            "login_url": login.login_url,
            "expires_at": _minutes_from_now(10),
        }
        with state.mutation() as cur:
            if not orchestrator.runtime_network_enabled("claude_code") or orchestrator.runtime_status("claude_code") != "awaiting_login":
                claude_code.close_login_process()
                raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is only available while awaiting_login")
            state.set_oauth_login(cur, "claude", response)
        return response
    finally:
        OAUTH_LOGIN_LOCK.release()


def current_claude_oauth_login() -> dict[str, str]:
    if not orchestrator.runtime_network_enabled("claude_code"):
        raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is unavailable while Claude provider access is disabled")
    if orchestrator.runtime_status("claude_code") != "awaiting_login":
        raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is only available while awaiting_login")
    oauth = state.oauth_login("claude")
    if oauth is None:
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
    with state.mutation() as cur:
        state.set_oauth_login(cur, "claude", None)
    orchestrator.refresh_runtime_status("claude_code")
    return {"status": "accepted"}


def current_agent_accounts() -> dict[str, Any]:
    statuses = orchestrator.all_runtime_status_records()
    return {"accounts": [_current_agent_account(statuses, "codex"), _current_agent_account(statuses, "claude_code")]}


def _current_agent_account(statuses: dict[str, dict[str, Any]], runtime_type: str) -> dict[str, Any]:
    status = str(statuses.get(runtime_type, {}).get("status", "loading"))
    if runtime_type == "claude_code":
        response = {"agent_runtime": "claude_code", "provider": "claude", "status": status}
        if status != "active":
            return response
        account = read_claude_account()
        response.update(_account_response_metadata(account, runtime_type))
        return response
    response = {"agent_runtime": "codex", "provider": "openai", "status": status}
    if status != "active":
        return response
    account = read_openai_account()
    response.update(_account_response_metadata(account, runtime_type))
    return response


def _account_response_metadata(account: dict[str, Any], runtime_type: str) -> dict[str, Any]:
    response: dict[str, Any] = {}
    for key in ("account_id", "email"):
        value = account.get(key)
        if isinstance(value, str) and value:
            response[key] = value
    plan_type = _normalize_plan_type(account)
    if plan_type:
        response["plan_type"] = plan_type
    if runtime_type == "codex":
        codex_usage = _normalize_codex_usage(account.get("codex_usage"))
        if codex_usage:
            response["codex_usage"] = codex_usage
    if runtime_type == "claude_code":
        claude_usage = _normalize_claude_usage(account.get("claude_usage"))
        if claude_usage:
            response["claude_usage"] = claude_usage
    return response


def _normalize_plan_type(account: dict[str, Any]) -> str | None:
    for key in ("plan_type", "planType", "plan"):
        value = account.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _normalize_codex_usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    last_checked_at = _public_metadata_scalar(value.get("last_checked_at"))
    if isinstance(last_checked_at, str):
        result["last_checked_at"] = last_checked_at
    rate_limits = _normalize_rate_limit_snapshot(value.get("rate_limits"))
    if rate_limits:
        result["rate_limits"] = rate_limits
    return result


def _normalize_claude_usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("current_session_used_percent", "weekly_used_percent"):
        scalar = _public_metadata_scalar(value.get(key))
        if type(scalar) in (int, float):
            result[key] = scalar
    resets_at_text = value.get("weekly_resets_at_text")
    if isinstance(resets_at_text, str) and resets_at_text:
        result["weekly_resets_at_text"] = resets_at_text
    last_checked_at = _public_metadata_scalar(value.get("last_checked_at"))
    if isinstance(last_checked_at, str):
        result["last_checked_at"] = last_checked_at
    expected_keys = {
        "current_session_used_percent",
        "weekly_used_percent",
        "weekly_resets_at_text",
        "last_checked_at",
    }
    return result if set(result) == expected_keys else {}


def _normalize_rate_limit_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("primary", "secondary"):
        window = _normalize_rate_limit_window(value.get(key))
        if window:
            result[key] = window
    credits = _normalize_credits(value.get("credits"))
    if credits:
        result["credits"] = credits
    return result


def _normalize_rate_limit_window(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("used_percent", "window_duration_mins", "resets_at"):
        scalar = _public_metadata_scalar(value.get(key))
        if scalar is not None:
            result[key] = scalar
    return result


def _normalize_credits(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("has_credits", "unlimited", "balance"):
        scalar = _public_metadata_scalar(value.get(key))
        if scalar is not None:
            result[key] = scalar
    return result


def _public_metadata_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return value or None
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
        return value
    return None


def create_task(body: Any) -> dict[str, Any]:
    input_message = _message(body, "input_message")
    thread_id = _thread_id(body)
    agent_runtime = _agent_runtime(body)
    with state.mutation() as cur:
        existing_runtime = state.thread_task_runtime(cur, thread_id) or state.thread_session_runtime(cur, thread_id)
        if existing_runtime is not None and existing_runtime != agent_runtime:
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"thread_id is already used by {existing_runtime}; use that runtime or choose a new thread_id",
            )
        if state.queued_task_count(cur) >= QUEUED_TASK_LIMIT:
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"task queue is full ({QUEUED_TASK_LIMIT} queued tasks); cancel queued tasks or wait",
            )
        task_id = f"task_{state.allocate_task_number(cur)}"
        now = utc_now()
        task = {
            "task_id": task_id,
            "status": QUEUED,
            "agent_runtime": agent_runtime,
            "thread_id": thread_id,
            "input_message": input_message,
            "created_at": now,
            "updated_at": now,
        }
        state.insert_task(cur, task)
    orchestrator.WORKER_WAKE.set()
    return public_task(task)


def list_tasks(last_seen_task_id: str | None) -> dict[str, Any]:
    tasks = state.active_tasks()
    ordered = _queue_order(tasks)
    start = 0
    if last_seen_task_id:
        for index, task in enumerate(ordered):
            if task[1]["task_id"] == last_seen_task_id:
                start = index + 1
                break
    return {"tasks": [public_task(task, queue_position=position) for position, task in ordered[start : start + TASK_LIMIT]]}


def list_threads() -> dict[str, Any]:
    threads = {summary["thread_id"]: summary for summary in state.thread_summaries()}
    for runtime, thread_id, last_used_at in state.session_summaries():
        _merge_thread_mapping(threads, thread_id, runtime, {"last_used_at": last_used_at})
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
    return {"tasks": [public_task(task) for task in state.tasks_for_thread(thread_id, THREAD_TASK_LIMIT)]}


def get_task(task_id: str) -> dict[str, Any]:
    return public_task(_require_task(state.get_task(task_id)))


def update_task(task_id: str, body: Any) -> dict[str, Any]:
    input_message = _message(body, "input_message")
    with state.mutation() as cur:
        task = _require_task(state.get_task(task_id, cur))
        if task["status"] != QUEUED:
            raise ApiError(HTTPStatus.CONFLICT, "only queued tasks can be updated")
        task["input_message"] = input_message
        task["updated_at"] = utc_now()
        state.save_task(cur, task)
        return public_task(task)


def steer_task(task_id: str, body: Any) -> dict[str, str]:
    steer_message = _message(body, "steer_message")
    with state.mutation() as cur:
        task = _require_task(state.get_task(task_id, cur))
        if task["status"] != RUNNING:
            raise ApiError(HTTPStatus.CONFLICT, "only running tasks can be steered")
        if state.pending_steer_count(cur, task_id) >= PENDING_STEER_LIMIT:
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"task already has {PENDING_STEER_LIMIT} undelivered steer messages; wait for delivery",
            )
        state.append_task_steer(cur, task_id, steer_message)
        task["updated_at"] = utc_now()
        state.save_task(cur, task)
        state.append_agent_event(cur, "task.message", task_id, {"message": steer_message, "source": "user"})
    return {"status": "accepted"}


def cancel_task(task_id: str) -> dict[str, str]:
    with state.mutation() as cur:
        task = _require_task(state.get_task(task_id, cur))
        if task["status"] != QUEUED:
            raise ApiError(HTTPStatus.CONFLICT, "only queued tasks can be cancelled")
        task_status.set_status(task, CANCELLED, now=utc_now())
        state.save_task(cur, task)
        state.append_agent_event(cur, "task.cancelled", task_id, {})
    return {"status": "accepted"}


def kill_task(task_id: str) -> dict[str, str]:
    with state.mutation() as cur:
        task = _require_task(state.get_task(task_id, cur))
        if task["status"] != RUNNING:
            raise ApiError(HTTPStatus.CONFLICT, "only running tasks can be killed")
        task_status.set_status(task, CANCELLED, now=utc_now())
        state.save_task(cur, task)
        state.append_agent_event(cur, "task.cancelled", task_id, {})
    # Kill the task's runtime process outside the mutation (closing can be
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
        # The validated policy goes straight to the database row the proxy
        # reads (the proxy role cannot write it back). Only stored after
        # parse_network_controls above accepted it — same validation the old
        # root helper performed.
        policy = parsed.to_json()
        updated_at = utc_now()
        state.save_network_policy(policy, updated_at)
        orchestrator.reconcile_runtime_status_after_policy_change()
        return {"network_controls": policy, "updated_at": updated_at}
    finally:
        NETWORK_POLICY_LOCK.release()


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


def _require_task(task: dict[str, Any] | None) -> dict[str, Any]:
    if task is None:
        raise ApiError(HTTPStatus.NOT_FOUND, "task not found")
    return task


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


def _one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    if len(values) != 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must appear once")
    return values[0]


def _agent_file_path(query: dict[str, list[str]]) -> str:
    value = _one(query, "path")
    if value is None or value == "":
        return "/"
    if "\0" in value:
        raise ApiError(HTTPStatus.BAD_REQUEST, "path contains a NUL byte")
    if len(value) > 4096:
        raise ApiError(HTTPStatus.BAD_REQUEST, "path is too long")
    return value


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
    worker attached anymore, so fail it rather than leave it running forever.
    (Idempotency replay records live in this process's memory, so a restart
    already cleared them along with any in-flight reservations.)"""
    error_message = "host runtime restarted while the task was running"
    with state.mutation() as cur:
        for task_id in state.fail_running_tasks(cur, error_message):
            state.append_agent_event(cur, "task.failed", task_id, {"error_message": error_message})


def main() -> int:
    # Schema migrations are deploy-plane work: bootstrap runs `migrate up`
    # before services start, and the service itself never migrates — a stray
    # service start can therefore never move the schema under other code, and
    # a schema/code mismatch (unsupported) fails loudly here instead of being
    # papered over.
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
