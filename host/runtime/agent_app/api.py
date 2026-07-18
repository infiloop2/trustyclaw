"""Agent-facing app API service: HTTP over a Unix domain socket.

Agents working an app-created task call their app's backend through this
service (the dedicated ``trustyclaw-agent-app`` process; see
``agent_app_service``). The MCP shim forwards the ``app_api`` tool here; this
module authenticates the caller, reads its host thread from a kernel-owned
scope, derives that thread's owning app, and reverse-proxies the call to the app
backend's loopback port — the one uid besides the admin service that nftables
allows to open new connections to app ports.

Attribution is kernel-verified, not claimed: the orchestrator spawns every
task turn inside a systemd scope named after its host thread id
(``trustyclaw-agent-thread-<thread_id>.scope`` via the run-claude-code /
run-codex-app-server helpers), so the caller's thread is read from
``/proc/<peer pid>/cgroup``. A process cannot rewrite its own cgroup (the
cgroupfs is root-owned and delegation is off), so — unlike an environment
token, which any same-uid process can read out of ``/proc/<pid>/environ`` — a
concurrently running agent for another app cannot present this thread's
identity. The scope pid is pinned with a pidfd across the /proc reads, so a
pid recycled mid-check fails closed. App-created host threads use the reserved
``<app_id>__<thread_id>`` namespace; the service splits that trusted prefix and
requires the installed app manifest to enable ``agent.api``. No database join,
registration row, or task-lifecycle cleanup is involved.

The agent-facing HTTP surface is one JSON route: ``POST /call`` with
``{"method", "path", "body"?}`` proxies one request to the
owning app backend's agent route namespace (``/agent/...``) and returns
``{"status": <http status>, "body": <decoded JSON>}`` verbatim, so the
agent sees the app's own validation errors and can retry in-turn.

"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import select
import socket
import struct
import threading
from typing import Any, Callable

from host.constants import AGENT_APP_SOCKET_PATH, LOOPBACK
from host.runtime.core import app_platform

DEFAULT_SOCKET_PATH = AGENT_APP_SOCKET_PATH
SOCKET_PATH = os.environ.get("TRUSTYCLAW_AGENT_APP_SOCKET", DEFAULT_SOCKET_PATH)
AGENT_PEER_USER = "trustyclaw-agent"
MAX_REQUEST_BODY_BYTES = 256 * 1024
# App backends parse the response themselves; cap what one call can pull back
# through the socket so a misbehaving app cannot balloon agent turns.
MAX_RESPONSE_BODY_BYTES = 1024 * 1024
# Agent app calls block a handler thread on the app backend; richer than the
# 10s browser-bridge budget (agent-triggered work may hit the app's own DB),
# but still bounded so a stuck app cannot pin threads forever.
APP_CALL_TIMEOUT_SECONDS = 30
# One process-wide host cap keeps total app pressure bounded. A saturated app
# can cause another caller to receive 429 and retry; the deliberately simple
# policy has no per-app fairness scheduler.
MAX_CONCURRENT_CALLS = 8
_CALL_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)
REQUEST_READ_TIMEOUT_SECONDS = 30
# The agent-callable namespace on an app backend. Everything else — operator
# UI routes above all — is unreachable through this proxy by construction.
AGENT_ROUTE_PREFIX = "/agent/"
AGENT_PATH_RE = re.compile(r"^/agent/[A-Za-z0-9._~/-]{0,512}(?:\?[A-Za-z0-9._~=&%+-]{0,512})?$")
ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
# Trusted markers the app backend receives instead of anything agent-claimed;
# they arrive only over the app's loopback port, which nftables restricts to
# the admin and agent-app service uids.
AGENT_THREAD_HEADER = "X-TrustyClaw-Agent-Thread"
AGENT_PROXY_HEADER = "X-TrustyClaw-Agent-App-Proxy"
# The systemd scope the runtime helpers create per host thread; matching on the
# full trustyclaw_agent.slice path rejects scopes an agent could mint through
# a user manager (those live under user.slice).
_THREAD_SCOPE_RE = re.compile(
    r"^\d+:[^:]*:/trustyclaw_agent\.slice/"
    r"trustyclaw-agent-thread-([A-Za-z0-9_-]{1,64})\.scope(?:/|$)",
    re.MULTILINE,
)


class AttributionError(Exception):
    """The caller could not be attributed to an app-scoped host thread."""


def thread_id_for_pid(pid: int) -> str:
    """The host thread id of the scope ``pid`` runs in, kernel-verified.

    The pid is pinned with a pidfd before /proc is read and its liveness is
    re-checked after, so a pid that exited and was recycled between the peer
    credential read and the cgroup read cannot be attributed: if the original
    process died at any point across the check, the pidfd readiness probe fails
    and the call is rejected rather than trusted.
    """
    try:
        pidfd = os.pidfd_open(pid, 0)
    except OSError as exc:
        raise AttributionError(f"peer process is gone: {exc}") from exc
    try:
        if select.select([pidfd], [], [], 0)[0]:
            raise AttributionError("peer process is gone")
        try:
            cgroup = Path(f"/proc/{pid}/cgroup").read_text()
        except OSError as exc:
            raise AttributionError(f"peer cgroup unreadable: {exc}") from exc
        match = _THREAD_SCOPE_RE.search(cgroup)
        if select.select([pidfd], [], [], 0)[0]:
            raise AttributionError("peer process died during attribution")
        if match is None:
            raise AttributionError("peer is not inside a host thread scope")
        return match.group(1)
    finally:
        os.close(pidfd)


def resolve_context(host_thread_id: str) -> dict[str, Any]:
    """Resolve a kernel-attributed app-prefixed host thread."""
    app_id, separator, app_thread_id = host_thread_id.partition(app_platform.APP_SCOPED_ID_SEPARATOR)
    if not separator or not app_id or not app_thread_id:
        raise AttributionError("host thread is not app-scoped")
    app = app_platform.app_by_id(app_id)
    if app is None or not app.agent_api:
        raise AttributionError("thread's app does not offer an agent API")
    return {
        "app_id": app.id,
        "thread_id": app_thread_id,
        "port": app.port,
    }


def proxy_call(context: dict[str, Any], method: Any, path: Any, body: Any) -> dict[str, Any]:
    """Proxy one validated agent call to the owning app backend and return
    ``{"status", "body"}``. Raises ValueError for caller mistakes returned to
    the agent as its own error, never sent to the app."""
    if not isinstance(method, str) or method.upper() not in ALLOWED_METHODS:
        raise ValueError(f"method must be one of {', '.join(sorted(ALLOWED_METHODS))}")
    if not isinstance(path, str) or not AGENT_PATH_RE.fullmatch(path) or "/../" in path or path.endswith("/.."):
        raise ValueError(f"path must match {AGENT_PATH_RE.pattern}")
    encoded = None if body is None else json.dumps(body, sort_keys=True).encode()
    if encoded is not None and len(encoded) > MAX_REQUEST_BODY_BYTES:
        raise ValueError(f"body exceeds {MAX_REQUEST_BODY_BYTES} bytes")
    headers = {
        AGENT_PROXY_HEADER: context["app_id"],
        AGENT_THREAD_HEADER: context["thread_id"],
    }
    if encoded is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(encoded))
    conn: http.client.HTTPConnection | None = None
    try:
        conn = http.client.HTTPConnection(LOOPBACK, context["port"], timeout=APP_CALL_TIMEOUT_SECONDS)
        conn.request(method.upper(), path, body=encoded, headers=headers)
        response = conn.getresponse()
        raw = response.read(MAX_RESPONSE_BODY_BYTES + 1)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if len(raw) > MAX_RESPONSE_BODY_BYTES:
        raise RuntimeError("app backend response too large")
    try:
        decoded = json.loads(raw.decode() or "null")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("app backend returned invalid JSON") from exc
    return {"status": int(response.status), "body": decoded}


def _peer_uids(user: str) -> frozenset[int]:
    # Outside a bootstrapped host (tests, the UI mock) the service accounts do
    # not exist; the socket then belongs to the developer running it.
    try:
        return frozenset({pwd.getpwnam(user).pw_uid})
    except KeyError:
        return frozenset({os.getuid()})


def agent_peer_uids() -> frozenset[int]:
    """The uids allowed to call this service: the agent only. Falls back to
    the current uid off a bootstrapped host."""
    return _peer_uids(AGENT_PEER_USER)


class AgentAppRequestHandler(BaseHTTPRequestHandler):
    server: "AgentAppServer"
    timeout = REQUEST_READ_TIMEOUT_SECONDS

    def address_string(self) -> str:  # AF_UNIX has no client address tuple
        return "local"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _peer(self) -> tuple[int, int]:
        creds = self.connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        pid, uid, _ = struct.unpack("3i", creds)
        return pid, uid

    def _send_json(self, status: HTTPStatus | int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _attributed_context(self) -> dict[str, Any] | None:
        """Authenticate the peer uid and resolve its app-scoped host thread,
        or send the error response and return None."""
        pid, uid = self._peer()
        if uid not in self.server.agent_uids:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Peer not allowed."})
            return None
        try:
            thread_id = self.server.thread_resolver(pid)
            return resolve_context(thread_id)
        except AttributionError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"No agent app context: {exc}"})
            return None

    def do_GET(self) -> None:
        # Authenticate before revealing anything, even the route list.
        _pid, uid = self._peer()
        status = HTTPStatus.FORBIDDEN if uid not in self.server.agent_uids else HTTPStatus.NOT_FOUND
        error = "Peer not allowed." if status == HTTPStatus.FORBIDDEN else "Unknown path."
        self._send_json(status, {"error": error})

    def do_POST(self) -> None:
        if self.path != "/call":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown path."})
            return
        context = self._attributed_context()
        if context is None:
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request too large."})
            return
        if not _CALL_SLOTS.acquire(blocking=False):
            self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many concurrent app calls."})
            return
        try:
            try:
                request = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be JSON."})
                return
            if not isinstance(request, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be a JSON object."})
                return
            method, path = request.get("method"), request.get("path")
            try:
                result = proxy_call(context, method, path, request.get("body"))
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except Exception:
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": f"App backend unavailable: {context['app_id']}"},
                )
                return
            self._send_json(HTTPStatus.OK, result)
        finally:
            _CALL_SLOTS.release()


class AgentAppServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True

    def __init__(
        self,
        socket_path: str,
        agent_uids: frozenset[int],
        thread_resolver: Callable[[int], str] | None = None,
    ) -> None:
        self.agent_uids = agent_uids
        # Injectable for tests, which cannot run inside real systemd thread
        # scopes; production always uses the kernel cgroup attribution.
        self.thread_resolver = thread_resolver if thread_resolver is not None else thread_id_for_pid
        # typeshed models HTTPServer addresses as (host, port) tuples only;
        # with address_family = AF_UNIX the address is the socket path.
        super().__init__(socket_path, AgentAppRequestHandler)  # type: ignore[arg-type]

    def server_bind(self) -> None:
        path = Path(str(self.server_address))
        path.unlink(missing_ok=True)
        self.socket.bind(str(path))
        # World-connectable; the peer-credential check above is the
        # authentication.
        path.chmod(0o666)


def serve_forever(socket_path: str = SOCKET_PATH) -> None:
    """Bind the agent-app socket and serve it in the foreground (the dedicated
    trustyclaw-agent-app service entry point)."""
    AgentAppServer(socket_path, agent_peer_uids()).serve_forever()
