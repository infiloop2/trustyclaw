"""Localhost admin API (127.0.0.1:7443), reached through an operator endpoint.

The supported endpoint paths are SSH port forwarding and the optional
Cloudflare Tunnel. API routes require the admin bearer; only static admin/app
assets and the side-effect-free OAuth callback shell are unauthenticated.

Route handlers validate the documented protocol and update admin state in the
local Postgres database (through the storage accessors in ``state``);
running tasks through the selected agent runtime is delegated to
``orchestrator``, which owns the worker pool and live task processes.
Operations that require root or agent-user authority cross through fixed
root-owned sudo helpers. Database-backed host state is updated directly under
the admin database role.

Authentication compares a SHA-256 hash of the presented bearer password
against ``admin_password_sha256`` from the config table, so the cleartext
password is never persisted on the host.
"""

from __future__ import annotations

import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Callable, NamedTuple
from urllib.parse import parse_qs, urlparse

from host.config import AGENT_RUNTIMES, ConfigError, parse_network_controls
from host.constants import ADMIN_API_PORT, LOOPBACK, MAX_REQUEST_BODY_BYTES, PROXY_PORT
from host.network_integrations.bedrock.manifest import SUPPORTED_REGIONS as BEDROCK_REGIONS
from host.network_integrations.github.push_gate import pending as github_pending_push
from host.session_options import session_config_error
# app_backend_admin_api imports this module back to dispatch through route().
# The cycle is safe with plain module imports: each side binds the module
# object and reads its attributes only at request time, never during import.
from host.runtime.admin_api import app_api_proxy, app_backend_api as app_backend_admin_api, bedrock_credentials, claude_code, codex_app_server, github_credential, github_repo_audit, orchestrator, task_status, tools_client as tools_admin_api, upgrade_check
from host.runtime.core import app_platform, network_policy, state
from host.runtime.tools import tools_host
from host.runtime.admin_api.orchestrator import agent_runtime_status
from host.runtime.core.state import (
    TASK_LIMIT,
    load_config,
    page_agent_events_before,
    page_task_events,
    read_claude_account,
    read_openai_account,
    utc_now,
)
from host.runtime.admin_api.task_status import CANCELLED, QUEUED, RUNNING
from host.version import version_status


HOST = LOOPBACK
PORT = ADMIN_API_PORT
RUNTIME_DIR = Path(__file__).parent
TOOLS_DIR = RUNTIME_DIR.parents[1] / "tools"


def _tool_guide_assets() -> dict[str, tuple[Path, str]]:
    routes: dict[str, tuple[Path, str]] = {}
    for asset in sorted(TOOLS_DIR.glob("**/guide_assets/**/*.png")):
        route = f"/guide-assets/{asset.name}"
        if route in routes:
            raise RuntimeError(f"duplicate tool guide asset filename: {asset.name}")
        routes[route] = (asset, "image/png")
    return routes


UI_ASSETS = {
    "/": (RUNTIME_DIR / "admin_ui.html", "text/html; charset=utf-8"),
    # The page the operator registers as the OAuth redirect URI for tool
    # connect flows; the SPA reads the code/state query parameters on load.
    "/oauth/callback": (RUNTIME_DIR / "admin_ui.html", "text/html; charset=utf-8"),
    "/admin_ui.css": (RUNTIME_DIR / "admin_ui.css", "text/css; charset=utf-8"),
    "/favicon.ico": (RUNTIME_DIR / "admin_favicon.svg", "image/svg+xml"),
    "/favicon.svg": (RUNTIME_DIR / "admin_favicon.svg", "image/svg+xml"),
    "/workspace-kit/view_blocks.css": (
        app_platform.APP_ROOT / "workspace_kit" / "ui" / "view_blocks.css",
        "text/css; charset=utf-8",
    ),
    "/workspace-kit/view_blocks.js": (
        app_platform.APP_ROOT / "workspace_kit" / "ui" / "view_blocks.js",
        "application/javascript; charset=utf-8",
    ),
}
# The admin UI ships as native ES modules in host/runtime/admin_ui/. The
# served set is fixed at startup from the files present, so any other
# /admin_ui/ path stays a 404.
UI_ASSETS.update({
    f"/admin_ui/{module.name}": (module, "application/javascript; charset=utf-8")
    for module in sorted((RUNTIME_DIR / "admin_ui").glob("*.js"))
})
# Provider setup screenshots live with their owning tool integration. They are
# audited release assets and never load from provider domains in the operator's
# browser. Public filenames are unique across tools so manifests stay portable.
UI_ASSETS.update(_tool_guide_assets())
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "base-uri 'none'; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data:; "
        "media-src blob:; "
        "object-src 'none'; "
        "script-src 'self'; "
        "style-src 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
UNTRUSTED_FILE_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox",
    "Content-Disposition": "inline",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MESSAGE_LIMIT = 50_000
MAINTENANCE_INTERVAL_SECONDS = 3600  # scheduled state cleanup cadence (not per-request)
FINISHED_TASK_LIMIT = 100_000  # finished tasks kept as history before the oldest are pruned
THREAD_TASK_LIMIT = 1000
THREAD_TASK_MESSAGE_BYTES_LIMIT = 200_000
THREAD_MAP_LIMIT = 100_000  # user thread -> runtime session mappings kept before LRU pruning
# Queued tasks and undelivered steers are the two operator-driven inputs that
# would otherwise grow admin state without bound (active tasks are never
# pruned; steers queue until the worker delivers them). Both caps return 409.
QUEUED_TASK_LIMIT = 1000
PENDING_STEER_LIMIT = 20
OAUTH_LOGIN_LOCK_TIMEOUT_SECONDS = 5
# OAuth login can start while awaiting login or in error: error states (a
# changed account, malformed local credentials) are recovered by simply
# logging in again — resetting the linked account never has to fix them first.
OAUTH_LOGIN_STATUSES = ("awaiting_login", "error")
REBOOT_HELPER_TIMEOUT_SECONDS = 10
AGENT_FILE_HELPER_TIMEOUT_SECONDS = 10
AGENT_FILE_HELPER_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/read-agent-file"]
AGENT_FILE_UPLOAD_HELPER_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/upload-agent-file"]
AGENT_FILE_UPLOAD_MAX_BYTES = 25 * 1024 * 1024
AGENT_FILE_UPLOAD_FILENAME_MAX_BYTES = 200
AGENT_FILE_STREAM_MAX_BYTES = 200_000_000
AGENT_FILE_STREAM_MEDIA_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime"}
AGENT_AUTH_CLEAR_HELPER_TIMEOUT_SECONDS = 10
AGENT_AUTH_CLEAR_HELPER_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/clear-agent-auth"]
AGENT_CGROUP_ROOT = Path("/sys/fs/cgroup/trustyclaw_agent.slice")
PROC_ROOT = Path("/proc")
AGENT_PROCESS_LIMIT = 1000
APP_SCOPED_ID_SEPARATOR = app_platform.APP_SCOPED_ID_SEPARATOR
# Lock inventory for this module (each request runs on its own handler
# thread, so every handler is concurrent with every other and with the
# orchestrator's workers):
# - The mutation lock (private to state.py, entered through state.mutation()):
#   every admin-state write cycle. Held briefly; slow work (runtime spawns,
#   helper subprocesses, process closes) always runs outside the mutation so
#   reads and /v1/health never stall behind it. Reads are lock-free queries.
# - OAUTH_LOGIN_LOCK: serializes device-login starts so two clicks cannot mint
#   two device codes (the mint runs outside the mutation lock, so that lock
#   alone cannot prevent a double mint, which would leak a login process).
#   Timeout-guarded so a stuck mint returns 409 instead of piling up threads.
OAUTH_LOGIN_LOCK = threading.Lock()


