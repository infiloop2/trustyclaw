"""Agent-facing tools service: HTTP over a Unix domain socket.

Agent runtimes call bundled tools through an MCP shim
(``host.runtime.tools_mcp_shim``) that forwards to this service (the
dedicated ``trustyclaw-tools`` process; see ``tools_service``). Unix peer
credentials give a kernel-verified caller identity, and every route is
scoped to exactly one peer: the agent uid gets the MCP surface
(``GET /tools``, ``POST /call``) and the admin uid gets the operator
delegation routes (``POST /operator/...``) — neither can call the other's
routes, no admin password involved and none required.

The agent-facing HTTP surface is two routes, both JSON:

- ``GET /tools`` — the callable tool actions for the enabled tools, named
  ``<tool_id>_<action>``, plus the built-in ``check_tool_approval``.
- ``POST /call`` — ``{"name": ..., "input": {...}}`` executes one action and
  returns the result shape from ``tools_host`` (``executed`` /
  ``pending_approval`` / ``failed``).
Approval status checks are a built-in action invoked through ``POST /call``,
so the agent can resume after the operator decides in the admin UI.

Approval-gated actions never execute during the initial agent call: they create
a pending approval and the operator decides in the admin UI (see ``admin_api``).
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pwd
import re
import socket
import struct
import threading
from typing import Any

from host.runtime import state, tools_host

DEFAULT_SOCKET_PATH = "/run/trustyclaw-tools/tools.sock"
SOCKET_PATH = os.environ.get("TRUSTYCLAW_TOOLS_SOCKET", DEFAULT_SOCKET_PATH)
# Peers are scoped strictly by path: the agent gets exactly the MCP surface
# (GET /tools, POST /call), and the admin service gets exactly the operator
# delegation routes (POST /operator/...) that need this service's egress
# (OAuth code exchange, token revoke) or run tool code that touches
# third-party data. Neither peer can call the other's routes.
AGENT_PEER_USER = "trustyclaw-agent"
ADMIN_PEER_USER = "trustyclaw-admin"
MAX_REQUEST_BODY_BYTES = 256 * 1024
# Tool calls block a handler thread on third-party requests (30s timeouts in
# the packages), so cap concurrency instead of letting a runaway agent stack
# threads.
MAX_CONCURRENT_CALLS = 8
_CALL_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)
# The world-connectable socket parses the request line and headers before the
# handler's peer-credential check runs, so a local peer that connects and stalls
# mid-request would otherwise pin a handler thread indefinitely. A read timeout
# closes such connections; a client sends its whole request up front, so this
# never fires in normal use, and it does not bound in-flight tool execution
# (that time is spent on third-party calls, not socket reads). A local peer
# that deliberately dribbles bytes can hold one daemon thread per connection;
# every peer on the box is a known service uid, so that costs nothing worth
# defending against.
REQUEST_READ_TIMEOUT_SECONDS = 30

CHECK_APPROVAL_TOOL = {
    "name": "check_tool_approval",
    "description": (
        "Check the status of a tool action approval. Approval-gated actions "
        "return an approval_id and wait for the operator to decide in the "
        "TrustyClaw admin UI; poll this with that id to learn the outcome "
        "(pending, approved, denied, expired, executed, or failed)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "approval_id": {"type": "string"},
        },
        "required": ["approval_id"],
        "additionalProperties": False,
    },
}

# Always listed so the agent can distinguish "bundled but not enabled" (ask the
# operator to enable it) from "no bundled tool at all" (build the capability
# itself). It reads only manifests plus the enablement set — no credentials,
# no third-party calls.
LIST_BUNDLED_TOOLS_TOOL = {
    "name": "list_bundled_tools",
    "description": (
        "List every tool bundled with this TrustyClaw host and whether it is "
        "currently enabled. A tool listed here but not enabled exists on the "
        "host but its actions stay hidden until the operator enables it (and, "
        "for OAuth tools, connects it) in the admin UI's Tools tab — ask the "
        "operator instead of building a replacement. A capability with no "
        "entry here has no bundled tool at all."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}


def action_listing() -> list[dict[str, Any]]:
    """The agent-callable actions for the currently enabled tools."""
    listing: list[dict[str, Any]] = []
    enabled = state.enabled_tool_ids()
    for tool_id, tool in tools_host.BUNDLED_TOOLS.items():
        if tool_id not in enabled:
            continue
        manifest = tool.manifest
        for spec in manifest.actions:
            listing.append(
                {
                    "name": f"{tool_id}_{spec.id}",
                    "description": f"{manifest.display_name}: {spec.description}",
                    "input_schema": spec.input_schema,
                }
            )
    listing.append(LIST_BUNDLED_TOOLS_TOOL)
    listing.append(CHECK_APPROVAL_TOOL)
    return listing


def _resolve_action(name: str) -> tuple[str, str] | None:
    """Map a listed action name back to (tool_id, action)."""
    for tool_id, tool in tools_host.BUNDLED_TOOLS.items():
        prefix = f"{tool_id}_"
        if name.startswith(prefix) and tool.manifest.action(name[len(prefix):]) is not None:
            return tool_id, name[len(prefix):]
    return None


def call_action(name: Any, tool_input: Any) -> dict[str, Any]:
    if not isinstance(name, str):
        raise tools_host.ToolCallError("Tool name must be a string.")
    if name == "check_tool_approval":
        return _check_approval(tool_input)
    if name == "list_bundled_tools":
        return _list_bundled_tools()
    resolved = _resolve_action(name)
    if resolved is None:
        raise tools_host.ToolCallError(f"Unknown tool: {name}.")
    return tools_host.execute_action(resolved[0], resolved[1], tool_input)


def _list_bundled_tools() -> dict[str, Any]:
    """The full bundled catalog with enablement, so the agent can tell the
    operator which existing tool to enable instead of rebuilding it."""
    enabled = state.enabled_tool_ids()
    tools = [
        {
            "tool_id": tool_id,
            "display_name": tool.manifest.display_name,
            "description": tool.manifest.description,
            "connection": tool.manifest.connection,
            "enabled": tool_id in enabled,
            "action_ids": [spec.id for spec in tool.manifest.actions],
        }
        for tool_id, tool in tools_host.BUNDLED_TOOLS.items()
    ]
    return {"status": "executed", "result": {"tools": tools}}


def _check_approval(tool_input: Any) -> dict[str, Any]:
    approval_id = tool_input.get("approval_id") if isinstance(tool_input, dict) else None
    if not isinstance(approval_id, str) or not approval_id:
        raise tools_host.ToolCallError("check_tool_approval requires approval_id.")
    # The id carries the token; state.tool_approval verifies it constant-time
    # and returns None on any mismatch, so a guessed number never resolves.
    record = state.tool_approval(approval_id)
    if record is None:
        raise tools_host.ToolCallError(f"Unknown approval: {approval_id}.")
    result: dict[str, Any] = {
        "status": "executed",
        "result": {
            "approval_id": record["approval_id"],
            "approval_status": record["status"],
            "summary": record["summary"],
        },
    }
    if record["status"] in {"executed", "failed"}:
        result["result"]["execution_result"] = record["result"]
    return result


class OperatorError(Exception):
    """An operator delegation failure carrying the HTTP status to return."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# Operator operations the admin service reverse-proxies to this service: the
