"""Agent Chat app backend.

The app owns the Agent Chat thread index, task references, archive state, and
presentation preferences. Host task contents and execution remain host-owned and
are accessed through the host admin API by this backend.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import time
from typing import Any
from urllib.parse import quote, unquote, urlparse
import uuid

from host.constants import LOOPBACK
from host.runtime import db


HOST = os.environ.get("TRUSTYCLAW_APP_HOST", LOOPBACK)
PORT = int(os.environ.get("TRUSTYCLAW_APP_PORT", "7450"))
DB_SCHEMA = os.environ.get("TRUSTYCLAW_APP_DB_SCHEMA", "app_agent_chat")
ADMIN_API_SOCKET = os.environ.get("TRUSTYCLAW_APP_ADMIN_API_SOCKET", "/run/trustyclaw-admin-api/app-backend.sock")
MAX_REQUEST_BODY_BYTES = 128 * 1024
APP_ID = "agent_chat"
RUNTIME_OPTIONS = {"codex", "claude_code"}
DENSITY_OPTIONS = {"compact", "comfortable", "spacious"}
DEFAULT_PREFERENCES = {"density": "comfortable", "show_completed": True}


class AppError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = "TrustyClawAgentChat/0.1"

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _handle(self, method: str) -> None:
        try:
            self._require_host_proxy()
            body = self._read_body()
            response: dict[str, Any]
            path = urlparse(self.path).path
            if method == "GET" and path == "/health":
                response = {"status": "ok", "app": "agent_chat"}
            elif method == "GET" and path == "/preferences":
                response = {"preferences": read_preferences()}
            elif method == "PUT" and path == "/preferences":
                response = {"preferences": write_preferences(body)}
            elif method == "GET" and path == "/threads":
                response = list_app_threads()
            elif method == "GET" and path.startswith("/threads/") and path.endswith("/tasks"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    raise AppError(HTTPStatus.NOT_FOUND, "route not found")
                response = list_app_thread_tasks(_path_segment(parts[1]))
            elif method == "POST" and path.startswith("/threads/") and path.endswith("/archive"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    raise AppError(HTTPStatus.NOT_FOUND, "route not found")
                response = {"thread": archive_app_thread(_path_segment(parts[1]))}
            elif method == "POST" and path == "/tasks":
                response = create_app_task(body)
            elif method == "GET" and path.startswith("/tasks/"):
                parts = path.strip("/").split("/")
                if len(parts) != 2:
                    raise AppError(HTTPStatus.NOT_FOUND, "route not found")
                task_id = _path_segment(parts[1])
                _require_app_task(task_id)
                response = call_admin_api("GET", f"/v1/tasks/{quote(task_id, safe='')}")
            elif method == "POST" and path.startswith("/tasks/"):
                parts = path.strip("/").split("/")
                if len(parts) != 3 or parts[2] not in {"cancel", "kill", "steer"}:
                    raise AppError(HTTPStatus.NOT_FOUND, "route not found")
                task_id = _path_segment(parts[1])
                _require_app_task(task_id)
                response = call_admin_api("POST", f"/v1/tasks/{quote(task_id, safe='')}/{parts[2]}", body)
            else:
                raise AppError(HTTPStatus.NOT_FOUND, "route not found")
            self._send_json(HTTPStatus.OK, response)
        except AppError as exc:
            self._send_json(exc.status, {"error": {"message": exc.message}})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": str(exc)}})

    def _require_host_proxy(self) -> None:
        if self.headers.get("X-TrustyClaw-App-Proxy") != APP_ID:
            raise AppError(HTTPStatus.UNAUTHORIZED, "missing host app proxy marker")

    def _read_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise AppError(HTTPStatus.BAD_REQUEST, "malformed Content-Length") from exc
        if length < 0:
            raise AppError(HTTPStatus.BAD_REQUEST, "malformed Content-Length")
        if length > MAX_REQUEST_BODY_BYTES:
            raise AppError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        if length == 0:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise AppError(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}") from exc

    def _send_json(self, status: HTTPStatus, body: Any) -> None:
        data = json.dumps(body, sort_keys=True).encode()
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(data)


def read_preferences() -> dict[str, Any]:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("SELECT density, show_completed FROM preferences WHERE singleton = TRUE")
        row = cur.fetchone()
    if not row:
        return dict(DEFAULT_PREFERENCES)
    return {"density": row[0], "show_completed": row[1]}


def write_preferences(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise AppError(HTTPStatus.BAD_REQUEST, "preferences request must be an object")
    preferences = body.get("preferences")
    if not isinstance(preferences, dict):
        raise AppError(HTTPStatus.BAD_REQUEST, "preferences must be an object")
    extra = sorted(set(preferences) - {"density", "show_completed"})
    if extra:
        raise AppError(HTTPStatus.BAD_REQUEST, f"unsupported preference field: {extra[0]}")
    current = read_preferences()
    density = preferences.get("density", current["density"])
    show_completed = preferences.get("show_completed", current["show_completed"])
    if density not in DENSITY_OPTIONS:
        raise AppError(HTTPStatus.BAD_REQUEST, "density must be compact, comfortable, or spacious")
    if not isinstance(show_completed, bool):
        raise AppError(HTTPStatus.BAD_REQUEST, "show_completed must be a boolean")
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            """
            INSERT INTO preferences (singleton, density, show_completed, updated_at)
            VALUES (TRUE, %s, %s, %s)
            ON CONFLICT (singleton) DO UPDATE SET
                density = EXCLUDED.density,
                show_completed = EXCLUDED.show_completed,
                updated_at = EXCLUDED.updated_at
            """,
            (density, show_completed, _utc_now()),
        )
    return {"density": density, "show_completed": show_completed}


def list_app_threads() -> dict[str, Any]:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            """
            SELECT thread_id, agent_runtime, updated_at
            FROM threads
            WHERE archived = FALSE
            ORDER BY updated_at DESC, thread_id ASC
            """
        )
        rows = cur.fetchall()
    app_threads: list[dict[str, Any]] = []
    for thread_id, runtime, updated_at in rows:
        task_response = list_app_thread_tasks(thread_id)
        tasks = task_response["tasks"]
        timestamps = [
            str(task.get("updated_at") or task.get("created_at"))
            for task in tasks
            if isinstance(task.get("updated_at") or task.get("created_at"), str)
        ]
        app_threads.append(
            {
                "thread_id": thread_id,
                "agent_runtime": runtime,
                "last_used_at": max(timestamps, default=updated_at),
                "task_count": len(tasks),
                "active_tasks": [
                    {"task_id": task["task_id"], "status": task["status"]}
                    for task in tasks
                    if task.get("status") in {"queued", "running"} and isinstance(task.get("task_id"), str)
                ],
                "app_archived": False,
            }
        )
    app_threads.sort(key=lambda item: str(item.get("last_used_at") or ""), reverse=True)
    return {"threads": app_threads}


def list_app_thread_tasks(thread_id: str) -> dict[str, Any]:
    _require_app_thread(thread_id, include_archived=False)
    known_task_ids = _app_task_ids_for_thread(thread_id)
    response = call_admin_api("GET", f"/v1/threads/{quote(thread_id, safe='')}/tasks")
    host_tasks = response.get("tasks", [])
    if not isinstance(host_tasks, list):
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid task list")
    return {"tasks": [task for task in host_tasks if isinstance(task, dict) and task.get("task_id") in known_task_ids]}


def create_app_task(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise AppError(HTTPStatus.BAD_REQUEST, "task request must be an object")
    thread_id = _required_text(body.get("thread_id"), "thread_id")
    agent_runtime = _required_text(body.get("agent_runtime", "codex"), "agent_runtime")
    if agent_runtime not in RUNTIME_OPTIONS:
        raise AppError(HTTPStatus.BAD_REQUEST, "agent_runtime must be codex or claude_code")
    _validate_app_thread_runtime(thread_id, agent_runtime)
    response = call_admin_api("POST", "/v1/tasks", body)
    task_id = _required_text(response.get("task_id"), "task_id")
    response_thread_id = _required_text(response.get("thread_id"), "thread_id")
    response_runtime = _required_text(response.get("agent_runtime"), "agent_runtime")
    if response_thread_id != thread_id or response_runtime != agent_runtime:
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned mismatched task reference")
    try:
        _record_app_task(thread_id, agent_runtime, task_id)
    except Exception:
        _cancel_orphaned_host_task(task_id)
        raise
    return response


def _cancel_orphaned_host_task(task_id: str) -> None:
    try:
        call_admin_api("POST", f"/v1/tasks/{quote(task_id, safe='')}/cancel", {})
    except Exception:
        pass


def archive_app_thread(thread_id: str) -> dict[str, Any]:
    _require_app_thread(thread_id, include_archived=True)
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "UPDATE threads SET archived = TRUE, updated_at = %s WHERE thread_id = %s",
            (now, thread_id),
        )
        cur.execute("SELECT thread_id, agent_runtime, archived, updated_at FROM threads WHERE thread_id = %s", (thread_id,))
        row = cur.fetchone()
    if not row:
        raise AppError(HTTPStatus.NOT_FOUND, "thread not found")
    return {"thread_id": row[0], "agent_runtime": row[1], "archived": row[2], "updated_at": row[3]}


def _record_app_task(thread_id: str, agent_runtime: str, task_id: str) -> None:
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("SELECT agent_runtime FROM threads WHERE thread_id = %s", (thread_id,))
        row = cur.fetchone()
        if row and row[0] != agent_runtime:
            raise AppError(HTTPStatus.CONFLICT, "thread already belongs to another agent runtime")
        cur.execute(
            """
            INSERT INTO threads (thread_id, agent_runtime, archived, created_at, updated_at)
            VALUES (%s, %s, FALSE, %s, %s)
            ON CONFLICT (thread_id) DO UPDATE SET
                archived = FALSE,
                updated_at = EXCLUDED.updated_at
            """,
            (thread_id, agent_runtime, now, now),
        )
        cur.execute(
            """
            INSERT INTO thread_tasks (task_id, thread_id, created_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (task_id) DO NOTHING
            """,
            (task_id, thread_id, now),
        )


def _require_app_thread(thread_id: str, *, include_archived: bool) -> None:
    with db.transaction() as cur:
        _set_search_path(cur)
        if include_archived:
            cur.execute("SELECT 1 FROM threads WHERE thread_id = %s", (thread_id,))
        else:
            cur.execute("SELECT 1 FROM threads WHERE thread_id = %s AND archived = FALSE", (thread_id,))
        row = cur.fetchone()
    if not row:
        raise AppError(HTTPStatus.NOT_FOUND, "thread not found")


def _validate_app_thread_runtime(thread_id: str, agent_runtime: str) -> None:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("SELECT agent_runtime FROM threads WHERE thread_id = %s", (thread_id,))
        row = cur.fetchone()
    if row and row[0] != agent_runtime:
        raise AppError(HTTPStatus.CONFLICT, "thread already belongs to another agent runtime")


def _app_task_ids_for_thread(thread_id: str) -> set[str]:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("SELECT task_id FROM thread_tasks WHERE thread_id = %s", (thread_id,))
        rows = cur.fetchall()
    return {row[0] for row in rows}


def _require_app_task(task_id: str) -> None:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("SELECT 1 FROM thread_tasks WHERE task_id = %s", (task_id,))
        row = cur.fetchone()
    if not row:
        raise AppError(HTTPStatus.NOT_FOUND, "task not found")


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        status = HTTPStatus.BAD_GATEWAY if label == "task_id" else HTTPStatus.BAD_REQUEST
        raise AppError(status, f"{label} must be a non-empty string")
    return value.strip()


def call_admin_api(method: str, path: str, body: Any = None) -> dict[str, Any]:
    encoded_body = None if body is None else json.dumps(body, sort_keys=True).encode()
    headers = {
        "Host": "trustyclaw-admin-api",
        "X-TrustyClaw-App-Backend": APP_ID,
    }
    if method != "GET":
        headers["Idempotency-Key"] = f"app-{uuid.uuid4()}"
    if encoded_body is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(encoded_body))
    else:
        headers["Content-Length"] = "0"
    request = [f"{method} {path} HTTP/1.1", *(f"{name}: {value}" for name, value in headers.items()), "", ""]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(ADMIN_API_SOCKET)
        sock.sendall("\r\n".join(request).encode() + (encoded_body or b""))
        raw = _read_http_response(sock)
    status, payload = _parse_http_response(raw)
    if status >= 400:
        message = payload.get("error", {}).get("message") if isinstance(payload, dict) else None
        raise AppError(HTTPStatus(status), message or "host admin request failed")
    if not isinstance(payload, dict):
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid response")
    return payload


def _read_http_response(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        if sum(len(item) for item in chunks) > MAX_REQUEST_BODY_BYTES + 4096:
            raise AppError(HTTPStatus.BAD_GATEWAY, "host admin response too large")
    return b"".join(chunks)


def _parse_http_response(raw: bytes) -> tuple[int, Any]:
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned malformed response")
    lines = head.decode("iso-8859-1").split("\r\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError) as exc:
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned malformed status") from exc
    try:
        payload = json.loads(body.decode() or "{}")
    except json.JSONDecodeError as exc:
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid JSON") from exc
    return status, payload


def _set_search_path(cur: Any) -> None:
    cur.execute(f"SET LOCAL search_path TO {_quote_ident(DB_SCHEMA)}")


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _path_segment(value: str) -> str:
    decoded = unquote(value)
    if not decoded or "/" in decoded or "\\" in decoded:
        raise AppError(HTTPStatus.BAD_REQUEST, "invalid path segment")
    return decoded


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main() -> int:
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