# ApiError is defined in a shared module so it is one class whether admin_api is
# loaded as __main__ (the service) or as host.runtime.admin_api.service (by the modules
# it dispatches to, e.g. tools_admin_api). See host/runtime/admin_api/errors.py.
from host.runtime.admin_api.errors import ApiError


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
            if method == "GET":
                app_asset = app_platform.ui_asset(path.path)
                if app_asset is not None:
                    self._send_app_ui_asset(*app_asset)
                    return
            self._authenticate()
            bridge_app_id = self.headers.get("X-TrustyClaw-App-Bridge", "") or None
            if method == "GET" and path.path == "/v1/agent-files/content":
                self._send_agent_file(_agent_file_path(parse_qs(path.query)))
                return
            if method == "POST" and path.path == "/v1/agent-files/upload":
                if bridge_app_id is not None:
                    raise ApiError(HTTPStatus.FORBIDDEN, "app bridge requests may only target the app's own API")
                self._send_agent_file_upload(parse_qs(path.query))
                return
            response = route(
                method, path.path, parse_qs(path.query), self._read_body(), bridge_app_id=bridge_app_id
            )
            self._send_json(HTTPStatus.OK, response)
        except ApiError as exc:
            self._send_json(exc.status, {"error": {"message": exc.message}})
        except app_platform.AppError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": str(exc)}})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": str(exc)}})

    def _send_ui_asset(self, path: str) -> None:
        asset, content_type = UI_ASSETS[path]
        data = asset.read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._send_ui_cache_headers()
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_app_ui_asset(self, app: app_platform.AppManifest, asset: Path, content_type: str) -> None:
        data = asset.read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._send_ui_cache_headers()
        self._send_app_ui_security_headers(app)
        self.end_headers()
        self.wfile.write(data)

    def _send_ui_cache_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")

    def _send_security_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)

    def _send_app_ui_security_headers(self, app: app_platform.AppManifest) -> None:
        asset_origin = self._app_ui_asset_origin()
        worker_policy = "; worker-src blob:; webrtc 'block'" if app.capability_worker else ""
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; "
            "base-uri 'none'; "
            "connect-src 'none'; "
            f"font-src 'self' {asset_origin} data:; "
            "form-action 'none'; "
            "frame-ancestors 'self'; "
            "frame-src 'none'; "
            f"img-src 'self' {asset_origin} data:; "
            "navigate-to 'self'; "
            "object-src 'none'; "
            "sandbox allow-scripts allow-forms allow-modals; "
            f"script-src 'self' {asset_origin}; "
            f"style-src 'self' 'unsafe-inline' {asset_origin}"
            f"{worker_policy}",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")

    def _app_ui_asset_origin(self) -> str:
        host = self.headers.get("Host", "")
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
            host = f"{LOOPBACK}:{ADMIN_API_PORT}"
        forwarded_proto = self.headers.get("X-Forwarded-Proto", "")
        scheme = forwarded_proto if forwarded_proto in {"http", "https"} else "http"
        return f"{scheme}://{host}"

    def _authenticate(self) -> None:
        expected = load_config().get("admin_password_sha256", "")
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            presented = hashlib.sha256(auth.removeprefix("Bearer ").encode()).hexdigest()
            if expected and hmac.compare_digest(presented, expected):
                return
            raise ApiError(HTTPStatus.UNAUTHORIZED, "missing or invalid admin password")
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
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_agent_file(self, path: str) -> None:
        expected_media_type = AGENT_FILE_STREAM_MEDIA_TYPES.get(Path(path).suffix.lower())
        if expected_media_type is None:
            raise ApiError(HTTPStatus.BAD_REQUEST, "agent file streaming supports only MP4 or MOV video files")
        process = subprocess.Popen(
            [*AGENT_FILE_HELPER_COMMAND, "stream", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        try:
            raw_header = process.stdout.readline(4097)
            if len(raw_header) > 4096 or not raw_header.endswith(b"\n"):
                process.kill()
                raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "agent file helper returned an invalid stream header")
            try:
                header = json.loads(raw_header)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                process.wait(timeout=AGENT_FILE_HELPER_TIMEOUT_SECONDS)
                raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "agent file helper returned invalid JSON") from exc
            if not isinstance(header, dict) or "size_bytes" not in header:
                process.wait(timeout=AGENT_FILE_HELPER_TIMEOUT_SECONDS)
                status = {
                    2: HTTPStatus.NOT_FOUND,
                    3: HTTPStatus.BAD_REQUEST,
                    4: HTTPStatus.BAD_REQUEST,
                }.get(process.returncode, HTTPStatus.INTERNAL_SERVER_ERROR)
                message = header.get("error", {}).get("message") if isinstance(header, dict) else None
                raise ApiError(status, str(message or "agent file helper failed"))
            size_bytes = header.get("size_bytes")
            media_type = header.get("media_type")
            if (
                not isinstance(size_bytes, int)
                or not 0 <= size_bytes <= AGENT_FILE_STREAM_MAX_BYTES
                or media_type != expected_media_type
            ):
                process.kill()
                raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "agent file helper returned invalid metadata")
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", expected_media_type)
            self.send_header("Content-Length", str(size_bytes))
            self._send_ui_cache_headers()
            for name, value in UNTRUSTED_FILE_SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            remaining = size_bytes
            try:
                while remaining:
                    chunk = process.stdout.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            if remaining:
                process.kill()
                self.close_connection = True
        finally:
            if process.poll() is None:
                try:
                    process.wait(timeout=AGENT_FILE_HELPER_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def _send_agent_file_upload(self, query: dict[str, list[str]]) -> None:
        filename = _agent_file_upload_filename(query)
        length = self._content_length(AGENT_FILE_UPLOAD_MAX_BYTES)
        process = subprocess.Popen(
            [*AGENT_FILE_UPLOAD_HELPER_COMMAND, filename, str(length)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        try:
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "upload ended before Content-Length bytes were received")
                process.stdin.write(chunk)
                remaining -= len(chunk)
            process.stdin.close()
            process.wait(timeout=AGENT_FILE_HELPER_TIMEOUT_SECONDS)
        except BrokenPipeError as exc:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            try:
                process.wait(timeout=AGENT_FILE_HELPER_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self._terminate_agent_file_upload_helper(process)
                raise ApiError(HTTPStatus.GATEWAY_TIMEOUT, "agent file upload helper timed out") from exc
        except subprocess.TimeoutExpired as exc:
            if process.poll() is None:
                self._terminate_agent_file_upload_helper(process)
            raise ApiError(HTTPStatus.GATEWAY_TIMEOUT, "agent file upload helper timed out") from exc
        except BaseException:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            if process.poll() is None:
                try:
                    # EOF lets the helper run its finally block and remove a
                    # partial .uploading-* file after a short client body.
                    process.wait(timeout=AGENT_FILE_HELPER_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    self._terminate_agent_file_upload_helper(process)
            raise
        stdout = process.stdout.read(64 * 1024).decode("utf-8", "replace")
        stderr = process.stderr.read(64 * 1024).decode("utf-8", "replace")
        if process.returncode != 0:
            raise ApiError(
                HTTPStatus.BAD_REQUEST if process.returncode == 2 else HTTPStatus.INTERNAL_SERVER_ERROR,
                _helper_error_message(stdout, stderr) or "agent file upload helper failed",
            )
        try:
            uploaded = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "agent file upload helper returned invalid JSON") from exc
        if (
            not isinstance(uploaded, dict)
            or not isinstance(uploaded.get("name"), str)
            or uploaded.get("original_name") != filename
            or uploaded.get("path") != f"user-files/{uploaded.get('name')}"
            or uploaded.get("size_bytes") != length
            or not isinstance(uploaded.get("uploaded_at"), str)
        ):
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "agent file upload helper returned invalid JSON")
        self._send_json(HTTPStatus.OK, {"file": uploaded})


    @staticmethod
    def _terminate_agent_file_upload_helper(process: subprocess.Popen[bytes]) -> None:
        try:
            process.kill()
            process.wait(timeout=AGENT_FILE_HELPER_TIMEOUT_SECONDS)
        except (PermissionError, subprocess.TimeoutExpired):
            # The admin user may not be allowed to signal a sudo helper after
            # it demotes. Never turn the request timeout into an unbounded wait.
            pass

    def _content_length(self, maximum: int) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise ApiError(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
        try:
            length = int(raw)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "malformed Content-Length") from exc
        if length < 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "malformed Content-Length")
        if length > maximum:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"upload exceeds {maximum} bytes")
        return length