# whole OAuth connect flow (so no OAuth tool code runs in the admin service) and
# approval decisions (which run the approved payload over this service's egress).
OPERATOR_START_RE = re.compile(r"^/operator/tools/([a-z0-9_]{1,64})/oauth_connect/start$")
OPERATOR_COMPLETE_RE = re.compile(r"^/operator/tools/([a-z0-9_]{1,64})/oauth_connect/complete$")
OPERATOR_DISCONNECT_RE = re.compile(r"^/operator/tools/([a-z0-9_]{1,64})/oauth_connect/disconnect$")
OPERATOR_DECIDE_RE = re.compile(
    r"^/operator/tools/([a-z0-9_]{1,64})/approvals/([A-Za-z0-9._:-]{1,128})/(approve|deny)$"
)


def _operator_connect_flow(tool_id: str, *, require_enabled: bool = True) -> Any:
    """The tool's OAuth connect flow (tool.credentials), or an OperatorError.
    tool.credentials is the single source of truth for whether a tool has a
    connect flow; a non-None flow narrows the type for the connect calls."""
    tool = tools_host.BUNDLED_TOOLS.get(tool_id)
    if tool is None:
        raise OperatorError(HTTPStatus.NOT_FOUND, f"unknown tool: {tool_id}")
    flow = tool.credentials
    if flow is None:
        raise OperatorError(HTTPStatus.CONFLICT, f"{tool_id} has no OAuth connect flow")
    if require_enabled and tool_id not in state.enabled_tool_ids():
        raise OperatorError(HTTPStatus.CONFLICT, f"{tool_id} is not enabled")
    return flow


