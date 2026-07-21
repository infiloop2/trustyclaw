"""Stdio MCP server that forwards tool calls to the host tool sockets.

Agent harnesses cannot call the tool services directly — they speak MCP.
This shim is the bridge: Claude Code (``--mcp-config``), Codex
(``mcp_servers`` in ``/etc/codex/managed_config.toml``), Hermes
(``mcp_servers`` in the managed ``~/.hermes/config.yaml``), and Pi (the
``pi_tools_bridge.js`` extension beside this module — Pi has no MCP client,
so the bridge speaks this protocol for it) spawn it as
``trustyclaw-agent`` for each session, it serves the MCP handshake plus
``tools/list`` and ``tools/call`` over stdio (newline-delimited JSON-RPC),
and forwards both over Unix sockets whose services authenticate the calling
user by kernel peer credentials:

- Bundled tool actions go to the tools socket (``host.runtime.tools.api``).
- Network introspection goes to the agent-network socket
  (``host.runtime.agent_network.api``).
- The ``app_api`` tool goes to the agent-app socket
  (``host.runtime.agent_app.api``), which derives the app-prefixed host thread
  of this shim process from its cgroup and proxies the call to that app
  backend. The tool is always listed so the MCP surface stays stable; calls
  outside an opted-in app thread fail closed at the service.

It runs as the agent user with agent privileges, keeps no state or secrets, and
uses only the stdlib. Its public staging actions open local media as the agent
and stream bytes to the tools service. In the reverse direction, every binary
tool result is atomically materialized at a host-generated path under the
agent's ``/tool_assets`` directory. No pathname crosses into the tools service.
If a socket is unavailable it omits the unavailable bundled tools, rather than
failing the whole agent session.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import stat
import sys
from typing import Any
import urllib.parse

from host import constants

SOCKET_PATH = os.environ.get("TRUSTYCLAW_TOOLS_SOCKET", constants.TOOLS_SOCKET_PATH)
AGENT_APP_SOCKET_PATH = os.environ.get(
    "TRUSTYCLAW_AGENT_APP_SOCKET", constants.AGENT_APP_SOCKET_PATH
)
AGENT_NETWORK_SOCKET_PATH = os.environ.get(
    "TRUSTYCLAW_AGENT_NETWORK_SOCKET",
    constants.AGENT_NETWORK_SOCKET_PATH,
)
APP_API_TOOL_NAME = "app_api"
NETWORK_TOOL_NAMES = frozenset({"list_network_integrations", "recent_network_denials"})
REQUEST_TIMEOUT_SECONDS = 120
PENDING_APPROVAL_HINT = (
    "This action needs operator approval. Tell the user to approve or deny it "
    "in the TrustyClaw admin UI (Tools tab), then check the outcome with the "
    "check_tool_approval tool."
)
MAX_VIDEO_BYTES = 200_000_000
MIN_VIDEO_BYTES = 512
MAX_IMAGE_BYTES = 200_000_000
MIN_IMAGE_BYTES = 512
STREAMING_RESULT_HEADER = "streaming-asset"
STREAMING_MEDIA_TYPE_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
)
STAGE_VIDEO_TOOL = {
    "name": "stage_video",
    "description": (
        "Stream an agent-workspace MP4 or MOV into the private TrustyClaw tools service "
        "for Runway editing or an approval-gated Instagram Reel. Returns a short-lived, "
        "tool-scoped video_asset_id; pass it directly to the consuming tool and never "
        "store it as durable app state."
    ),
    "inputSchema": {
        "type": "object",
        "required": ["path", "for_tool"],
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute MP4 or MOV path from the Agent workspace / Files root.",
            },
            "for_tool": {
                "type": "string",
                "enum": ["runway", "instagram"],
                "description": "Destination tool; staged ids cannot cross tools.",
            },
        },
        "additionalProperties": False,
    },
}
STAGE_IMAGE_TOOL = {
    "name": "stage_image",
    "description": (
        "Stream an agent-workspace JPEG, PNG, or WebP into the private TrustyClaw tools "
        "service for Runway. Returns a short-lived image_asset_id to pass directly to "
        "runway_generate_video; never store it as durable app state."
    ),
    "inputSchema": {
        "type": "object",
        "required": ["path", "for_tool"],
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute JPEG, PNG, or WebP path from the Agent workspace / Files root.",
            },
            "for_tool": {
                "type": "string",
                "enum": ["runway"],
                "description": "Destination tool; staged ids cannot cross tools.",
            },
        },
        "additionalProperties": False,
    },
}


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost", timeout=REQUEST_TIMEOUT_SECONDS)
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


def _tools_request(
    method: str, path: str, body: dict[str, Any] | None = None, socket_path: str = SOCKET_PATH
) -> dict[str, Any]:
    connection = UnixHTTPConnection(socket_path)
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


def _stream_suffix(filename: str) -> str:
    suffix = os.path.splitext(filename)[1]
    if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
        return suffix.lower()
    return ".bin"


def _materialize_stream(response: http.client.HTTPResponse) -> dict[str, Any]:
    """Atomically write one bounded tool stream into agent-owned storage."""
    raw_length = response.getheader("Content-Length", "")
    if not raw_length.isascii() or not raw_length.isdecimal():
        raise RuntimeError("Tool asset stream did not include a valid size.")
    size_bytes = int(raw_length)
    if not 1 <= size_bytes <= MAX_VIDEO_BYTES:
        raise RuntimeError("Tool asset stream size is outside the supported range.")
    media_type = response.getheader("Content-Type", "").strip().lower()
    if not STREAMING_MEDIA_TYPE_RE.fullmatch(media_type):
        raise RuntimeError("Tool asset stream returned an unsupported media type.")
    encoded_filename = response.getheader("X-TrustyClaw-Filename", "")
    if not 1 <= len(encoded_filename) <= 1024:
        raise RuntimeError("Tool asset stream returned an invalid filename.")
    try:
        filename = urllib.parse.unquote(encoded_filename, errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("Tool asset stream returned an invalid filename.") from exc
    if (
        not 1 <= len(filename.encode("utf-8")) <= 255
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise RuntimeError("Tool asset stream returned an invalid filename.")

    agent_home = os.path.realpath(
        os.environ.get("HOME") or "/mnt/trustyclaw-agent/agent-home"
    )
    asset_directory = os.path.join(agent_home, "tool_assets")
    try:
        os.mkdir(asset_directory, 0o700)
    except FileExistsError:
        pass
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(asset_directory, directory_flags)
    except OSError as exc:
        raise RuntimeError("Agent tool_assets storage is unavailable.") from exc
    temporary = f".incoming-{os.getpid()}-{os.urandom(16).hex()}"
    descriptor = -1
    try:
        os.fchmod(directory_fd, 0o700)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        remaining = size_bytes
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            while remaining:
                chunk = response.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("Tool asset stream ended early.")
                destination.write(chunk)
                remaining -= len(chunk)
            destination.flush()
            os.fsync(destination.fileno())

        suffix = _stream_suffix(filename)
        for _ in range(4):
            final_name = f"asset-{os.urandom(16).hex()}{suffix}"
            try:
                os.link(
                    temporary,
                    final_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError("Could not allocate an agent tool asset path.")
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
        return {
            "path": f"/tool_assets/{final_name}",
            "media_type": media_type,
            "size_bytes": size_bytes,
        }
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _tools_action_request(
    body: dict[str, Any], socket_path: str = SOCKET_PATH
) -> dict[str, Any]:
    connection = UnixHTTPConnection(socket_path)
    try:
        payload = json.dumps(body).encode("utf-8")
        connection.request(
            "POST", "/call", body=payload, headers={"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        if (
            response.status == 200
            and response.getheader("X-TrustyClaw-Result") == STREAMING_RESULT_HEADER
        ):
            return {"status": "executed", "result": _materialize_stream(response)}
        raw = response.read()
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"HTTP {response.status} returned an invalid tool response.") from exc
        if response.status != 200:
            message = decoded.get("error") if isinstance(decoded, dict) else None
            raise RuntimeError(str(message or f"HTTP {response.status}"))
        return decoded if isinstance(decoded, dict) else {}
    finally:
        connection.close()


def _listed_socket_tools(socket_path: str) -> list[dict[str, Any]]:
    try:
        tools = _tools_request("GET", "/tools", socket_path=socket_path).get("tools")
    except Exception:
        tools = []
    if not isinstance(tools, list):
        tools = []
    listed = [
        {
            "name": tool["name"],
            "description": tool["description"],
            "inputSchema": tool["input_schema"],
        }
        for tool in tools
        if isinstance(tool, dict)
    ]
    return listed


def _list_tools() -> list[dict[str, Any]]:
    listed = _listed_socket_tools(SOCKET_PATH)
    listed.extend(_listed_socket_tools(AGENT_NETWORK_SOCKET_PATH))
    names = [str(tool.get("name", "")) for tool in listed]
    if any(name.startswith("runway_") for name in names):
        listed.append(STAGE_IMAGE_TOOL)
    if any(name.startswith(("runway_", "instagram_")) for name in names):
        listed.append(STAGE_VIDEO_TOOL)
    listed.append(_app_api_tool())
    return listed


def _stage_asset(arguments: dict[str, Any], *, kind: str) -> dict[str, Any]:
    action = f"stage_{kind}"
    if set(arguments) != {"path", "for_tool"}:
        raise RuntimeError(f"{action} requires exactly path and for_tool.")
    path = arguments.get("path")
    for_tool = arguments.get("for_tool")
    if not isinstance(path, str) or not path:
        raise RuntimeError(f"{action} path must be a non-empty string.")
    public_path, local_path = _workspace_local_path(path)
    allowed_tools = {"runway", "instagram"} if kind == "video" else {"runway"}
    if for_tool not in allowed_tools:
        choices = "runway or instagram" if kind == "video" else "runway"
        raise RuntimeError(f"{action} for_tool must be {choices}.")
    suffix = os.path.splitext(public_path)[1].lower()
    media_types = (
        {".mp4": "video/mp4", ".mov": "video/quicktime"}
        if kind == "video"
        else {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    )
    media_type = media_types.get(suffix)
    if media_type is None:
        supported = "MP4 or MOV" if kind == "video" else "JPEG, PNG, or WebP"
        raise RuntimeError(f"{action} accepts only {supported} files.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(local_path, flags)
    except OSError as exc:
        raise RuntimeError(f"Could not open the {kind} as a regular, non-symlink file.") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"{action} path must be a regular file.")
        minimum = MIN_VIDEO_BYTES if kind == "video" else MIN_IMAGE_BYTES
        maximum = MAX_VIDEO_BYTES if kind == "video" else MAX_IMAGE_BYTES
        if not minimum <= info.st_size <= maximum:
            raise RuntimeError(
                f"{action} file size must be between {minimum} and {maximum} bytes."
            )
        filename = os.path.basename(public_path)
        connection = UnixHTTPConnection(SOCKET_PATH)
        try:
            headers = {
                "Content-Type": media_type,
                "Content-Length": str(info.st_size),
                "X-TrustyClaw-Tool": str(for_tool),
                "X-TrustyClaw-Filename": urllib.parse.quote(filename, safe=""),
            }
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                try:
                    connection.request("POST", f"/assets/{kind}", body=source, headers=headers)
                except (BrokenPipeError, ConnectionResetError):
                    # The service can reply with an error (quota full, bad
                    # filename, too large) and close the socket before we finish
                    # sending the body. Recover its response instead of failing
                    # with an opaque "Broken pipe".
                    pass
                response = connection.getresponse()
                raw = response.read()
            if response.status != 200:
                message = f"HTTP {response.status}"
                try:
                    error = json.loads(raw.decode("utf-8"))
                    if isinstance(error, dict) and error.get("error"):
                        message = str(error["error"])
                except (ValueError, UnicodeDecodeError):
                    pass
                raise RuntimeError(message)
            decoded = json.loads(raw.decode("utf-8"))
            return decoded if isinstance(decoded, dict) else {}
        finally:
            connection.close()
    finally:
        os.close(descriptor)


def _stage_video(arguments: dict[str, Any]) -> dict[str, Any]:
    return _stage_asset(arguments, kind="video")


def _stage_image(arguments: dict[str, Any]) -> dict[str, Any]:
    return _stage_asset(arguments, kind="image")


def _workspace_local_path(path: Any) -> tuple[str, str]:
    """Map a Files-tab path onto this agent process's home directory."""
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        raise RuntimeError("workspace path must be absolute from the agent Files root.")
    parts = [part for part in path.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise RuntimeError("workspace path must not contain dot segments.")
    public_path = "/" + "/".join(parts)
    agent_home = os.path.realpath(
        os.environ.get("HOME") or "/mnt/trustyclaw-agent/agent-home"
    )
    local_path = os.path.join(agent_home, *parts)
    if os.path.commonpath((agent_home, os.path.realpath(local_path))) != agent_home:
        raise RuntimeError("workspace path resolves outside the agent Files root.")
    return public_path, local_path


def _app_api_tool() -> dict[str, Any]:
    """Return the stable app_api declaration; listing grants no authority."""
    return {
        "name": APP_API_TOOL_NAME,
        "description": (
            "Call the installed app backend associated with this app-created thread. "
            "Requests are proxied to the app's /agent/ routes with the app-visible "
            "thread identity attached; the app decides what each route allows. Returns "
            '{"status": <HTTP status>, "body": <response JSON>} so you can read '
            "validation errors and retry within this turn. Use only routes and "
            "request shapes documented by the current app instructions; do not "
            "use this tool when the current instructions define no app API."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                "path": {
                    "type": "string",
                    "description": "App backend route; must start with /agent/",
                },
                "body": {"description": "Optional JSON request body."},
            },
            "required": ["method", "path"],
            "additionalProperties": False,
        },
    }