def route(
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: Any,
    *,
    bridge_app_id: str | None = None,
    app_backend_id: str | None = None,
) -> Any:
    # A bridge-tagged request (the admin shell forwarding an app iframe's
    # postMessage) may only target that app's own API surface — enforced here,
    # before any route dispatch, so no host route is reachable through the
    # bridge. Un-normalized dot-segment paths that pass the literal prefix
    # check are proxied into the app's own backend, which 404s them.
    if bridge_app_id is not None and not path.startswith(f"/v1/apps/{bridge_app_id}/api/"):
        raise ApiError(HTTPStatus.FORBIDDEN, "app bridge requests may only target the app's own API")
    if method == "GET" and path == "/v1/health":
        return health()
    if method == "GET" and path == "/v1/agent-runtime/status":
        return agent_runtime_status()
    if method == "GET" and path == "/v1/agent-runtime/account":
        if query:
            raise ApiError(HTTPStatus.BAD_REQUEST, "agent-runtime account endpoint does not accept query parameters")
        return current_agent_accounts()
    if method == "POST" and path == "/v1/agent-runtime/refresh":
        return refresh_agent_runtime_accounts(body)
    if path == "/v1/apps" and method == "GET":
        return list_apps()
    if path.startswith("/v1/apps/"):
        return app_api_proxy.route_app_request(method, path, query, body)
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
    if path == "/v1/agent-runtime/bedrock-credentials":
        if method == "GET":
            return current_bedrock_credentials()
        if method == "POST":
            return connect_bedrock_credentials(body)
        if method == "DELETE":
            return disconnect_bedrock_credentials()
    if path == "/v1/agent-runtime/reset-linked-account" and method == "POST":
        return reset_linked_account(body)
    if path == "/v1/tasks":
        if method == "POST":
            return create_task(body, app_backend_id=app_backend_id)
        if method == "GET":
            return list_tasks(_one(query, "last_seen_task_id"))
    if path.startswith("/v1/tasks/"):
        return task_route(method, path, query, body)
    if path == "/v1/threads" and method == "GET":
        return list_threads()
    if path.startswith("/v1/threads/"):
        return thread_route(method, path, query)
    if path == "/v1/events" and method == "GET":
        _reject_query_keys(query, {"before", "limit"}, "event")
        return {
            "events": page_agent_events_before(
                _optional_non_negative_int(query, "before"),
                limit=_event_page_limit(query),
            )
        }
    if path == "/v1/network/policy":
        if method == "GET":
            return network_policy.network_policy_response()
        if method == "PUT":
            return replace_network_policy(body)
    if path == "/v1/tools/events" and method == "GET":
        _reject_query_keys(query, {"before", "limit"}, "tool event")
        return {
            "events": state.page_tool_events_before(
                _optional_non_negative_int(query, "before"),
                limit=_event_page_limit(query),
            )
        }
    tool_event_match = re.fullmatch(r"/v1/tools/events/([1-9][0-9]*)", path)
    if tool_event_match and method == "GET":
        if query:
            raise ApiError(HTTPStatus.BAD_REQUEST, "tool event detail does not accept query parameters")
        event = state.tool_event(int(tool_event_match.group(1)))
        if event is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "tool event not found")
        return {"event": event}
    if path == "/v1/tools" or path.startswith("/v1/tools/"):
        return tools_admin_api.tools_route(method, path, body)
    if path == "/v1/network/events" and method == "GET":
        _reject_query_keys(query, {"before", "decision", "limit"}, "network event")
        return {
            "events": state.page_network_events_before(
                _optional_non_negative_int(query, "before"),
                decision=_network_event_decision(query),
                limit=_event_page_limit(query),
            )
        }
    if path == "/v1/network-tools/github-credential":
        # Deliberately not gated on the GitHub integration being enabled:
        # staging the credential first, then enabling, is the flow that never
        # leaves the proxy allowing repositories with no working token.
        # reconcile() ties the published token to enablement either way.
        if method == "GET":
            return _credential_response(github_credential.metadata())
        if method == "PUT":
            return replace_github_credential(body)
        if method == "DELETE":
            deleted = github_credential.delete()
            github_repo_audit.refresh(force=True)
            return _credential_response(deleted)
    if path == "/v1/network-tools/github-audit" and method == "POST":
        # The UI's re-check action: re-converge with a fresh mint first
        # (grants may have changed on GitHub), force-refresh the repository
        # audits with that published token, and
        # return the updated credential view (warnings included).
        github_credential.reconcile(mint_fresh=True)
        github_repo_audit.refresh(force=True)
        return _credential_response(github_credential.metadata())
    if path == "/v1/network-tools/github-pending-pushes" and method == "GET":
        return {"pending_pushes": state.read_pending_pushes()}
    if path.startswith("/v1/network-tools/github-pending-pushes/") and method == "POST":
        parts = [part for part in path.split("/") if part]
        # .../github-pending-pushes/<id>/<approve|reject>
        if len(parts) == 5 and parts[4] in ("approve", "reject"):
            return resolve_pending_push(parts[3], parts[4])
    if path == "/v1/agent-files" and method == "GET":
        return agent_file_list(_agent_file_path(query))
    if path == "/v1/agent-files/read" and method == "GET":
        return agent_file_read(_agent_file_path(query))
    if path == "/v1/agent-processes" and method == "GET":
        return agent_processes()
    if path == "/v1/host-runtime/reboot" and method == "POST":
        return reboot_host()
    raise ApiError(HTTPStatus.NOT_FOUND, "route not found")


def list_apps() -> dict[str, Any]:
    return {"apps": [app.public() for app in app_platform.installed_apps()]}


def resolve_pending_push(push_id: str, action: str) -> dict[str, Any]:
    try:
        push = github_pending_push.approve(push_id) if action == "approve" else github_pending_push.reject(push_id)
    except github_pending_push.PendingPushError as exc:
        status = HTTPStatus.NOT_FOUND if "not found" in str(exc) else HTTPStatus.CONFLICT
        raise ApiError(status, str(exc)) from exc
    return {"pending_push": push}


