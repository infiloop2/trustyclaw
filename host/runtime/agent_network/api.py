"""Agent network introspection over a peer-authenticated Unix socket.

This service exposes only the two read-only network tools. It runs as the
non-egress ``trustyclaw-agent-network`` user and reads only the policy and
network-event tables granted to that database role. The MCP shim aggregates
this socket with the independent tools and app sockets.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pwd
import socket
import struct
import threading
from typing import Any

from host.constants import AGENT_NETWORK_SOCKET_PATH
from host.network_integrations import registry
from host.runtime.core import network_policy, state

DEFAULT_SOCKET_PATH = AGENT_NETWORK_SOCKET_PATH
SOCKET_PATH = os.environ.get("TRUSTYCLAW_AGENT_NETWORK_SOCKET", DEFAULT_SOCKET_PATH)
AGENT_PEER_USER = "trustyclaw-agent"
MAX_REQUEST_BODY_BYTES = 16 * 1024
MAX_CONCURRENT_CALLS = 8
REQUEST_READ_TIMEOUT_SECONDS = 30
_CALL_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)

LIST_NETWORK_INTEGRATIONS_TOOL = {
    "name": "list_network_integrations",
    "description": (
        "List every network integration on this host with whether it is enabled and its "
        "policy options. All agent network traffic passes through exactly one integration, "
        "including operator-configured custom domains. If a destination is not covered, ask "
        "the operator to enable or configure its integration in the admin UI's Network tab."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}
RECENT_NETWORK_DENIALS_TOOL = {
    "name": "recent_network_denials",
    "description": (
        "List this host's most recent denied network requests with each denial's code and "
        "guidance on what would change the outcome. Use this after an HTTP request, git push, "
        "or package install failed with a 403 or unclear client error."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "How many recent denials to return (default 20).",
            },
        },
        "additionalProperties": False,
    },
}


class NetworkToolCallError(ValueError):
    pass


def action_listing() -> list[dict[str, Any]]:
    return [LIST_NETWORK_INTEGRATIONS_TOOL, RECENT_NETWORK_DENIALS_TOOL]


def call_action(name: Any, tool_input: Any) -> dict[str, Any]:
    if name == "list_network_integrations":
        return _list_network_integrations()
    if name == "recent_network_denials":
        return _recent_network_denials(tool_input)
    raise NetworkToolCallError(f"Unknown network tool: {name}.")


def _list_network_integrations() -> dict[str, Any]:
    policy = network_policy.load_policy()
    stored_integrations = policy.get("network_integrations")
    stored_integrations = stored_integrations if isinstance(stored_integrations, dict) else {}
    integrations = []
    for integration_id, registered in registry.NETWORK_INTEGRATIONS.items():
        stored = stored_integrations.get(integration_id)
        stored = stored if isinstance(stored, dict) else {}
        entry: dict[str, Any] = {
            "integration_id": integration_id,
            "display_name": registered.manifest.display_name,
            "description": registered.manifest.description,
            # A disabled integration serializes away entirely, so presence in
            # the stored policy is enablement.
            "enabled": bool(stored),
        }
        options = {key: value for key, value in stored.items() if key != "enabled"}
        if options:
            entry["options"] = options
        integrations.append(entry)
    return {"status": "executed", "result": {"network_integrations": integrations}}


def _recent_network_denials(tool_input: Any) -> dict[str, Any]:
    limit = 20
    if isinstance(tool_input, dict) and tool_input.get("limit") is not None:
        raw_limit = tool_input["limit"]
        if not isinstance(raw_limit, int) or isinstance(raw_limit, bool) or not 1 <= raw_limit <= 100:
            raise NetworkToolCallError("limit must be an integer between 1 and 100.")
        limit = raw_limit
    catalog = registry.denial_reason_catalog()
    denials = []
    for event in state.page_network_events_before(None, decision="denied", limit=limit):
        entry = {
            key: event[key]
            for key in ("timestamp", "protocol", "method", "host", "port", "path", "query")
            if key in event
        }
        code = event.get("reason_code")
        if code is not None:
            entry["reason_code"] = code
            reason = catalog.get(code)
            if reason is not None:
                entry["guidance"] = reason.guidance
        denials.append(entry)
    return {"status": "executed", "result": {"denials": denials}}


def _agent_peer_uids() -> frozenset[int]:
    try:
        return frozenset({pwd.getpwnam(AGENT_PEER_USER).pw_uid})
    except KeyError:
        return frozenset({os.getuid()})


class NetworkIntrospectionRequestHandler(BaseHTTPRequestHandler):
    server: "NetworkIntrospectionServer"
    timeout = REQUEST_READ_TIMEOUT_SECONDS

    def address_string(self) -> str:
        return "local"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _peer_allowed(self) -> bool:
        credentials = self.connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        _pid, uid, _gid = struct.unpack("3i", credentials)
        return uid in self.server.agent_uids

    def _send_json(self, status: HTTPStatus | int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if not self._peer_allowed():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Peer not allowed."})
        elif self.path != "/tools":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown path."})
        else:
            self._send_json(HTTPStatus.OK, {"tools": action_listing()})

    def do_POST(self) -> None:
        if not self._peer_allowed():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Peer not allowed."})
            return
        if self.path != "/call":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown path."})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request too large."})
            return
        if not _CALL_SLOTS.acquire(blocking=False):
            self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many concurrent calls."})
            return
        try:
            try:
                body = json.loads(self.rfile.read(length).decode() or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be JSON."})
                return
            if not isinstance(body, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be a JSON object."})
                return
            try:
                result = call_action(body.get("name"), body.get("input"))
            except NetworkToolCallError as exc:
                result = {"status": "failed", "error": str(exc)}
            except Exception:
                result = {"status": "failed", "error": "Network introspection failed."}
            self._send_json(HTTPStatus.OK, result)
        finally:
            _CALL_SLOTS.release()


class NetworkIntrospectionServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True

    def __init__(self, socket_path: str, agent_uids: frozenset[int]) -> None:
        self.agent_uids = agent_uids
        super().__init__(socket_path, NetworkIntrospectionRequestHandler)  # type: ignore[arg-type]

    def server_bind(self) -> None:
        path = Path(str(self.server_address))
        path.unlink(missing_ok=True)
        self.socket.bind(str(path))
        path.chmod(0o666)


def serve_forever(socket_path: str = SOCKET_PATH) -> None:
    NetworkIntrospectionServer(socket_path, _agent_peer_uids()).serve_forever()