def _call_app_api(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        result = _tools_request("POST", "/call", arguments, socket_path=AGENT_APP_SOCKET_PATH)
    except RuntimeError as exc:
        return _tool_text(f"App API call failed: {exc}", is_error=True)
    except Exception as exc:
        return _tool_text(f"App API unavailable: {exc}", is_error=True)
    return _tool_text(json.dumps(result, indent=2))


def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments")
    if name == APP_API_TOOL_NAME:
        return _call_app_api(arguments if isinstance(arguments, dict) else {})
    try:
        if not isinstance(name, str):
            raise RuntimeError("tool name must be a string.")
        if name in {"stage_image", "stage_video"}:
            if not isinstance(arguments, dict):
                raise RuntimeError(f"{name} arguments must be an object.")
            stage = _stage_image if name == "stage_image" else _stage_video
            return _tool_text(json.dumps(stage(arguments), indent=2))
        forwarded = dict(arguments) if isinstance(arguments, dict) else {}
        socket_path = AGENT_NETWORK_SOCKET_PATH if name in NETWORK_TOOL_NAMES else SOCKET_PATH
        result = _tools_action_request(
            {"name": name, "input": forwarded},
            socket_path=socket_path,
        )
    except Exception as exc:
        return _tool_text(f"Tool call failed: {exc}", is_error=True)
    status = result.get("status")
    if status == "executed":
        executed = result.get("result")
        return _tool_text(json.dumps(executed, indent=2))
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