def replace_github_credential(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "GitHub credential request must be an object")
    mode = body.get("mode")
    if mode == "pat":
        extra = sorted(set(body) - {"mode", "token"})
        if extra:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"GitHub credential request has unsupported fields: {', '.join(extra)}")
        token = body.get("token")
        if not isinstance(token, str) or not token.strip() or any(character.isspace() for character in token):
            raise ApiError(HTTPStatus.BAD_REQUEST, "token must be a non-empty token string without whitespace")
        saved = github_credential.set_pat(token.strip())
        github_repo_audit.refresh(force=True)
        return _credential_response(saved)
    if mode == "app":
        extra = sorted(set(body) - {"mode", "app_id", "installation_id", "private_key_pem"})
        if extra:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"GitHub credential request has unsupported fields: {', '.join(extra)}")
        app_id = body.get("app_id")
        installation_id = body.get("installation_id")
        private_key_pem = body.get("private_key_pem")
        if not isinstance(app_id, str) or not re.fullmatch(r"[0-9]{1,20}", app_id.strip()):
            raise ApiError(HTTPStatus.BAD_REQUEST, "app_id must be the numeric GitHub App id")
        if not isinstance(installation_id, str) or not re.fullmatch(r"[0-9]{1,20}", installation_id.strip()):
            raise ApiError(HTTPStatus.BAD_REQUEST, "installation_id must be the numeric installation id")
        if not isinstance(private_key_pem, str) or not private_key_pem.strip().startswith("-----BEGIN"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "private_key_pem must be the GitHub App PEM private key")
        saved = github_credential.set_app(app_id.strip(), installation_id.strip(), private_key_pem.strip() + "\n")
        github_repo_audit.refresh(force=True)
        return _credential_response(saved)
    raise ApiError(HTTPStatus.BAD_REQUEST, "mode must be 'pat' or 'app'")


def _credential_response(metadata: dict[str, Any]) -> dict[str, Any]:
    """The credential metadata plus per-repository audit warnings.

    Audit summaries are still useful without a configured credential: configured
    write repositories then cannot be verified, and the UI should show that as a
    warning instead of silently reporting no audit state.
    """
    audits = github_repo_audit.summaries()
    if audits:
        metadata = {**metadata, "repository_audits": audits}
    return metadata


class HelperTimedOut(Exception):
    """A root helper ran past its timeout. could_not_terminate means the
    unprivileged kill of the sudo-spawned child failed too."""

    def __init__(self, could_not_terminate: bool) -> None:
        super().__init__("root helper timed out")
        self.could_not_terminate = could_not_terminate


def _run_root_helper(argv: list[str], timeout_seconds: int) -> "subprocess.CompletedProcess[str]":
    """Run one sudo root helper; each caller maps returncodes and
    HelperTimedOut to its own status policy."""
    try:
        return subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except (subprocess.TimeoutExpired, PermissionError) as exc:
        # On timeout, subprocess.run kills the child — but the helper runs as
        # root via sudo, so the unprivileged service user's kill raises
        # PermissionError in place of TimeoutExpired.
        raise HelperTimedOut(isinstance(exc, PermissionError)) from exc


def reboot_host() -> dict[str, str]:
    """Run the reboot helper synchronously. ``systemctl reboot`` only schedules
    the reboot and returns, so this stays fast — and a helper that fails to even
    schedule it (e.g. a broken sudoers entry) surfaces as a 500 instead of a
    silent "accepted" for a reboot that will never happen. The host goes down
    moments after the response is sent."""
    try:
        proc = _run_root_helper(
            ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/reboot-host"],
            REBOOT_HELPER_TIMEOUT_SECONDS,
        )
    except HelperTimedOut:
        # The reboot may already be in flight; report accepted rather than a
        # false failure.
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
        proc = _run_root_helper([*AGENT_FILE_HELPER_COMMAND, action, path], AGENT_FILE_HELPER_TIMEOUT_SECONDS)
    except HelperTimedOut as exc:
        message = (
            "agent file helper timed out (the root helper could not be terminated)"
            if exc.could_not_terminate
            else "agent file helper timed out"
        )
        raise ApiError(HTTPStatus.GATEWAY_TIMEOUT, message) from exc
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


def agent_processes() -> dict[str, Any]:
    """Return a bounded process snapshot for the agent runtime slice — exactly
    the fields the admin UI renders."""
    pids = sorted(_agent_slice_pids())
    uptime = _proc_uptime()
    clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    processes: list[dict[str, Any]] = []
    for pid in pids[:AGENT_PROCESS_LIMIT]:
        process = _agent_process_info(pid, uptime, clk_tck)
        if process is not None:
            processes.append(process)
    return {"processes": processes, "truncated": len(pids) > AGENT_PROCESS_LIMIT}


def _agent_slice_pids() -> set[int]:
    if not AGENT_CGROUP_ROOT.is_dir():
        return set()
    pids: set[int] = set()
    try:
        for proc_file in AGENT_CGROUP_ROOT.rglob("cgroup.procs"):
            try:
                lines = proc_file.read_text().splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line in lines:
                try:
                    pid = int(line)
                except ValueError:
                    continue
                if pid > 0:
                    pids.add(pid)
    except OSError:
        return set()
    return pids


def _agent_process_info(pid: int, uptime: float, clk_tck: int) -> dict[str, Any] | None:
    proc_dir = PROC_ROOT / str(pid)
    try:
        stat = _proc_stat(proc_dir / "stat")
        status = _proc_status(proc_dir / "status")
    except (OSError, ValueError, IndexError):
        return None
    name = status.get("Name") or stat["name"]
    cmdline = _proc_cmdline(proc_dir / "cmdline") or f"[{name}]"
    result: dict[str, Any] = {
        "pid": pid,
        "state": stat["state"],
        "name": name,
        "cmdline": cmdline,
    }
    rss_bytes = _rss_bytes(status.get("VmRSS"))
    if rss_bytes is not None:
        result["rss_bytes"] = rss_bytes
    if uptime > 0 and clk_tck > 0:
        result["elapsed_seconds"] = int(max(0.0, uptime - (stat["start_ticks"] / clk_tck)))
    return result


def _proc_stat(path: Path) -> dict[str, Any]:
    raw = path.read_text()
    left = raw.find("(")
    right = raw.rfind(")")
    if left < 0 or right <= left:
        raise ValueError("malformed proc stat")
    fields = raw[right + 2 :].split()
    return {
        "name": raw[left + 1 : right],
        "state": fields[0],
        "start_ticks": int(fields[19]),
    }


def _proc_status(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key] = value.strip()
    return values