def _operator_start_connect(tool_id: str, body: Any) -> dict[str, Any]:
    flow = _operator_connect_flow(tool_id)
    if not isinstance(body, dict) or not isinstance(body.get("redirect_uri"), str) or not body["redirect_uri"]:
        raise OperatorError(HTTPStatus.BAD_REQUEST, "redirect_uri is required")
    api = tools_host.host_api_for(tools_host.bundled_tool(tool_id))
    try:
        return flow.start_connect({"redirect_uri": body["redirect_uri"]}, api)
    except (ValueError, KeyError) as exc:
        raise OperatorError(HTTPStatus.BAD_REQUEST, str(exc) or "invalid connect request") from exc
    except Exception as exc:  # noqa: BLE001 - tool packages redact their messages
        raise OperatorError(HTTPStatus.BAD_GATEWAY, str(exc) or "tool connect flow failed") from exc


def _operator_complete_connect(tool_id: str, body: Any) -> dict[str, Any]:
    flow = _operator_connect_flow(tool_id)
    if not isinstance(body, dict):
        raise OperatorError(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
    params = {key: body.get(key) for key in ("code", "redirect_uri", "state")}
    if not all(isinstance(value, str) and value for value in params.values()):
        raise OperatorError(HTTPStatus.BAD_REQUEST, "code, redirect_uri, and state are required")
    api = tools_host.host_api_for(tools_host.bundled_tool(tool_id))
    try:
        result = flow.complete_connect(params, api)
    except (ValueError, KeyError) as exc:
        raise OperatorError(HTTPStatus.BAD_REQUEST, str(exc) or "invalid connect request") from exc
    except Exception as exc:  # noqa: BLE001 - tool packages redact their messages
        raise OperatorError(HTTPStatus.BAD_GATEWAY, str(exc) or "tool connect flow failed") from exc
    account = result.get("account") if isinstance(result, dict) else None
    label = account.get("label") if isinstance(account, dict) else None
    state.record_tool_event(tool_id, "oauth_connect", "connected", label if isinstance(label, str) else "")
    return result


def _operator_disconnect(tool_id: str) -> dict[str, Any]:
    # Disconnect skips the enabled gate so stored tokens can always be revoked.
    flow = _operator_connect_flow(tool_id, require_enabled=False)
    api = tools_host.host_api_for(tools_host.bundled_tool(tool_id))
    try:
        flow.disconnect(api)
    except Exception as exc:  # noqa: BLE001 - tool packages redact their messages
        raise OperatorError(HTTPStatus.BAD_GATEWAY, str(exc) or "tool disconnect failed") from exc
    state.record_tool_event(tool_id, "oauth_connect", "disconnected", "")
    return {"tool_id": tool_id, "connected": False}


def _operator_decide(tool_id: str, approval_id: str, decision: str) -> dict[str, Any]:
    # The approval is addressed under its tool, so reject a decision whose tool
    # does not own the approval before spending it.
    if state.tool_approval(approval_id, tool_id=tool_id) is None:
        raise OperatorError(HTTPStatus.NOT_FOUND, "unknown approval")
    try:
        return tools_host.decide_approval(approval_id, decision)
    except tools_host.ToolCallError as exc:
        raise OperatorError(HTTPStatus.CONFLICT, str(exc)) from exc


def handle_operator(path: str, body: Any) -> dict[str, Any]:
    """Dispatch one admin-delegated operator operation. Raises OperatorError
    with the HTTP status the admin service should return."""
    start = OPERATOR_START_RE.fullmatch(path)
    if start:
        return _operator_start_connect(start.group(1), body)
    complete = OPERATOR_COMPLETE_RE.fullmatch(path)
    if complete:
        return _operator_complete_connect(complete.group(1), body)
    disconnect = OPERATOR_DISCONNECT_RE.fullmatch(path)
    if disconnect:
        return _operator_disconnect(disconnect.group(1))
    decide = OPERATOR_DECIDE_RE.fullmatch(path)
    if decide:
        return _operator_decide(decide.group(1), decide.group(2), decide.group(3))
    raise OperatorError(HTTPStatus.NOT_FOUND, "unknown path")


def _peer_uids(user: str) -> frozenset[int]:
    # Outside a bootstrapped host (tests, the UI mock) the service accounts do
    # not exist; the socket then belongs to the developer running it.
    try:
        return frozenset({pwd.getpwnam(user).pw_uid})
    except KeyError:
        return frozenset({os.getuid()})


def agent_peer_uids() -> frozenset[int]:
    """The uids allowed to call the agent MCP routes (GET /tools, POST /call):
    the agent only. Falls back to the current uid off a bootstrapped host."""
    return _peer_uids(AGENT_PEER_USER)


def admin_peer_uids() -> frozenset[int]:
    """The uids allowed to call the operator delegation routes: the admin
    service only. Falls back to the current uid off a bootstrapped host."""
    return _peer_uids(ADMIN_PEER_USER)


class ToolsRequestHandler(BaseHTTPRequestHandler):
    server: "ToolsServer"
    # Bound how long a connection may stall while sending its request line and
    # headers, before do_GET/do_POST (and the peer-credential check) run.
    timeout = REQUEST_READ_TIMEOUT_SECONDS

    def address_string(self) -> str:  # AF_UNIX has no client address tuple
        return "local"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _peer_uid(self) -> int:
        creds = self.connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        _, uid, _ = struct.unpack("3i", creds)
        return uid

    def _peer_is_agent(self) -> bool:
        return self._peer_uid() in self.server.agent_uids

    def _peer_is_admin(self) -> bool:
        return self._peer_uid() in self.server.admin_uids

    def _send_json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        # Peers are scoped strictly by path; every GET route belongs to the
        # agent MCP surface, so only the agent peer reaches it.
        if not self._peer_is_agent():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Peer not allowed."})
            return
        if self.path == "/tools":
            self._send_json(HTTPStatus.OK, {"tools": action_listing()})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown path."})

    def _read_json_object_body(self, length: int) -> dict[str, Any] | None:
        """Read and parse the request body as a JSON object, or send the error
        response and return None. Operator disconnect/decide carry no body, so an
        empty body is tolerated as an empty object."""
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be JSON."})
            return None
        if not isinstance(body, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be a JSON object."})
            return None
        return body

    def do_POST(self) -> None:
        # Peers are scoped strictly by path: operator delegation routes belong
        # to the admin service, everything else (the agent MCP surface) to the
        # agent; neither peer can call the other's routes.
        is_operator = self.path.startswith("/operator/")
        if is_operator and not self._peer_is_admin():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Operator routes require the admin peer."})
            return
        if not is_operator and not self._peer_is_agent():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Peer not allowed."})
            return
        if not is_operator and self.path != "/call":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown path."})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Malformed Content-Length."})
            return
        if length < 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Malformed Content-Length."})
            return
        if length > MAX_REQUEST_BODY_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request too large."})
            return
        if is_operator:
            # Operator routes are operator-initiated and low volume, so they do not
            # share the agent-call concurrency cap; a busy agent must not be able to
            # 429 the operator's approve/deny/connect/disconnect.
            body = self._read_json_object_body(length)
            if body is None:
                return
            try:
                result = handle_operator(self.path, body)
            except OperatorError as exc:
                self._send_json(exc.status, {"error": exc.message})
                return
            self._send_json(HTTPStatus.OK, result)
            return
        # Agent tool calls each block a handler thread on a third-party request, so
        # they are capacity-capped, checked before the agent-controlled body is read.
        if not _CALL_SLOTS.acquire(blocking=False):
            self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many concurrent tool calls."})
            return
        try:
            body = self._read_json_object_body(length)
            if body is None:
                return
            result = call_action(body.get("name"), body.get("input"))
        except tools_host.ToolCallError as exc:
            result = {"status": "failed", "error": str(exc), "reconnect_required": False}
        except Exception:
            # Tool packages map their own errors; anything else must not leak
            # internals to the agent.
            result = {"status": "failed", "error": "Tool call failed.", "reconnect_required": False}
        finally:
            _CALL_SLOTS.release()
        self._send_json(HTTPStatus.OK, result)


class ToolsServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True

    def __init__(
        self, socket_path: str, agent_uids: frozenset[int], admin_uids: frozenset[int] | None = None
    ) -> None:
        # Strictly path-scoped peers: agent_uids reach the agent MCP routes,
        # admin_uids reach the operator delegation routes, nothing overlaps by
        # construction (off a bootstrapped host both fall back to the
        # developer's uid).
        self.agent_uids = agent_uids
        self.admin_uids = admin_uids if admin_uids is not None else admin_peer_uids()
        # typeshed models HTTPServer addresses as (host, port) tuples only;
        # with address_family = AF_UNIX the address is the socket path.
        super().__init__(socket_path, ToolsRequestHandler)  # type: ignore[arg-type]

    def server_bind(self) -> None:
        path = Path(str(self.server_address))
        path.unlink(missing_ok=True)
        self.socket.bind(str(path))
        # World-connectable like the Postgres socket; the peer-credential
        # check above is the authentication.
        path.chmod(0o666)


def serve_forever(socket_path: str = SOCKET_PATH) -> None:
    """Bind the tools socket and serve it in the foreground (the dedicated
    trustyclaw-tools service entry point)."""
    ToolsServer(socket_path, agent_peer_uids()).serve_forever()
