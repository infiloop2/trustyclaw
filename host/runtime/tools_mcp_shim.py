"""Stdio MCP server that forwards tool calls to the host tools socket.

Agent harnesses cannot call the tools service directly — they speak MCP.
This shim is the bridge: Claude Code (``--mcp-config``) and Codex
(``mcp_servers`` in ``/etc/codex/managed_config.toml``) spawn it as
``trustyclaw-agent`` for each session, it serves the MCP handshake plus
``tools/list`` and ``tools/call`` over stdio (newline-delimited JSON-RPC),
and forwards both to the admin service's Unix socket, which authenticates
the calling user by kernel peer credentials.

It runs as the agent user with agent privileges, so it is deliberately a dumb
pipe: no state, no secrets, stdlib only. If the tools socket is unavailable it
reports an empty tool list rather than failing the whole agent session.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
from typing import Any

SOCKET_PATH = os.environ.get("TRUSTYCLAW_TOOLS_SOCKET", "/run/trustyclaw-tools/tools.sock")
REQUEST_TIMEOUT_SECONDS = 120
PENDING_APPROVAL_HINT = (
    "This action needs operator approval. Tell the user to approve or deny it "
    "in the TrustyClaw admin UI (Tools tab), then check the outcome with the "
    "check_tool_approval tool."
)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost", timeout=REQUEST_TIMEOUT_SECONDS)
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


def _tools_request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    connection = UnixHTTPConnection(SOCKET_PATH)
    try:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        decoded = json.loads(response.read().decode("utf-8"))
        if response.status != 200:
            raise RuntimeError(str(decoded.get("error") or f"HTTP {response.status}"))
        return decoded if isinstance(decoded, dict) else {}
    finally:
        connection.close()


def _list_tools() -> list[dict[str, Any]]:
    try:
        tools = _tools_request("GET", "/tools").get("tools")
    except Exception:
        return []
    if not isinstance(tools, list):
        return []
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "inputSchema": tool["input_schema"],
        }
        for tool in tools
        if isinstance(tool, dict)
    ]


def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments")
    try:
        result = _tools_request(
            "POST", "/call", {"name": name, "input": arguments if isinstance(arguments, dict) else {}}
        )
    except Exception as exc:
        return _tool_text(f"Tool call failed: {exc}", is_error=True)
    status = result.get("status")
    if status == "executed":
        return _tool_text(json.dumps(result.get("result"), indent=2))
    if status == "pending_approval":
        pending = {
            "approval_id": result.get("approval_id"),
            "summary": result.get("summary"),
            "next_step": PENDING_APPROVAL_HINT,
        }
        return _tool_text(json.dumps(pending, indent=2))
    error = str(result.get("error") or "Tool call failed.")
    if result.get("reconnect_required"):
        error += " (The operator needs to reconnect this tool in the admin UI.)"
    return _tool_text(error, is_error=True)


def _tool_text(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    if method == "initialize":
        params = message.get("params") or {}
        return {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "trustyclaw-tools", "version": "1.0.0"},
        }
    if method == "tools/list":
        return {"tools": _list_tools()}
    if method == "tools/call":
        return _call_tool(message.get("params") or {})
    if method == "ping":
        return {}
    return None


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue  # notifications need no response
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        try:
            result = _handle(message)
        except Exception as exc:
            response["error"] = {"code": -32603, "message": str(exc) or "Internal error."}
        else:
            if result is None:
                response["error"] = {"code": -32601, "message": "Method not found."}
            else:
                response["result"] = result
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