def _proc_cmdline(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return raw.rstrip(b"\0").replace(b"\0", b" ").decode("utf-8", "replace")


def _proc_uptime() -> float:
    try:
        return float((PROC_ROOT / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _rss_bytes(rss_line: str | None) -> int | None:
    if not rss_line:
        return None
    parts = rss_line.split()
    if not parts:
        return None
    try:
        value = int(parts[0])
    except ValueError:
        return None
    unit = parts[1].lower() if len(parts) > 1 else "kb"
    return value * 1024 if unit == "kb" else value


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


def task_route(
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: Any,
) -> Any:
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
        return {"events": page_task_events(task_id, _optional_non_negative_int(query, "since"))}
    raise ApiError(HTTPStatus.NOT_FOUND, "task route not found")


def thread_route(method: str, path: str, query: dict[str, list[str]]) -> Any:
    parts = path.strip("/").split("/")
    if len(parts) == 4 and parts[3] == "tasks" and method == "GET":
        thread_id = parts[2]
        if not THREAD_ID_RE.fullmatch(thread_id):
            raise ApiError(HTTPStatus.NOT_FOUND, "thread route not found")
        _reject_query_keys(query, {"limit", "message_bytes"}, "thread task")
        return list_thread_tasks(
            thread_id,
            limit=_bounded_positive_query_int(query, "limit", THREAD_TASK_LIMIT, THREAD_TASK_LIMIT),
            message_bytes=_optional_bounded_positive_query_int(
                query, "message_bytes", THREAD_TASK_MESSAGE_BYTES_LIMIT
            ),
        )
    if len(parts) == 4 and parts[3] == "events" and method == "GET":
        thread_id = parts[2]
        if not THREAD_ID_RE.fullmatch(thread_id):
            raise ApiError(HTTPStatus.NOT_FOUND, "thread route not found")
        _reject_query_keys(query, {"since", "limit"}, "thread event")
        return {
            "events": state.page_thread_events(
                thread_id,
                _optional_non_negative_int(query, "since"),
                _event_page_limit(query),
            )
        }
    raise ApiError(HTTPStatus.NOT_FOUND, "thread route not found")


def health() -> dict[str, Any]:
    runtime = agent_runtime_status()
    network_status = network_policy.network_status()
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
        "upgrade": upgrade_check.status(),
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


def prune_state() -> None:
    """Bound admin-state growth: keep all active tasks plus the most recent
    finished ones and cap the thread->session maps. Runs on a schedule
    (maintenance_loop), never on the request path; the deletes are indexed and
    touch only rows beyond the caps. The audit logs prune themselves on their
    own append cadence."""
    with state.mutation() as cur:
        state.prune_finished_tasks(cur, FINISHED_TASK_LIMIT)
        # Retained tasks keep their canonical thread; unreferenced mappings use
        # the ordinary per-runtime LRU cap.
        for runtime_type in AGENT_RUNTIME_TYPES:
            state.prune_thread_sessions(cur, runtime_type, THREAD_MAP_LIMIT)
    # Approval expiry and history pruning use their own short mutations.
    tools_host.maintain_approvals()


def maintenance_loop() -> None:
    while True:
        time.sleep(MAINTENANCE_INTERVAL_SECONDS)
        try:
            prune_state()
        except Exception:
            pass  # maintenance is best-effort; never crash the service


def _mint_codex_login() -> tuple[dict[str, str], dict[str, str]]:
    login = codex_app_server.start_device_login()
    response = {
        "status": "awaiting_login",
        "device_code": login.user_code,
        "login_url": login.verification_url,
        "expires_at": _minutes_from_now(10),
    }
    return response, response | {"login_id": login.login_id}


def _mint_claude_login() -> tuple[dict[str, str], dict[str, str]]:
    login = claude_code.start_oauth_login()
    response = {
        "status": "awaiting_code",
        "login_url": login.login_url,
        "expires_at": _minutes_from_now(10),
    }
    return response, response


class _OAuthLoginFlow(NamedTuple):
    """One runtime's login flow: the codex and claude endpoints are the same
    machine, differing only in these fields. mint returns (public response,
    persisted record); close tears down a login whose gate re-check lost."""

    runtime_type: str
    # oauth_logins keys on the provider spelling ('claude'), not the runtime
    # type ('claude_code'); orchestrator.mark_oauth_login_completed keys the
    # same way.
    oauth_key: str
    display: str
    provider: str
    response_keys: tuple[str, ...]
    mint: Callable[[], tuple[dict[str, str], dict[str, str]]]
    close: Callable[[], None]


_OAUTH_LOGIN_FLOWS = {
    "codex": _OAuthLoginFlow(
        runtime_type="codex",
        oauth_key="codex",
        display="Codex",
        provider="OpenAI",
        response_keys=("status", "device_code", "login_url", "expires_at"),
        mint=_mint_codex_login,
        close=lambda: codex_app_server.close_login_server(),
    ),
    "claude_code": _OAuthLoginFlow(
        runtime_type="claude_code",
        oauth_key="claude",
        display="Claude",
        provider="Claude",
        response_keys=("status", "login_url", "expires_at"),
        mint=_mint_claude_login,
        close=lambda: claude_code.close_login_process(),
    ),
}


def _require_oauth_login_available(flow: _OAuthLoginFlow) -> None:
    if not orchestrator.runtime_network_enabled(flow.runtime_type):
        raise ApiError(
            HTTPStatus.CONFLICT,
            f"{flow.display} OAuth login is unavailable while {flow.provider} provider access is disabled",
        )
    if orchestrator.runtime_status(flow.runtime_type) not in OAUTH_LOGIN_STATUSES:
        raise ApiError(
            HTTPStatus.CONFLICT,
            f"{flow.display} OAuth login is only available while awaiting_login or in error",
        )


def _start_oauth_login(flow: _OAuthLoginFlow) -> dict[str, str]:
    if not OAUTH_LOGIN_LOCK.acquire(timeout=OAUTH_LOGIN_LOCK_TIMEOUT_SECONDS):
        raise ApiError(HTTPStatus.CONFLICT, f"{flow.display} OAuth login is already starting")
    try:
        _require_oauth_login_available(flow)
        oauth = state.oauth_login(flow.oauth_key)
        if oauth:
            return {key: oauth[key] for key in flow.response_keys}
        response, persisted = flow.mint()
        with state.mutation() as cur:
            # Re-check the gate inside the mutation: a policy disable or a
            # completed refresh that raced the slow mint must not park a
            # fresh login process, so the loser closes it here.
            try:
                _require_oauth_login_available(flow)
            except ApiError:
                flow.close()
                raise
            state.set_oauth_login(cur, flow.oauth_key, persisted)
        return response
    finally:
        OAUTH_LOGIN_LOCK.release()


def _current_oauth_login_response(flow: _OAuthLoginFlow) -> dict[str, str]:
    _require_oauth_login_available(flow)
    oauth = state.oauth_login(flow.oauth_key)
    if not oauth:
        raise ApiError(HTTPStatus.NOT_FOUND, f"{flow.display} OAuth login has not been started")
    return {key: oauth[key] for key in flow.response_keys}


def start_codex_oauth_login() -> dict[str, str]:
    return _start_oauth_login(_OAUTH_LOGIN_FLOWS["codex"])


def current_codex_oauth_login() -> dict[str, str]:
    return _current_oauth_login_response(_OAUTH_LOGIN_FLOWS["codex"])


def start_claude_oauth_login() -> dict[str, str]:
    return _start_oauth_login(_OAUTH_LOGIN_FLOWS["claude_code"])


def current_claude_oauth_login() -> dict[str, str]:
    return _current_oauth_login_response(_OAUTH_LOGIN_FLOWS["claude_code"])


def complete_claude_oauth_login(body: Any) -> dict[str, str]:
    if not isinstance(body, dict) or not isinstance(body.get("code"), str) or not body["code"].strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "code must be a non-empty string")
    if not orchestrator.runtime_network_enabled("claude_code"):
        raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is unavailable while Claude provider access is disabled")
    try:
        claude_code.complete_oauth_login(body["code"])
    except claude_code.ClaudeCodeError as exc:
        raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
    orchestrator.mark_oauth_login_completed("claude", _claude_completed_token_hash())
    status = orchestrator.refresh_runtime_status("claude_code")
    if status != "active":
        # The pending login record must survive until the refresh above: it is
        # the operator-approval window that lets the refresh capture the first
        # trusted account. On an active result the refresh clears it itself.
        with state.mutation() as cur:
            state.set_oauth_login(cur, "claude", None)
    return {"status": "accepted"}


def _claude_completed_token_hash() -> str | None:
    """Bind the operator approval to the token the login just wrote: first
    capture requires attesting this exact token, so agent credentials swapped
    after completion do not inherit the approval. If the read fails, the
    completion refresh cannot capture a first trusted account and the
    non-active completion path clears the spent login so the operator can
    retry."""
    try:
        account = claude_code.read_claude_account()
    except claude_code.ClaudeCodeError:
        return None
    value = account.get("access_token_sha256") if account else None
    return value if isinstance(value, str) and value else None


# Long-term IAM user access key ids only (AKIA prefix, 20 characters).
# Temporary session credentials (ASIA...) need an X-Amz-Security-Token the
# proxy deliberately denies, so rejecting them here with a clear message
# beats the generic STS failure they would otherwise hit.
BEDROCK_ACCESS_KEY_ID_RE = re.compile(r"^AKIA[0-9A-Z]{16}$")


def connect_bedrock_credentials(body: Any) -> dict[str, str]:
    """Store the operator-pasted AWS key pair and region as one connection.

    Only this operator API
    writes that row, so the stored credential is the approval. The request
    synchronously attests the key even while Bedrock is disabled; a failed
    candidate is never stored and leaves any previous validated connection
    unchanged."""
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be an object")
    unexpected = sorted(set(body) - {"access_key_id", "secret_access_key", "region"})
    if unexpected:
        raise ApiError(HTTPStatus.BAD_REQUEST, "unexpected request fields: " + ", ".join(unexpected))
    access_key_id = body.get("access_key_id")
    secret_access_key = body.get("secret_access_key")
    region = body.get("region")
    if not isinstance(access_key_id, str) or not access_key_id.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "access_key_id must be a non-empty string")
    if not BEDROCK_ACCESS_KEY_ID_RE.fullmatch(access_key_id.strip()):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "access_key_id must be a long-term IAM access key id (20 characters, AKIA prefix); "
            "temporary session credentials (ASIA...) are not supported — create a long-term "
            "access key for a dedicated IAM user instead",
        )
    if not isinstance(secret_access_key, str) or not secret_access_key.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "secret_access_key must be a non-empty string")
    if region not in BEDROCK_REGIONS:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "region must be one of " + ", ".join(BEDROCK_REGIONS),
        )
    try:
        status, error_message = orchestrator.replace_and_validate_bedrock_credentials(
            access_key_id.strip(),
            secret_access_key.strip(),
            region,
        )
    except bedrock_credentials.BedrockCredentialsError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
    if status != "active":
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            error_message or "AWS credential validation failed",
        )
    if not orchestrator.runtime_network_enabled("hermes"):
        return {"status": "accepted"}
    # Runtime refresh reads the validated row without another AWS call. The
    # proxy reads that same row directly.
    orchestrator.refresh_runtime_status("hermes")
    return {"status": "accepted"}


def current_bedrock_credentials() -> dict[str, Any]:
    """Return non-secret metadata for the validated Bedrock credential."""
    access_key_id = state.read_bedrock_access_key_id()
    response: dict[str, Any] = {"connected": access_key_id is not None}
    if access_key_id is not None:
        response["access_key_id"] = access_key_id
        region = state.read_bedrock_region()
        if region is not None:
            response["region"] = region
    return response


def disconnect_bedrock_credentials() -> dict[str, str]:
    """Delete the AWS connection and stop Hermes."""
    orchestrator.disconnect_bedrock_connection()
    return {"status": "accepted"}


def reset_linked_account(body: Any) -> dict[str, str]:
    """Delete the linked-account guard: the operator-approved anchor, its
    proxy pin, pending OAuth approval, local agent auth files, and old runtime
    processes. Callable in any runtime status."""
    if not isinstance(body, dict) or body.get("agent_runtime") not in OAUTH_RUNTIME_TYPES:
        raise ApiError(HTTPStatus.BAD_REQUEST, "agent_runtime must be one of " + ", ".join(OAUTH_RUNTIME_TYPES))
    runtime_type = body["agent_runtime"]
    orchestrator.reset_linked_account(runtime_type)
    try:
        _clear_local_agent_auth(runtime_type)
    except ApiError:
        orchestrator.refresh_runtime_status(runtime_type)
        raise
    orchestrator.refresh_runtime_status(runtime_type)
    return {"status": "accepted"}


def _clear_local_agent_auth(runtime_type: str) -> None:
    helper_runtime = "claude" if runtime_type == "claude_code" else "codex"
    try:
        proc = _run_root_helper(
            [*AGENT_AUTH_CLEAR_HELPER_COMMAND, helper_runtime], AGENT_AUTH_CLEAR_HELPER_TIMEOUT_SECONDS
        )
    except HelperTimedOut as exc:
        message = (
            f"{runtime_type} reset helper could not be terminated; retry reset"
            if exc.could_not_terminate
            else f"{runtime_type} reset timed out clearing local auth files; retry reset"
        )
        raise ApiError(HTTPStatus.CONFLICT, message) from exc
    if proc.returncode != 0:
        detail = _helper_error_message(proc.stdout, proc.stderr)
        message = f"{runtime_type} reset failed clearing local auth files; retry reset"
        if detail:
            message = f"{message}: {detail}"
        raise ApiError(HTTPStatus.CONFLICT, message)


AGENT_RUNTIME_TYPES = ("codex", "claude_code", "hermes")
OAUTH_RUNTIME_TYPES = ("codex", "claude_code")


def current_agent_accounts() -> dict[str, Any]:
    statuses = orchestrator.all_runtime_status_records()
    return {
        "accounts": [
            _current_agent_account(statuses, "codex"),
            _current_agent_account(statuses, "claude_code"),
            _current_bedrock_account(statuses),
        ]
    }


def refresh_agent_runtime_accounts(body: Any) -> dict[str, Any]:
    runtime_types: tuple[str, ...]
    if body is None:
        runtime_types = AGENT_RUNTIME_TYPES
    elif isinstance(body, dict):
        runtime = body.get("agent_runtime")
        if runtime is None:
            runtime_types = AGENT_RUNTIME_TYPES
        elif isinstance(runtime, str) and runtime in AGENT_RUNTIME_TYPES:
            runtime_types = (runtime,)
        else:
            raise ApiError(HTTPStatus.BAD_REQUEST, "agent_runtime must be one of " + ", ".join(AGENT_RUNTIME_TYPES))
    else:
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be an object")
    for runtime_type in runtime_types:
        force_probe = (
            runtime_type != "hermes"
            or orchestrator.runtime_network_enabled(runtime_type)
        )
        orchestrator.refresh_runtime_status(runtime_type, force_provider_probe=force_probe)
    return current_agent_accounts()


def _current_agent_account(statuses: dict[str, dict[str, Any]], runtime_type: str) -> dict[str, Any]:
    status = str(statuses.get(runtime_type, {}).get("status", "loading"))
    if runtime_type == "claude_code":
        response = {"agent_runtime": "claude_code", "provider": "claude", "status": status}
        account = read_claude_account()
        if not _claude_account_is_operator_approved(account):
            account = {}
    else:
        response = {"agent_runtime": "codex", "provider": "openai", "status": status}
        account = read_openai_account()
        if not _openai_account_is_operator_approved(account):
            account = {}
    if status == "active":
        response.update(_account_response_metadata(account, runtime_type))
        return response
    # The account anchor outlives sessions and deactivation; expose its
    # identity (never plan/usage) so the UI can show which account is linked
    # while the runtime is logged out or in error.
    for key in ("account_id", "email", "arn"):
        value = account.get(key)
        if isinstance(value, str) and value:
            response[key] = value
    return response


def _current_bedrock_account(statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    status = str(statuses.get("hermes", {}).get("status", "loading"))
    response: dict[str, Any] = {
        "provider": "bedrock",
        "agent_runtimes": ["hermes"],
        "status": status,
        # Live usage survives credential state on purpose: the counters record
        # month-to-date work already done, and reporting them costs one local
        # aggregate read.
        "bedrock_usage": _bedrock_live_usage(),
    }
    # Credential and display metadata are stored or cleared atomically, so the
    # account is meaningful only while the validated credential remains.
    account = state.read_bedrock_account() if state.read_bedrock_access_key_id() else {}
    if status == "active":
        response.update(_account_response_metadata(account, "bedrock"))
        return response
    for key in ("account_id", "arn"):
        value = account.get(key)
        if isinstance(value, str) and value:
            response[key] = value
    return response


_BEDROCK_USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def _bedrock_live_usage() -> dict[str, Any]:
    """Month-to-date Bedrock usage.

    The proxy counts the token usage AWS reports in each allowed response and
    the USD it priced that response at, per model and UTC day. This
    sums the current month straight from those stored counters — the cost is
    the recorded figure, not re-priced at read time. It remains an estimate of
    what AWS will bill, not the bill itself: unmetered requests (``requests``
    minus ``metered_requests``) are surfaced instead of silently rounding the
    estimate down."""
    month_start = time.strftime("%Y-%m-01", time.gmtime())
    usage: dict[str, Any] = {
        "month_to_date": 0.0,
        "currency": "USD",
        "requests": 0,
        "metered_requests": 0,
        **{field: 0 for field in _BEDROCK_USAGE_TOKEN_FIELDS},
    }
    for row in state.read_bedrock_usage(month_start):
        usage["requests"] += row["requests"]
        usage["metered_requests"] += row["metered_requests"]
        usage["month_to_date"] += row["cost_usd"]
        for field in _BEDROCK_USAGE_TOKEN_FIELDS:
            usage[field] += row[field]
    usage["month_to_date"] = round(usage["month_to_date"], 4)
    return usage


def _openai_account_is_operator_approved(account: dict[str, Any]) -> bool:
    return account.get("operator_approval") == orchestrator.OPENAI_OPERATOR_APPROVAL


def _claude_account_is_operator_approved(account: dict[str, Any]) -> bool:
    return account.get("identity_attestation") == orchestrator.CLAUDE_IDENTITY_ATTESTATION


# Bedrock is absent on purpose: its usage is computed live from the proxy's
# token counters (_bedrock_live_usage), never stored on the account row.
_RUNTIME_USAGE_KEYS = {
    "codex": "codex_usage",
    "claude_code": "claude_usage",
}


def _account_response_metadata(account: dict[str, Any], runtime_type: str) -> dict[str, Any]:
    # Provider capture sanitizes metadata before storage; this selects only the
    # public fields without re-normalizing provider-owned usage shapes.
    response: dict[str, Any] = {}
    for key in ("account_id", "email", "plan_type", "arn"):
        value = account.get(key)
        if isinstance(value, str) and value:
            response[key] = value
    usage_key = _RUNTIME_USAGE_KEYS.get(runtime_type)
    if usage_key is None:
        return response
    usage = account.get(usage_key)
    if isinstance(usage, dict) and usage:
        response[usage_key] = usage
    return response


def create_task(
    body: Any,
    *,
    app_backend_id: str | None = None,
) -> dict[str, Any]:
    input_message = _message(body, "input_message")
    thread_id = _thread_id(body)
    _validate_thread_id_not_reserved_by_app(thread_id, app_backend_id)
    with state.mutation() as cur:
        session_config = state.thread_session_config(cur, thread_id)
        agent_runtime, model, effort = _resolve_task_session_config(body, session_config)
        if state.queued_task_count(cur) >= QUEUED_TASK_LIMIT:
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"task queue is full ({QUEUED_TASK_LIMIT} queued tasks); cancel queued tasks or wait",
            )
        task_id = f"task_{state.allocate_task_number(cur)}"
        now = utc_now()
        provider_session_id = session_config.get("provider_session_id") if session_config else None
        state.save_thread_session(
            cur,
            agent_runtime,
            thread_id,
            provider_session_id,
            now,
            model,
            effort,
        )
        task = {
            "task_id": task_id,
            "status": QUEUED,
            "agent_runtime": agent_runtime,
            "model": model,
            "effort": effort,
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
    threads = state.thread_summaries()
    ordered = sorted(
        threads,
        key=lambda item: (str(item["last_used_at"]), item["agent_runtime"], item["thread_id"]),
        reverse=True,
    )
    return {"threads": ordered}


def list_thread_tasks(
    thread_id: str,
    *,
    limit: int = THREAD_TASK_LIMIT,
    message_bytes: int | None = None,
) -> dict[str, Any]:
    return {
        "tasks": [
            public_task(task, message_bytes=message_bytes)
            for task in state.tasks_for_thread(thread_id, limit)
        ]
    }


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
        if task["agent_runtime"] == "hermes":
            raise ApiError(
                HTTPStatus.CONFLICT,
                "Hermes tasks do not support steering; create a new task on the same thread_id",
            )
        if state.pending_steer_count(cur, task_id) >= PENDING_STEER_LIMIT:
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"task already has {PENDING_STEER_LIMIT} undelivered steer messages; wait for delivery",
            )
        state.append_task_steer(cur, task_id, steer_message)
        now = utc_now()
        task["updated_at"] = now
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
    policy = parsed.to_json()
    # The validated policy goes straight to the database row the proxy reads
    # (the proxy role cannot write it back); the write is atomic under the
    # mutation lock. Two concurrent replacements are last-writer-wins — a
    # single operator double-submitting — and the runtime reconcile below is
    # idempotent and re-run by the poller either way.
    record = state.network_policy_record()
    previous_policy = record["controls"] if record else {}
    updated_at = utc_now()
    state.save_network_policy(policy, updated_at)
    orchestrator.reconcile_runtime_status_after_policy_change()
    # Converge the installed GitHub credential to the published policy —
    # install on enable, remove on disable, with a fresh App mint on any
    # GitHub-integration change (an installation token only covers
    # repositories granted at mint time, so it must postdate the
    # enablement/repository list it serves). Enablement and credential health
    # stay separate concerns: a publish never fails on credential problems; a
    # failed mint or install records itself in the credential's validation
    # status, the working token is withdrawn (fail closed), and the poller
    # retries. The policy is already committed, so a transient convergence
    # failure (e.g. a policy read racing a concurrent replace) must not turn
    # this publish into an error — the poller retries convergence either way.
    # Repository audits
    # follow with the published token (forced on a GitHub change, TTL-gated
    # otherwise); they warn, never gate, so the publish result does not
    # depend on them either.
    try:
        github_changed = network_policy.managed_integration("github", previous_policy) != network_policy.managed_integration("github", policy)
        github_credential.reconcile(mint_fresh=github_changed)
        github_repo_audit.refresh(force=github_changed)
    except Exception:
        pass
    return {"network_controls": policy, "updated_at": updated_at}


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
    return {"mounts": mounts}


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


def public_task(
    task: dict[str, Any],
    queue_position: int | None = None,
    *,
    message_bytes: int | None = None,
) -> dict[str, Any]:
    value = {
        "task_id": task["task_id"],
        "status": task["status"],
        "agent_runtime": task["agent_runtime"],
        "model": task["model"],
        "effort": task["effort"],
        "thread_id": task["thread_id"],
        "input_message": _clip_encoded_text(task["input_message"], message_bytes),
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
    }
    if task.get("output_message") is not None:
        value["output_message"] = _clip_encoded_text(task["output_message"], message_bytes)
    if task.get("error_message") is not None:
        value["error_message"] = _clip_encoded_text(task["error_message"], message_bytes)
    if queue_position is not None:
        value["queue_position"] = queue_position
    return value


def _validate_thread_id_not_reserved_by_app(thread_id: str, app_backend_id: str | None) -> None:
    app_id, separator, visible_thread_id = thread_id.partition(APP_SCOPED_ID_SEPARATOR)
    if not separator:
        return
    if app_backend_id == app_id and visible_thread_id:
        return
    raise ApiError(
        HTTPStatus.BAD_REQUEST,
        f"thread_id prefix {app_id}{APP_SCOPED_ID_SEPARATOR} is reserved for app backend tasks",
    )

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
        raise ApiError(HTTPStatus.BAD_REQUEST, "agent_runtime must be one of " + ", ".join(sorted(AGENT_RUNTIMES)))
    return _agent_runtime_value(value)


def _agent_runtime_value(value: str) -> str:
    if value not in AGENT_RUNTIMES:
        raise ApiError(HTTPStatus.BAD_REQUEST, "agent_runtime must be one of " + ", ".join(sorted(AGENT_RUNTIMES)))
    return value


def _session_config(body: Any, runtime: str) -> tuple[str, str]:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
    model = body.get("model")
    effort = body.get("effort")
    error = session_config_error(runtime, model, effort)
    if error is not None:
        raise ApiError(HTTPStatus.BAD_REQUEST, error)
    assert isinstance(model, str) and isinstance(effort, str)
    return model, effort


def _resolve_task_session_config(
    body: Any,
    session_config: dict[str, Any] | None,
) -> tuple[str, str, str]:
    stored = None
    if session_config is not None:
        stored = (
            session_config["agent_runtime"],
            session_config["model"],
            session_config["effort"],
        )

    assert isinstance(body, dict)
    fields = ("agent_runtime", "model", "effort")
    supplied = [field for field in fields if field in body]
    if stored is not None:
        if not supplied:
            return stored
        if len(supplied) != len(fields):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "agent_runtime, model, and effort must be provided together",
            )
        requested_runtime = _agent_runtime(body)
        requested_model, requested_effort = _session_config(body, requested_runtime)
        requested = (requested_runtime, requested_model, requested_effort)
        if requested != stored:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "agent_runtime, model, and effort must match the existing thread configuration",
            )
        return requested
    if not supplied:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "agent_runtime, model, and effort are required when starting a new thread",
        )
    if len(supplied) != len(fields):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "agent_runtime, model, and effort must be provided together",
        )

    agent_runtime = _agent_runtime(body)
    model, effort = _session_config(body, agent_runtime)
    return agent_runtime, model, effort


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


def _agent_file_upload_filename(query: dict[str, list[str]]) -> str:
    _reject_query_keys(query, {"filename"}, "agent file upload")
    value = _one(query, "filename")
    if value is None or value in {"", ".", ".."}:
        raise ApiError(HTTPStatus.BAD_REQUEST, "filename must be non-empty")
    if any(character in value for character in ("/", "\\", "\0")):
        raise ApiError(HTTPStatus.BAD_REQUEST, "filename must not contain path separators or a NUL byte")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ApiError(HTTPStatus.BAD_REQUEST, "filename must not contain control characters")
    if len(value.encode("utf-8")) > AGENT_FILE_UPLOAD_FILENAME_MAX_BYTES:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"filename must be at most {AGENT_FILE_UPLOAD_FILENAME_MAX_BYTES} UTF-8 bytes",
        )
    return value


def _optional_non_negative_int(query: dict[str, list[str]], key: str) -> int | None:
    value = _one(query, key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be an integer") from exc
    if parsed < 0:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be non-negative")
    return parsed


def _bounded_positive_query_int(
    query: dict[str, list[str]],
    key: str,
    default: int,
    maximum: int,
) -> int:
    value = _one(query, key)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be an integer") from exc
    if parsed < 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be positive")
    if parsed > maximum:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be at most {maximum}")
    return parsed


def _optional_bounded_positive_query_int(
    query: dict[str, list[str]],
    key: str,
    maximum: int,
) -> int | None:
    if _one(query, key) is None:
        return None
    return _bounded_positive_query_int(query, key, maximum, maximum)


def _clip_encoded_text(value: str, maximum: int | None) -> str:
    if maximum is None:
        return value
    encoded = value.encode()
    if len(encoded) <= maximum:
        return value
    suffix = "…".encode()
    if maximum < len(suffix):
        return encoded[:maximum].decode(errors="ignore")
    return encoded[: maximum - len(suffix)].decode(errors="ignore") + "…"


def _network_event_decision(query: dict[str, list[str]]) -> str | None:
    value = _one(query, "decision")
    if value is None or value == "all":
        return None
    if value not in {"allowed", "denied"}:
        raise ApiError(HTTPStatus.BAD_REQUEST, "decision must be allowed, denied, or all")
    return value


def _event_page_limit(query: dict[str, list[str]]) -> int:
    value = _one(query, "limit")
    if value is None:
        return state.EVENT_PAGE_LIMIT
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "limit must be an integer") from exc
    if parsed < 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, "limit must be positive")
    if parsed > state.EVENT_PAGE_LIMIT:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"limit must be at most {state.EVENT_PAGE_LIMIT}")
    return parsed


def _reject_query_keys(query: dict[str, list[str]], allowed: set[str], label: str) -> None:
    unexpected = sorted(set(query) - allowed)
    if unexpected:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"unsupported {label} query parameter: {unexpected[0]}")


def _minutes_from_now(minutes: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + minutes * 60))


def initialize_state() -> None:
    """Recover after a restart or reboot: a task that was mid-turn has no
    worker attached anymore, so fail it rather than leave it running forever.
    (A pending push interrupted mid-resolve is still pending and the operator
    approves or rejects it again.) The tools service applies the same policy to its own
    interrupted state at its startup: an approval caught mid-execution is
    marked failed, never re-executed (tools_host.recover_interrupted_approvals)."""
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
    # rather than fail the live instance's running task first.
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    app_backend_httpd = app_backend_admin_api.create_app_backend_admin_server()
    initialize_state()
    # The agent-facing tools socket and tool execution run in the dedicated
    # trustyclaw-tools service (its own user, egress, and scoped DB role); the
    # admin service only forwards operator operations to it.
    orchestrator.start_workers()
    threading.Thread(target=maintenance_loop, daemon=True).start()
    threading.Thread(target=upgrade_check.poll, daemon=True).start()
    threading.Thread(target=app_backend_httpd.serve_forever, daemon=True).start()
    try:
        httpd.serve_forever()
    finally:
        app_backend_httpd.server_close()
        app_backend_admin_api.unlink_app_backend_admin_socket()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
