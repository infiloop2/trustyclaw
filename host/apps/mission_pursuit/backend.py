"""Mission Pursuit app backend.

Mission Pursuit is a single persistent workspace shared by the operator and their
agent. The operator talks to the agent through one conversation; the agent
talks back through host tasks and, over time, furnishes the workspace with
artifacts (stored data plus a declarative rendered view) and schedules
(future runs it plans for itself).

The security boundary in one sentence: the agent's only channel into this app
is the host's agent app API (the ``app_api`` tool, proxied to the ``/agent/``
routes below with kernel-attributed task identity — see
docs/architecture/apps/agent-app-api.md), and this backend is the sole validator
and writer. The host attaches the app package's static agent instructions;
the app composes current task input from the live workspace digest, a tiny
bounded handoff after internal provider-thread rotation, and the message.
During the turn the agent calls
``POST /agent/actions`` (one action per call, the same strict schemas and
caps as ever), ``GET /agent/artifacts/<id>`` for full artifact reads, and
``GET /agent/workspace`` for a state refresh. Only the current thread while it
has an active run may call, verified against the host-attributed thread marker
and the app's own state.
Invalid actions are rejected synchronously — the agent sees the exact error
and can retry within the turn — and every applied or rejected action is
journaled in the operator feed. A completed task's output is plain chat.
Host access stays inside the normal app-backend socket allowlist: create
task, read task, cancel/kill/steer.

All work the app starts is a row in ``runs`` and passes through one run worker:
reap terminal host tasks, fire due schedules by inserting pending runs, then
dispatch the next pending run as a host task. Failed dispatches retry from
scratch; a lost host response can therefore leave the accepted task running
and create another task on retry.
"""

from __future__ import annotations

import calendar
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import re
import socket
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from host.constants import LOOPBACK
from host.runtime import db
from host.session_options import public_session_options, session_config_error


HOST = os.environ.get("TRUSTYCLAW_APP_HOST", LOOPBACK)
PORT = int(os.environ.get("TRUSTYCLAW_APP_PORT", "7451"))
DB_SCHEMA = os.environ.get("TRUSTYCLAW_APP_DB_SCHEMA", "app_mission_pursuit")
ADMIN_API_SOCKET = os.environ.get("TRUSTYCLAW_APP_ADMIN_API_SOCKET", "/run/trustyclaw-admin-api/app-backend.sock")
APP_ID = "mission_pursuit"

# Must comfortably fit a USER_MESSAGE_LIMIT message after the admin bridge
# reserializes it with ASCII escaping (up to 12 encoded bytes per character).
MAX_REQUEST_BODY_BYTES = 512 * 1024
ACTION_NAME_ERROR_BYTES = 80
ACTION_LABEL_BYTES = 40
# Validation error strings can interpolate agent-supplied block types and field
# names; clip them before they are journaled to the feed or returned to the
# agent so a huge payload cannot bloat the messages table or the response.
ERROR_DETAIL_BYTES = 500
# Task outputs carry artifact payloads, so host responses get a larger cap
# than the request bodies this backend accepts from the UI bridge.
MAX_ADMIN_RESPONSE_BYTES = 4 * 1024 * 1024

WORKSPACE_THREAD_PREFIX = "ws-"

# Mirror of the host admin API's MESSAGE_LIMIT: composed task inputs must fit.
HOST_INPUT_LIMIT = 50_000
USER_MESSAGE_LIMIT = 20_000
GOAL_LIMIT = 500
MEASUREMENT_LIMIT = 500
TITLE_LIMIT = 120
SCHEDULE_PROMPT_LIMIT = 4_000
DATA_LIMIT = 16_000
VIEW_LIMIT = 16_000
MEMORY_CONTENT_LIMIT = 300
TOOL_NOTE_LIMIT = 200
MAX_ARTIFACTS = 100
MAX_SCHEDULES = 20
MAX_MEMORIES = 40
MAX_TOOLS = 30
TOOL_PRIORITIES = ("must_have", "good_to_have")
TOOL_STATUSES = ("enabled", "implemented", "not_implemented")
# Per-turn write budget, enforced per attributed task id: the feed journals
# every action, so one runaway turn must not flood the operator ledger.
MAX_ACTIONS_PER_TURN = 16
MAX_VIEW_BLOCKS = 64
CONTROL_LABEL_LIMIT = 120
FIELD_VALUE_LIMIT = 1_000
FIELD_PLACEHOLDER_LIMIT = 200
# Bounds the runs backlog (and with it the /workspace busy list) when the
# queue head is blocked; schedule runs are bounded already.
MAX_QUEUED_CHAT_TURNS = 20
SCHEDULE_MIN_MINUTES = 5
SCHEDULE_MAX_MINUTES = 7 * 24 * 60
FEED_MESSAGE_LIMIT = 200
# The admin proxy rejects app backend responses over 1 MiB. Everything
# agent- or operator-authored that the snapshot returns is clipped by its
# *encoded* JSON size, not by character count, because default json.dumps
# escaping expands non-ASCII text up to 12 bytes per character. Message content
# is capped per row; message meta (which can carry an operator-submitted field
# value) and the artifact/memory/tool/schedule display strings are capped the
# same way, so the worst case stays well under the proxy cap: 200 rows x
# (2,500 + 2,000) encoded bytes plus the capped sections is ~0.9 MiB, and one
# full read is at most 800 KiB plus a small envelope.
SNAPSHOT_MESSAGE_BYTES = 2_500
# Per-feed-row cap on the encoded size of a message's meta object.
SNAPSHOT_META_BYTES = 2_000
# Per-string cap for artifact/memory/tool/schedule display fields in the
# snapshot; bounds each section's contribution regardless of escape expansion.
SNAPSHOT_FIELD_BYTES = 400
FULL_MESSAGE_BYTES = 800_000
DIGEST_SCHEDULE_LINES = MAX_SCHEDULES
DIGEST_ARTIFACT_LINES = 60
# When a composed input would exceed the host limit, digest sections are
# trimmed newest-tail-first down to this floor of lines per section, in a
# fixed order (artifacts, tools, memories, schedules). Goal and measurement
# always survive whole.
DIGEST_SECTION_FLOOR = 6
# Only a tiny deterministic bridge crosses an internal provider-thread
# rotation. Durable workspace state remains the authoritative handoff; these
# two messages preserve immediate conversational deixis such as "do that".
RECENT_CONTEXT_MESSAGES = 2
RECENT_CONTEXT_MESSAGE_LIMIT = 1_900

# The nightly dream cycle is seeded as a normal schedule the operator (or
# agent) can pause, edit, or delete; 1440-minute cadence on a fixed UTC-hour
# grid keeps it nightly and drift-free.
DREAM_SCHEDULE_ID = "dream_cycle"
DREAM_HOUR_UTC = 3

SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# UI refreshes and mutations wake the worker immediately. Thirty seconds is
# only the autonomous fallback while no browser or app action is active.
RUN_WORKER_IDLE_SECONDS = 30.0
DISPATCH_RETRY_SECONDS = 30.0
HOST_TASK_STATUSES_TERMINAL = frozenset({"completed", "failed", "cancelled"})

RUN_WORKER_WAKE = threading.Event()
_DISPATCH_ATTEMPTS: dict[int, float] = {}
_LAST_RUN_WORKER: dict[str, float] = {}
# Applied+rejected agent actions per attributed task id, enforcing the
# per-turn budget. In-memory is fine: this process is the only writer, a
# restart forgetting a partial count only re-opens headroom for the same
# bounded turn, and entries are dropped when the run is reaped.
_AGENT_ACTIONS_BY_TASK: dict[str, int] = {}
_AGENT_ACTIONS_LOCK = threading.Lock()

VIEW_BLOCK_TYPES = (
    "heading",
    "text",
    "callout",
    "metrics",
    "cards",
    "details",
    "list",
    "table",
    "checklist",
    "progress",
    "timeline",
    "kanban",
    "chart",
    "code",
    "button",
    "toggle",
    "field",
    "divider",
)
INTERACTIVE_VIEW_BLOCK_TYPES = frozenset({"button", "toggle", "field"})

SETUP_BRIEF = """== Builder setup ==
This workspace has no goal yet, so before anything else, help the human set it up. Work
conversationally, a step or two per turn, not as a form:
1. Ask for an ambitious goal: one that takes days or months, needs real collaboration
   between the two of you, and is worth measuring. Push back gently on small goals.
2. Ask how the two of you will measure progress, then store both (set_goal,
   set_measurement).
3. Sketch the working design together: which artifacts you will keep, what inputs you
   need from the human, how they should prompt you day to day, and which scheduled runs
   you will create for asynchronous work.
4. Go through the tools this goal needs and record each with upsert_tool: priority
   must_have or good_to_have; status enabled (you can use it now), implemented (exists on
   the host but the human must enable it), or not_implemented (does not exist yet; say so
   plainly).
5. Then show the human the levers of this workspace: goal, measurement, memories,
   artifacts, schedules, and the tools inventory. Everything can be changed later by
   asking, and the workspace panels stay editable by the human."""

DREAM_PROMPT = """Nightly dream cycle: clean up and reorganize this workspace's memory.
Review every memory in the digest: merge duplicates, drop stale or incorrect entries,
rewrite unclear ones so each is one small self-contained fact (under 300 characters), and
add memories for anything important from recent work that is not yet recorded. Keep the
tools inventory and the goal/measurement honest while you are at it. Finish with a short
summary of what changed; if nothing needs changing, say so and change nothing."""


class AppError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# HTTP surface (operator UI via the admin-shell bridge)


class Handler(BaseHTTPRequestHandler):
    server_version = "TrustyClawMissionPursuit/0.1"

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _handle(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            body = self._read_body()
            if parsed.path == "/agent" or parsed.path.startswith("/agent/"):
                # Agent calls arrive only through the host's agent-app proxy,
                # which derives the app and thread from kernel-owned scope
                # state. The marker below is host-asserted, never
                # agent-claimed (docs/architecture/apps/agent-app-api.md).
                self._require_agent_proxy()
                response = route_agent_request(
                    method,
                    parsed.path,
                    body,
                    thread_id=self.headers.get("X-TrustyClaw-Agent-Thread", ""),
                )
            else:
                self._require_host_proxy()
                response = route_ui_request(method, parsed.path, body, parse_qs(parsed.query))
            self._send_json(HTTPStatus.OK, response)
        except AppError as exc:
            self._send_json(exc.status, {"error": {"message": exc.message}})
        except Exception as exc:
            # The response body is returned verbatim to callers, including the
            # untrusted agent on /agent/ routes, so never leak raw exception
            # text (SQL, schema, socket paths). Log it for the operator instead.
            print(f"mission_pursuit unhandled error on {method} {self.path}: {exc}", file=sys.stderr, flush=True)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": "internal error"}})

    def _require_host_proxy(self) -> None:
        if self.headers.get("X-TrustyClaw-App-Proxy") != APP_ID:
            raise AppError(HTTPStatus.UNAUTHORIZED, "missing host app proxy marker")

    def _require_agent_proxy(self) -> None:
        if self.headers.get("X-TrustyClaw-Agent-App-Proxy") != APP_ID:
            raise AppError(HTTPStatus.UNAUTHORIZED, "missing host agent proxy marker")

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


def route_ui_request(method: str, path: str, body: Any, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    if method == "GET" and path == "/health":
        age = time.monotonic() - _LAST_RUN_WORKER.get("at", time.monotonic())
        return {"status": "ok", "app": APP_ID, "run_worker_age_seconds": round(age, 1)}
    if method == "GET" and path == "/workspace":
        snapshot = workspace_snapshot()
        # UI polling is a latency hint, not a second execution path: the one
        # run worker still owns reaping, scheduling, and dispatch.
        RUN_WORKER_WAKE.set()
        return snapshot
    if method == "GET" and path == "/session-options":
        return {"session_options": public_session_options()}
    if method == "POST" and path == "/messages":
        return send_message(body)
    if method == "POST" and path == "/settings":
        return update_agent_settings(body)
    if method == "POST" and path == "/interactions":
        return submit_artifact_interaction(body)
    parts = [unquote(part) for part in path.strip("/").split("/")]
    if any("/" in part or "\\" in part or not part for part in parts):
        raise AppError(HTTPStatus.BAD_REQUEST, "invalid path segment")
    if method == "GET" and len(parts) == 2 and parts[0] == "artifacts":
        return {"artifact": read_artifact(parts[1])}
    if method == "GET" and len(parts) == 2 and parts[0] == "messages":
        return {"message": read_message(parts[1])}
    if method == "DELETE" and len(parts) == 2 and parts[0] == "artifacts":
        return delete_artifact_from_ui(parts[1])
    if method == "POST" and len(parts) == 3 and parts[0] == "schedules" and parts[2] in {"enable", "disable"}:
        return set_schedule_enabled(parts[1], parts[2] == "enable")
    if method == "DELETE" and len(parts) == 2 and parts[0] == "schedules":
        return delete_schedule_from_ui(parts[1])
    if method == "POST" and len(parts) == 2 and parts[0] == "memories":
        return edit_memory_from_ui(parts[1], body)
    if method == "DELETE" and len(parts) == 2 and parts[0] == "memories":
        return delete_memory_from_ui(parts[1])
    if method == "DELETE" and len(parts) == 2 and parts[0] == "tools":
        return delete_tool_from_ui(parts[1])
    if method == "POST" and len(parts) == 3 and parts[0] == "tasks" and parts[2] == "stop":
        return stop_task(parts[1])
    if method == "POST" and len(parts) == 3 and parts[0] == "runs" and parts[2] == "discard":
        return discard_pending_run(parts[1])
    raise AppError(HTTPStatus.NOT_FOUND, "route not found")


# ---------------------------------------------------------------------------
# Agent surface (the app_api tool via the host agent-app proxy)


def route_agent_request(method: str, path: str, body: Any, *, thread_id: str) -> dict[str, Any]:
    task_id = _active_task_for_agent_thread(thread_id)
    parts = [unquote(part) for part in path.strip("/").split("/")]
    if any("/" in part or "\\" in part or not part for part in parts):
        raise AppError(HTTPStatus.BAD_REQUEST, "invalid path segment")
    if method == "GET" and parts == ["agent", "workspace"]:
        return agent_workspace_state()
    if method == "GET" and len(parts) == 3 and parts[:2] == ["agent", "artifacts"]:
        return {"artifact": read_artifact(parts[2])}
    if method == "POST" and parts == ["agent", "actions"]:
        return apply_agent_action(body, task_id)
    raise AppError(HTTPStatus.NOT_FOUND, "route not found")


def _active_task_for_agent_thread(thread_id: str) -> str:
    """Return the active task on the current workspace thread. Dispatch is
    serialized, so this is one row; an idle or old settings generation fails
    closed without host-owned attribution state."""
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("SELECT thread_seq FROM workspace WHERE singleton = TRUE")
        workspace = cur.fetchone()
        if not workspace or thread_id != f"{WORKSPACE_THREAD_PREFIX}{workspace[0]}":
            raise AppError(HTTPStatus.FORBIDDEN, "thread does not belong to the current workspace session")
        cur.execute(
            "SELECT task_id FROM runs WHERE status = 'active' AND thread_id = %s"
            " AND task_id IS NOT NULL LIMIT 1",
            (thread_id,),
        )
        active = cur.fetchone()
        if not active:
            raise AppError(HTTPStatus.FORBIDDEN, "thread has no active workspace turn")
        return str(active[0])


def agent_workspace_state() -> dict[str, Any]:
    """The digest's data as JSON: a mid-turn state refresher for the agent."""
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("SELECT goal, measurement FROM workspace WHERE singleton = TRUE")
        workspace = cur.fetchone()
        schedules, artifacts, memories, tools = _digest_rows(cur)
    return {
        "goal": workspace[0] if workspace else "",
        "measurement": workspace[1] if workspace else "",
        "schedules": schedules,
        "artifacts": artifacts,
        "memories": memories,
        "tools": tools,
    }


def apply_agent_action(body: Any, task_id: str) -> dict[str, Any]:
    """Validate and apply one agent action, journaling the outcome either way.
    Rejections come back synchronously (422) with the exact reason, so the
    agent corrects itself within the turn instead of via next-turn notices."""
    if not isinstance(body, dict) or not isinstance(body.get("action"), str):
        raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, 'body must be one action object with an "action" field')
    with _AGENT_ACTIONS_LOCK:
        used = _AGENT_ACTIONS_BY_TASK.get(task_id, 0)
        record_exhaustion = used == MAX_ACTIONS_PER_TURN
        if record_exhaustion:
            # MAX + 1 is the structural "sentinel recorded" state. Further
            # rejected calls stay bounded without growing the feed.
            _AGENT_ACTIONS_BY_TASK[task_id] = used + 1
        elif used < MAX_ACTIONS_PER_TURN:
            _AGENT_ACTIONS_BY_TASK[task_id] = used + 1
    if used >= MAX_ACTIONS_PER_TURN:
        reason = f"action budget exhausted ({MAX_ACTIONS_PER_TURN} per turn)"
        if record_exhaustion:
            try:
                with db.transaction() as cur:
                    _set_search_path(cur)
                    _insert_message(
                        cur,
                        "error",
                        f"Action rejected: {reason}",
                        {"action": "budget_exhausted"},
                        _utc_now(),
                    )
            except Exception:
                # Let the next rejected call retry the one durable sentinel.
                # Do not resurrect state if turn reaping already removed it.
                with _AGENT_ACTIONS_LOCK:
                    if _AGENT_ACTIONS_BY_TASK.get(task_id) == MAX_ACTIONS_PER_TURN + 1:
                        _AGENT_ACTIONS_BY_TASK[task_id] = MAX_ACTIONS_PER_TURN
                raise
        raise AppError(HTTPStatus.TOO_MANY_REQUESTS, reason)
    now = _utc_now()
    error = validate_action_shape(body)
    if error is None:
        with db.transaction() as cur:
            _set_search_path(cur)
            error = _apply_action(cur, body, now)
            if error is not None:
                # The rejection row must survive the rolled-forward
                # transaction; _apply_action made no earlier writes when it
                # returns an error, so committing here records only the row.
                error = _clip_error(error)
                _insert_message(cur, "error", f"Action rejected: {error}", {"action": _action_label(body)}, now)
    else:
        error = _clip_error(error)
        with db.transaction() as cur:
            _set_search_path(cur)
            _insert_message(cur, "error", f"Action rejected: {error}", {"action": _action_label(body)}, now)
    if error is not None:
        raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, error)
    return {"applied": True, "action": body["action"]}


# ---------------------------------------------------------------------------
# UI reads


def workspace_snapshot() -> dict[str, Any]:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT agent_runtime, model, effort, thread_seq, goal, measurement, created_at"
            " FROM workspace WHERE singleton = TRUE"
        )
        row = cur.fetchone()
        workspace = {
            "agent_runtime": row[0] if row else None,
            "model": row[1] if row else None,
            "effort": row[2] if row else None,
            "thread_seq": row[3] if row else 1,
            "goal": row[4] if row else "",
            "measurement": row[5] if row else "",
            "created_at": row[6] if row else None,
        }
        cur.execute(
            "SELECT id, role, content, meta, created_at FROM messages ORDER BY id DESC LIMIT %s",
            (FEED_MESSAGE_LIMIT,),
        )
        messages = []
        for r in reversed(cur.fetchall()):
            content, truncated = clip_encoded_text(r[2], SNAPSHOT_MESSAGE_BYTES)
            messages.append(
                {
                    "id": r[0],
                    "role": r[1],
                    "content": content,
                    "truncated": truncated,
                    "meta": _clip_snapshot_meta(json.loads(r[3])) if r[3] else None,
                    "created_at": r[4],
                }
            )
        cur.execute(
            "SELECT id, kind, status, task_id, host_status, schedule_id, last_error, created_at"
            " FROM runs WHERE status <> 'done' ORDER BY id ASC"
        )
        busy = [
            {
                "run_id": r[0],
                "kind": r[1],
                "status": r[2],
                "task_id": r[3],
                "host_status": r[4],
                "schedule_id": r[5],
                "last_error": r[6],
                "created_at": r[7],
            }
            for r in cur.fetchall()
        ]
        # The prompt is deliberately not in the snapshot: it is agent-authored
        # (up to 4k characters per schedule), and the UI does not render it.
        cur.execute(
            "SELECT s.schedule_id, s.title, s.every_minutes, s.next_run_at, s.enabled,"
            " s.created_at, s.updated_at, r.host_status, r.updated_at"
            " FROM schedules s LEFT JOIN runs r ON r.id = s.last_run_id"
            " ORDER BY s.created_at ASC, s.schedule_id ASC"
        )
        schedules = [
            {
                "schedule_id": r[0],
                "title": clip_encoded_text(r[1], SNAPSHOT_FIELD_BYTES)[0],
                "every_minutes": r[2],
                "next_run_at": r[3],
                "enabled": r[4],
                "created_at": r[5],
                "updated_at": r[6],
                "last_run_status": r[7],
                "last_run_at": r[8],
            }
            for r in cur.fetchall()
        ]
        cur.execute(
            "SELECT artifact_id, title, view IS NOT NULL, LENGTH(data), updated_at"
            " FROM artifacts ORDER BY updated_at DESC, artifact_id ASC"
        )
        artifacts = [
            {
                "artifact_id": r[0],
                "title": clip_encoded_text(r[1], SNAPSHOT_FIELD_BYTES)[0],
                "has_view": r[2],
                "data_chars": r[3],
                "updated_at": r[4],
            }
            for r in cur.fetchall()
        ]
        cur.execute("SELECT memory_id, content, updated_at FROM memories ORDER BY memory_id ASC")
        memories = [
            {"memory_id": r[0], "content": clip_encoded_text(r[1], SNAPSHOT_FIELD_BYTES)[0], "updated_at": r[2]}
            for r in cur.fetchall()
        ]
        cur.execute(
            "SELECT tool_id, title, priority, status, note, updated_at FROM tools"
            " ORDER BY priority DESC, tool_id ASC"
        )
        tools = [
            {
                "tool_id": r[0],
                "title": clip_encoded_text(r[1], SNAPSHOT_FIELD_BYTES)[0],
                "priority": r[2],
                "status": r[3],
                "note": clip_encoded_text(r[4], SNAPSHOT_FIELD_BYTES)[0],
                "updated_at": r[5],
            }
            for r in cur.fetchall()
        ]
    return {
        "workspace": workspace,
        "messages": messages,
        "busy": busy,
        "schedules": schedules,
        "artifacts": artifacts,
        "memories": memories,
        "tools": tools,
    }


def read_message(message_id: str) -> dict[str, Any]:
    if not message_id.isdigit():
        raise AppError(HTTPStatus.NOT_FOUND, "message not found")
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT id, role, content, meta, created_at FROM messages WHERE id = %s",
            (int(message_id),),
        )
        row = cur.fetchone()
    if not row:
        raise AppError(HTTPStatus.NOT_FOUND, "message not found")
    content, truncated = clip_encoded_text(row[2], FULL_MESSAGE_BYTES)
    return {
        "id": row[0],
        "role": row[1],
        "content": content,
        "truncated": truncated,
        "meta": json.loads(row[3]) if row[3] else None,
        "created_at": row[4],
    }


def clip_encoded_text(text: str, max_encoded_bytes: int) -> tuple[str, bool]:
    """Clip text so its JSON-encoded size (escaping included) fits the budget.
    Returns the clipped text and whether clipping happened."""

    def encoded_size(value: str) -> int:
        return len(json.dumps(value).encode()) - 2  # exclude the quotes

    if encoded_size(text) <= max_encoded_bytes:
        return text, False
    # A fitting prefix has at most max_encoded_bytes characters (every
    # character encodes to at least one byte), which bounds the search input.
    text = text[:max_encoded_bytes]
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if encoded_size(text[:middle]) <= max_encoded_bytes:
            low = middle
        else:
            high = middle - 1
    return text[:low], True


def _clip_error(message: str) -> str:
    """Bound a validation error before it is journaled or returned. The message
    may interpolate agent-supplied block types or field names."""
    clipped, _ = clip_encoded_text(message, ERROR_DETAIL_BYTES)
    return clipped


def _clip_snapshot_meta(meta: Any) -> Any:
    """Bound the encoded size of a feed row's meta so the aggregate snapshot
    stays under the admin proxy cap. Only string leaves grow with agent or
    operator input; clip each, then guarantee the whole object fits."""
    if not isinstance(meta, dict):
        return meta
    clipped = {
        key: clip_encoded_text(value, SNAPSHOT_FIELD_BYTES)[0] if isinstance(value, str) else value
        for key, value in meta.items()
    }
    if len(json.dumps(clipped).encode()) <= SNAPSHOT_META_BYTES:
        return clipped
    # Non-string leaves alone pushed it over budget (should not happen for the
    # meta this app writes); drop to a bounded marker rather than blow the cap.
    return {"action": clipped.get("action") if isinstance(clipped.get("action"), str) else "unknown"}


def _is_finite_number(value: Any) -> bool:
    """True only for a real finite int/float. ``math.isfinite`` raises
    OverflowError on an int too large to convert to float, so guard it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def read_artifact(artifact_id: str) -> dict[str, Any]:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT artifact_id, title, data, view, created_at, updated_at"
            " FROM artifacts WHERE artifact_id = %s",
            (artifact_id,),
        )
        row = cur.fetchone()
    if not row:
        raise AppError(HTTPStatus.NOT_FOUND, "artifact not found")
    return {
        "artifact_id": row[0],
        "title": row[1],
        "data": json.loads(row[2]),
        "view": json.loads(row[3]) if row[3] else None,
        "created_at": row[4],
        "updated_at": row[5],
    }


# ---------------------------------------------------------------------------
# UI writes


def send_message(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise AppError(HTTPStatus.BAD_REQUEST, "message request must be an object")
    setting_fields = {"agent_runtime", "model", "effort"}
    extra = sorted(set(body) - {"content"} - setting_fields)
    if extra:
        raise AppError(HTTPStatus.BAD_REQUEST, f"unsupported message field: {extra[0]}")
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise AppError(HTTPStatus.BAD_REQUEST, "content must be a non-empty string")
    content = content.strip()
    if len(content) > USER_MESSAGE_LIMIT:
        raise AppError(HTTPStatus.BAD_REQUEST, f"content must be at most {USER_MESSAGE_LIMIT} characters")
    supplied_settings = setting_fields & set(body)
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        runtime = _workspace_runtime(cur)
        if runtime is None:
            if supplied_settings != setting_fields:
                raise AppError(
                    HTTPStatus.BAD_REQUEST,
                    "agent_runtime, model, and effort are required for the first message",
                )
            requested_runtime = body["agent_runtime"]
            requested_model = body["model"]
            requested_effort = body["effort"]
            error = session_config_error(requested_runtime, requested_model, requested_effort)
            if error is not None:
                raise AppError(HTTPStatus.BAD_REQUEST, error)
            _ensure_workspace(cur, requested_runtime, requested_model, requested_effort, now)
        elif supplied_settings:
            raise AppError(
                HTTPStatus.BAD_REQUEST,
                "agent_runtime, model, and effort are changed through agent settings",
            )
        message_id, run_id, steer_task_id = _queue_human_input(cur, content, None, now)
    steered = False
    if steer_task_id is not None and run_id is not None:
        steered = _steer_running_turn(steer_task_id, run_id, content)
    RUN_WORKER_WAKE.set()
    return {"message_id": message_id, "steered": steered}


def submit_artifact_interaction(body: Any) -> dict[str, Any]:
    """Turn one native artifact control interaction into ordinary human
    input. The currently stored view is authoritative; controls cannot name a
    route, action, or direct state mutation."""
    if not isinstance(body, dict):
        raise AppError(HTTPStatus.BAD_REQUEST, "interaction request must be an object")
    required = {"artifact_id", "control_id", "value"}
    missing = sorted(required - set(body))
    if missing:
        raise AppError(HTTPStatus.BAD_REQUEST, f"interaction is missing required field: {missing[0]}")
    extra = sorted(set(body) - required)
    if extra:
        raise AppError(HTTPStatus.BAD_REQUEST, f"interaction has unsupported field: {extra[0]}")
    artifact_id = body["artifact_id"]
    control_id = body["control_id"]
    for name, value in (("artifact_id", artifact_id), ("control_id", control_id)):
        if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
            raise AppError(HTTPStatus.BAD_REQUEST, f"{name} must match {SLUG_RE.pattern}")

    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        if _workspace_runtime(cur) is None:
            raise AppError(HTTPStatus.CONFLICT, "workspace has not started yet")
        cur.execute("SELECT title, view FROM artifacts WHERE artifact_id = %s", (artifact_id,))
        artifact = cur.fetchone()
        if not artifact:
            raise AppError(HTTPStatus.NOT_FOUND, "artifact not found")
        view = json.loads(artifact[1]) if artifact[1] else []
        control = next(
            (
                block
                for block in view
                if isinstance(block, dict)
                and block.get("type") in INTERACTIVE_VIEW_BLOCK_TYPES
                and block.get("control_id") == control_id
            ),
            None,
        )
        if control is None:
            raise AppError(HTTPStatus.CONFLICT, "control is not present in the current artifact view")
        value_error = _interaction_value_error(control, body["value"])
        if value_error is not None:
            raise AppError(HTTPStatus.CONFLICT, value_error)
        event = {
            "type": "artifact_interaction",
            "artifact_id": artifact_id,
            "control_id": control_id,
            "control_type": control["type"],
            "value": body["value"],
        }
        content = json.dumps(event, sort_keys=True, separators=(",", ":"))
        meta = {
            "action": "artifact_interaction",
            "artifact_id": artifact_id,
            "artifact_title": artifact[0],
            "control_id": control_id,
            "control_label": control["label"],
            "control_type": control["type"],
            "value": body["value"],
        }
        message_id, run_id, steer_task_id = _queue_human_input(cur, content, meta, now)
    steered = False
    if steer_task_id is not None and run_id is not None:
        steered = _steer_running_turn(steer_task_id, run_id, content)
    RUN_WORKER_WAKE.set()
    return {"message_id": message_id, "steered": steered}


def _interaction_value_error(control: dict[str, Any], value: Any) -> str | None:
    kind = control["type"]
    if kind == "button":
        return None if value is True else "button value must be true"
    if kind == "toggle":
        return None if isinstance(value, bool) else "toggle value must be true or false"
    if not isinstance(value, str):
        return "field value must be a string"
    if len(value) > FIELD_VALUE_LIMIT:
        return f"field value must be at most {FIELD_VALUE_LIMIT} characters"
    return None


def _queue_human_input(
    cur: Any,
    content: str,
    meta: dict[str, Any] | None,
    now: str,
) -> tuple[int | None, int | None, str | None]:
    """Insert one human input and its ordinary chat run inside the caller's
    transaction, returning the active task that may accept it as a steer."""
    cur.execute("SELECT COUNT(*) FROM runs WHERE status <> 'done' AND kind = 'chat'")
    queued_row = cur.fetchone()
    if queued_row and queued_row[0] >= MAX_QUEUED_CHAT_TURNS:
        raise AppError(
            HTTPStatus.CONFLICT,
            f"message queue is full ({MAX_QUEUED_CHAT_TURNS} turns); wait or discard queued turns",
        )
    cur.execute(
        "SELECT task_id FROM runs WHERE status = 'active' AND host_status = 'running'"
        " AND NOT EXISTS (SELECT 1 FROM runs WHERE status = 'pending' AND kind = 'chat')"
        " ORDER BY id ASC LIMIT 1"
    )
    steer_row = cur.fetchone()
    steer_task_id = str(steer_row[0]) if steer_row else None
    cur.execute(
        "INSERT INTO messages (role, content, meta, created_at) VALUES ('user', %s, %s, %s) RETURNING id",
        (content, json.dumps(meta, sort_keys=True) if meta else None, now),
    )
    message_row = cur.fetchone()
    message_id = message_row[0] if message_row else None
    cur.execute(
        "INSERT INTO runs (kind, status, message_id, created_at, updated_at)"
        " VALUES ('chat', 'pending', %s, %s, %s) RETURNING id",
        (message_id, now, now),
    )
    run_row = cur.fetchone()
    run_id = run_row[0] if run_row else None
    return message_id, run_id, steer_task_id


def _steer_running_turn(task_id: str, run_id: int, content: str) -> bool:
    """Deliver a mid-turn message as a host task steer. The message keeps its
    queued run until the steer succeeds, so a crash or a task that finished in
    the window degrades to the normal queued turn (at-least-once delivery)
    instead of losing the message."""
    try:
        call_admin_api(
            "POST",
            f"/v1/tasks/{quote(task_id, safe='')}/steer",
            {"steer_message": content},
        )
    except Exception:
        return False
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "UPDATE runs SET status = 'done', host_status = 'steered', updated_at = %s"
            " WHERE id = %s AND status = 'pending' RETURNING id",
            (now, run_id),
        )
        if not cur.fetchone():
            # Dispatch claimed the run first; the message will arrive as its
            # own turn as well, which is the acceptable duplicate.
            return True
        _insert_event(cur, "Steered the message into the running turn", {"action": "steer", "task_id": task_id}, now)
    return True


def update_agent_settings(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise AppError(HTTPStatus.BAD_REQUEST, "settings request must be an object")
    required = {"agent_runtime", "model", "effort"}
    missing = sorted(required - set(body))
    if missing:
        raise AppError(HTTPStatus.BAD_REQUEST, f"settings are missing required field: {missing[0]}")
    extra = sorted(set(body) - required)
    if extra:
        raise AppError(HTTPStatus.BAD_REQUEST, f"unsupported settings field: {extra[0]}")
    requested_runtime = body["agent_runtime"]
    requested_model = body["model"]
    requested_effort = body["effort"]
    error = session_config_error(requested_runtime, requested_model, requested_effort)
    if error is not None:
        raise AppError(HTTPStatus.BAD_REQUEST, error)
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("SELECT agent_runtime, model, effort, thread_seq FROM workspace WHERE singleton = TRUE")
        current = cur.fetchone()
        if not current or not current[0]:
            raise AppError(HTTPStatus.CONFLICT, "workspace has not started yet")
        current_settings = (current[0], current[1], current[2])
        requested_settings = (requested_runtime, requested_model, requested_effort)
        if current_settings == requested_settings:
            return {
                "agent_runtime": current[0],
                "model": current[1],
                "effort": current[2],
                "thread_seq": current[3],
                "changed": False,
            }
        # A settings change rotates the immutable provider-thread config. No
        # pending or active run may cross that boundary.
        cur.execute("SELECT 1 FROM runs WHERE status <> 'done' LIMIT 1")
        if cur.fetchone():
            raise AppError(
                HTTPStatus.CONFLICT,
                "agent settings cannot change while turns are queued or running",
            )
        cur.execute(
            "UPDATE workspace SET thread_seq = thread_seq + 1, agent_runtime = %s, model = %s, effort = %s,"
            " updated_at = %s"
            " WHERE singleton = TRUE RETURNING thread_seq",
            (requested_runtime, requested_model, requested_effort, now),
        )
        row = cur.fetchone()
        thread_seq = row[0] if row else None
        label = f"Switched to {_runtime_label(requested_runtime)} · {requested_model} · {requested_effort.title()}"
        _insert_event(
            cur,
            label,
            {
                "action": "update_agent_settings",
                "agent_runtime": requested_runtime,
                "model": requested_model,
                "effort": requested_effort,
            },
            now,
        )
    return {
        "agent_runtime": requested_runtime,
        "model": requested_model,
        "effort": requested_effort,
        "thread_seq": thread_seq,
        "changed": True,
    }


def set_schedule_enabled(schedule_id: str, enabled: bool) -> dict[str, Any]:
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT every_minutes, next_run_at, enabled FROM schedules WHERE schedule_id = %s",
            (schedule_id,),
        )
        row = cur.fetchone()
        if not row:
            raise AppError(HTTPStatus.NOT_FOUND, "schedule not found")
        every_minutes, next_run_at, currently_enabled = row
        if enabled and every_minutes is None and next_run_at is None:
            raise AppError(HTTPStatus.CONFLICT, "one-shot schedule already ran; ask the agent for a new one")
        if enabled == currently_enabled:
            return {"schedule_id": schedule_id, "enabled": enabled}
        new_next = next_run_at
        if enabled and every_minutes is not None:
            # Re-enabling a paused recurring schedule restarts its cadence now.
            new_next = format_utc(time.time() + every_minutes * 60)
        cur.execute(
            "UPDATE schedules SET enabled = %s, next_run_at = %s, updated_at = %s WHERE schedule_id = %s",
            (enabled, new_next, now, schedule_id),
        )
    RUN_WORKER_WAKE.set()
    return {"schedule_id": schedule_id, "enabled": enabled}


def delete_schedule_from_ui(schedule_id: str) -> dict[str, Any]:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("DELETE FROM schedules WHERE schedule_id = %s RETURNING title", (schedule_id,))
        row = cur.fetchone()
        if not row:
            raise AppError(HTTPStatus.NOT_FOUND, "schedule not found")
        _insert_event(cur, f'Removed schedule "{row[0]}"', {"action": "delete_schedule", "schedule_id": schedule_id})
    return {"deleted": schedule_id}


def delete_artifact_from_ui(artifact_id: str) -> dict[str, Any]:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("DELETE FROM artifacts WHERE artifact_id = %s RETURNING title", (artifact_id,))
        row = cur.fetchone()
        if not row:
            raise AppError(HTTPStatus.NOT_FOUND, "artifact not found")
        _insert_event(cur, f'Removed artifact "{row[0]}"', {"action": "delete_artifact", "artifact_id": artifact_id})
    return {"deleted": artifact_id}


def edit_memory_from_ui(memory_id: str, body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise AppError(HTTPStatus.BAD_REQUEST, "memory request must be an object")
    extra = sorted(set(body) - {"content"})
    if extra:
        raise AppError(HTTPStatus.BAD_REQUEST, f"unsupported memory field: {extra[0]}")
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise AppError(HTTPStatus.BAD_REQUEST, "content must be a non-empty string")
    content = content.strip()
    if len(content) > MEMORY_CONTENT_LIMIT:
        raise AppError(HTTPStatus.BAD_REQUEST, f"content must be at most {MEMORY_CONTENT_LIMIT} characters")
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "UPDATE memories SET content = %s, updated_at = %s WHERE memory_id = %s RETURNING memory_id",
            (content, now, memory_id),
        )
        if not cur.fetchone():
            raise AppError(HTTPStatus.NOT_FOUND, "memory not found")
        _insert_event(cur, f"Edited memory {memory_id}", {"action": "edit_memory", "memory_id": memory_id}, now)
    return {"memory_id": memory_id, "content": content}


def delete_memory_from_ui(memory_id: str) -> dict[str, Any]:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("DELETE FROM memories WHERE memory_id = %s RETURNING memory_id", (memory_id,))
        if not cur.fetchone():
            raise AppError(HTTPStatus.NOT_FOUND, "memory not found")
        _insert_event(cur, f"Removed memory {memory_id}", {"action": "forget", "memory_id": memory_id})
    return {"deleted": memory_id}


def delete_tool_from_ui(tool_id: str) -> dict[str, Any]:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("DELETE FROM tools WHERE tool_id = %s RETURNING title", (tool_id,))
        row = cur.fetchone()
        if not row:
            raise AppError(HTTPStatus.NOT_FOUND, "tool not found")
        _insert_event(cur, f'Removed tool "{row[0]}"', {"action": "delete_tool", "tool_id": tool_id})
    return {"deleted": tool_id}


def discard_pending_run(run_id: str) -> dict[str, Any]:
    """Drop a queued turn that has not become a host task yet. Dispatch is
    strictly serialized, so this is the operator's way to unblock the queue
    when the head run cannot dispatch (for example after a runtime logout)."""
    if not run_id.isdigit():
        raise AppError(HTTPStatus.NOT_FOUND, "run not found")
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "UPDATE runs SET status = 'done', host_status = 'discarded', updated_at = %s"
            " WHERE id = %s AND status = 'pending' RETURNING kind",
            (now, int(run_id)),
        )
        row = cur.fetchone()
        if not row:
            raise AppError(HTTPStatus.NOT_FOUND, "no pending run with that id")
        _insert_event(cur, "Discarded a queued turn", {"action": "discard", "run_id": int(run_id)}, now)
    # A discarded run whose dispatch previously failed leaves a stale retry
    # entry; drop it so the cache does not grow for the process lifetime.
    _DISPATCH_ATTEMPTS.pop(int(run_id), None)
    RUN_WORKER_WAKE.set()
    return {"run_id": int(run_id), "discarded": True}


def stop_task(task_id: str) -> dict[str, Any]:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("SELECT 1 FROM runs WHERE task_id = %s", (task_id,))
        if not cur.fetchone():
            raise AppError(HTTPStatus.NOT_FOUND, "task not found")
    task = call_admin_api("GET", f"/v1/tasks/{quote(task_id, safe='')}")
    status = task.get("status")
    if status == "queued":
        try:
            call_admin_api("POST", f"/v1/tasks/{quote(task_id, safe='')}/cancel", {})
        except AppError:
            # The task started between the read and the cancel; kill it instead.
            call_admin_api("POST", f"/v1/tasks/{quote(task_id, safe='')}/kill", {})
    elif status == "running":
        call_admin_api("POST", f"/v1/tasks/{quote(task_id, safe='')}/kill", {})
    RUN_WORKER_WAKE.set()
    return {"task_id": task_id, "was": status}


# ---------------------------------------------------------------------------
# Agent output protocol: parsing


# ---------------------------------------------------------------------------
# Agent action protocol: shape validation (context-free, unit-testable)


def validate_action_shape(action: dict[str, Any]) -> str | None:
    """Validate one action's shape and field constraints. Returns an error
    string, or None when the action is well-formed. Existence and count
    checks happen later, inside the apply transaction."""
    name = action.get("action")
    if name == "set_goal":
        return _check_fields(action, required={"goal"}, optional=set()) or _check_text(action, "goal", GOAL_LIMIT, allow_empty=True)
    if name == "set_measurement":
        return _check_fields(action, required={"measurement"}, optional=set()) or _check_text(
            action, "measurement", MEASUREMENT_LIMIT, allow_empty=True
        )
    if name == "remember":
        error = _check_fields(action, required={"memory_id", "content"}, optional=set())
        return error or _check_slug(action, "memory_id") or _check_text(action, "content", MEMORY_CONTENT_LIMIT)
    if name == "forget":
        return _check_fields(action, required={"memory_id"}, optional=set()) or _check_slug(action, "memory_id")
    if name == "upsert_tool":
        error = _check_fields(action, required={"tool_id", "title", "priority", "status"}, optional={"note"})
        if error:
            return error
        if action["priority"] not in TOOL_PRIORITIES:
            return f"priority must be one of: {', '.join(TOOL_PRIORITIES)}"
        if action["status"] not in TOOL_STATUSES:
            return f"status must be one of: {', '.join(TOOL_STATUSES)}"
        return (
            _check_slug(action, "tool_id")
            or _check_text(action, "title", TITLE_LIMIT)
            or (_check_text(action, "note", TOOL_NOTE_LIMIT, allow_empty=True) if "note" in action else None)
        )
    if name == "delete_tool":
        return _check_fields(action, required={"tool_id"}, optional=set()) or _check_slug(action, "tool_id")
    if name == "create_artifact":
        error = _check_fields(action, required={"artifact_id", "title"}, optional={"data", "view"})
        return (
            error
            or _check_slug(action, "artifact_id")
            or _check_text(action, "title", TITLE_LIMIT)
            or _check_data(action)
            or _check_view(action)
        )
    if name == "update_artifact":
        error = _check_fields(action, required={"artifact_id"}, optional={"title", "data", "view"})
        if error:
            return error
        if not (set(action) - {"action", "artifact_id"}):
            return "update_artifact needs at least one of title, data, view"
        return (
            _check_slug(action, "artifact_id")
            or (_check_text(action, "title", TITLE_LIMIT) if "title" in action else None)
            or _check_data(action)
            or _check_view(action, allow_null=True)
        )
    if name == "delete_artifact":
        return _check_fields(action, required={"artifact_id"}, optional=set()) or _check_slug(action, "artifact_id")
    if name == "create_schedule":
        error = _check_fields(action, required={"schedule_id", "title", "prompt"}, optional={"every_minutes", "at"})
        return (
            error
            or _check_slug(action, "schedule_id")
            or _check_text(action, "title", TITLE_LIMIT)
            or _check_text(action, "prompt", SCHEDULE_PROMPT_LIMIT)
            or _check_cadence(action, required=True)
        )
    if name == "update_schedule":
        error = _check_fields(action, required={"schedule_id"}, optional={"title", "prompt", "every_minutes", "at", "enabled"})
        if error:
            return error
        if not (set(action) - {"action", "schedule_id"}):
            return "update_schedule needs at least one of title, prompt, every_minutes, at, enabled"
        if "enabled" in action and not isinstance(action["enabled"], bool):
            return "enabled must be true or false"
        return (
            _check_slug(action, "schedule_id")
            or (_check_text(action, "title", TITLE_LIMIT) if "title" in action else None)
            or (_check_text(action, "prompt", SCHEDULE_PROMPT_LIMIT) if "prompt" in action else None)
            or _check_cadence(action, required=False)
        )
    if name == "delete_schedule":
        return _check_fields(action, required={"schedule_id"}, optional=set()) or _check_slug(action, "schedule_id")
    allowed = (
        "set_goal, set_measurement, remember, forget, upsert_tool, delete_tool,"
        " create_artifact, update_artifact, delete_artifact,"
        " create_schedule, update_schedule, delete_schedule"
    )
    display_name = name if isinstance(name, str) else str(name)
    display_name, _ = clip_encoded_text(display_name, ACTION_NAME_ERROR_BYTES)
    return f"unknown action {display_name!r}; allowed actions: {allowed}"


def _check_fields(action: dict[str, Any], *, required: set[str], optional: set[str]) -> str | None:
    name = action.get("action")
    missing = sorted(required - set(action))
    if missing:
        return f"{name} is missing required field: {missing[0]}"
    extra = sorted(set(action) - required - optional - {"action"})
    if extra:
        return f"{name} has unsupported field: {extra[0]}"
    return None


def _check_text(action: dict[str, Any], key: str, limit: int, *, allow_empty: bool = False) -> str | None:
    value = action.get(key)
    if not isinstance(value, str):
        return f"{key} must be a string"
    if not allow_empty and not value.strip():
        return f"{key} must not be empty"
    if len(value) > limit:
        return f"{key} must be at most {limit} characters"
    return None


def _check_slug(action: dict[str, Any], key: str) -> str | None:
    value = action.get(key)
    if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
        return f"{key} must match {SLUG_RE.pattern}"
    return None


def _check_data(action: dict[str, Any]) -> str | None:
    if "data" not in action:
        return None
    try:
        serialized = json.dumps(action["data"], sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return "data must be JSON-serializable with finite numbers"
    if len(serialized) > DATA_LIMIT:
        return f"data must be at most {DATA_LIMIT} characters when serialized"
    return None


def _check_view(action: dict[str, Any], *, allow_null: bool = False) -> str | None:
    if "view" not in action:
        return None
    view = action["view"]
    if view is None:
        return None if allow_null else "view must be a list of blocks"
    return validate_view(view)


def _check_cadence(action: dict[str, Any], *, required: bool) -> str | None:
    has_every = "every_minutes" in action
    has_at = "at" in action
    if has_every and has_at:
        return "use either every_minutes or at, not both"
    if required and not has_every and not has_at:
        return "create_schedule needs every_minutes (recurring) or at (one-shot)"
    if has_every:
        every = action["every_minutes"]
        if isinstance(every, bool) or not isinstance(every, int):
            return "every_minutes must be an integer"
        if not SCHEDULE_MIN_MINUTES <= every <= SCHEDULE_MAX_MINUTES:
            return f"every_minutes must be between {SCHEDULE_MIN_MINUTES} and {SCHEDULE_MAX_MINUTES}"
    if has_at:
        if not isinstance(action["at"], str) or parse_utc(action["at"]) is None:
            return "at must be a UTC timestamp like 2026-07-09T15:00:00Z"
    return None


def validate_view(view: Any) -> str | None:
    if not isinstance(view, list) or not view:
        return "view must be a non-empty list of blocks"
    if len(view) > MAX_VIEW_BLOCKS:
        return f"view must have at most {MAX_VIEW_BLOCKS} blocks"
    control_ids: set[str] = set()
    for index, block in enumerate(view):
        error = _validate_block(block)
        if error:
            return f"view block {index + 1}: {error}"
        if block["type"] in INTERACTIVE_VIEW_BLOCK_TYPES:
            control_id = block["control_id"]
            if control_id in control_ids:
                return f"view block {index + 1}: duplicate control_id {control_id!r}"
            control_ids.add(control_id)
    if len(json.dumps(view, sort_keys=True)) > VIEW_LIMIT:
        return f"view must be at most {VIEW_LIMIT} characters when serialized"
    return None


def _validate_block(block: Any) -> str | None:
    if not isinstance(block, dict):
        return "block must be an object"
    kind = block.get("type")
    if kind == "heading":
        error = _block_fields(block, {"text"}, {"level"})
        if error:
            return error
        if "level" in block and block["level"] not in (1, 2, 3):
            return "heading level must be 1, 2, or 3"
        return _block_text(block, "text", 200)
    if kind == "text":
        return _block_fields(block, {"text"}, set()) or _block_text(block, "text", 4000)
    if kind == "callout":
        error = _block_fields(block, {"text"}, {"title", "tone"})
        if error:
            return error
        if block.get("tone", "info") not in ("info", "success", "warning", "danger"):
            return "callout tone must be info, success, warning, or danger"
        if "title" in block:
            error = _block_text(block, "title", 120)
            if error:
                return error
        return _block_text(block, "text", 2000)
    if kind == "metrics":
        error = _block_fields(block, {"items"}, set())
        if error:
            return error
        items = block["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 8:
            return "metrics items must be a list of 1 to 8 entries"
        for item in items:
            if not isinstance(item, dict):
                return "metrics items must be objects"
            extra = sorted(set(item) - {"label", "value", "delta"})
            if extra or "label" not in item or "value" not in item:
                return "metrics items need label and value (delta optional)"
            item_error = _block_text(item, "label", 80) or _block_text(item, "value", 40)
            if item_error:
                return item_error
            if "delta" in item:
                item_error = _block_text(item, "delta", 40)
                if item_error:
                    return item_error
        return None
    if kind == "cards":
        error = _block_fields(block, {"items"}, set())
        if error:
            return error
        items = block["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 12:
            return "cards items must be a list of 1 to 12 entries"
        for item in items:
            if not isinstance(item, dict):
                return "cards items must be objects"
            if set(item) - {"title", "text", "badge", "tone"} or "title" not in item:
                return "cards items need title (text, badge, and tone optional)"
            if item.get("tone", "neutral") not in ("neutral", "info", "success", "warning", "danger"):
                return "card tone must be neutral, info, success, warning, or danger"
            item_error = _block_text(item, "title", 120)
            if item_error:
                return item_error
            for field, limit in (("text", 1000), ("badge", 40)):
                if field in item:
                    item_error = _block_text(item, field, limit)
                    if item_error:
                        return item_error
        return None
    if kind == "details":
        error = _block_fields(block, {"items"}, set())
        if error:
            return error
        items = block["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 30:
            return "details items must be a list of 1 to 30 entries"
        for item in items:
            if not isinstance(item, dict) or set(item) != {"label", "value"}:
                return "details items need exactly label and value"
            item_error = _block_text(item, "label", 80) or _block_text(item, "value", 500)
            if item_error:
                return item_error
        return None
    if kind == "list":
        error = _block_fields(block, {"items"}, {"style"})
        if error:
            return error
        if block.get("style", "bullet") not in ("bullet", "number"):
            return "list style must be bullet or number"
        items = block["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            return "list items must be a list of 1 to 50 strings"
        if any(not isinstance(item, str) or not item or len(item) > 500 for item in items):
            return "list items must be non-empty strings of at most 500 characters"
        return None
    if kind == "table":
        error = _block_fields(block, {"columns", "rows"}, set())
        if error:
            return error
        columns = block["columns"]
        rows = block["rows"]
        if not isinstance(columns, list) or not 1 <= len(columns) <= 8:
            return "table columns must be a list of 1 to 8 names"
        if any(not isinstance(column, str) or len(column) > 80 for column in columns):
            return "table columns must be strings of at most 80 characters"
        if not isinstance(rows, list) or len(rows) > 50:
            return "table rows must be a list of at most 50 rows"
        for row in rows:
            if not isinstance(row, list) or len(row) != len(columns):
                return "each table row must match the number of columns"
            if any(not isinstance(cell, str) or len(cell) > 200 for cell in row):
                return "table cells must be strings of at most 200 characters"
        return None
    if kind == "checklist":
        error = _block_fields(block, {"items"}, set())
        if error:
            return error
        items = block["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            return "checklist items must be a list of 1 to 50 entries"
        for item in items:
            if not isinstance(item, dict) or set(item) != {"text", "done"}:
                return "checklist items need exactly text and done"
            if not isinstance(item["done"], bool):
                return "checklist done must be true or false"
            item_error = _block_text(item, "text", 200)
            if item_error:
                return item_error
        return None
    if kind == "progress":
        error = _block_fields(block, {"value"}, {"label"})
        if error:
            return error
        if "label" in block:
            error = _block_text(block, "label", 80)
            if error:
                return error
        value = block["value"]
        if not _is_finite_number(value) or not 0 <= value <= 100:
            return "progress value must be a number between 0 and 100"
        return None
    if kind == "timeline":
        error = _block_fields(block, {"items"}, set())
        if error:
            return error
        items = block["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 30:
            return "timeline items must be a list of 1 to 30 entries"
        for item in items:
            if not isinstance(item, dict):
                return "timeline items must be objects"
            if set(item) - {"title", "status", "text", "time"} or not {"title", "status"} <= set(item):
                return "timeline items need title and status (text and time optional)"
            if item["status"] not in ("done", "current", "upcoming"):
                return "timeline status must be done, current, or upcoming"
            item_error = _block_text(item, "title", 120)
            if item_error:
                return item_error
            for field, limit in (("text", 500), ("time", 80)):
                if field in item:
                    item_error = _block_text(item, field, limit)
                    if item_error:
                        return item_error
        return None
    if kind == "kanban":
        error = _block_fields(block, {"columns"}, set())
        if error:
            return error
        columns = block["columns"]
        if not isinstance(columns, list) or not 1 <= len(columns) <= 6:
            return "kanban columns must be a list of 1 to 6 entries"
        for column in columns:
            if not isinstance(column, dict) or set(column) != {"title", "items"}:
                return "kanban columns need exactly title and items"
            column_error = _block_text(column, "title", 80)
            if column_error:
                return column_error
            items = column["items"]
            if not isinstance(items, list) or len(items) > 20:
                return "kanban column items must be a list of at most 20 strings"
            if any(not isinstance(item, str) or not item or len(item) > 200 for item in items):
                return "kanban items must be non-empty strings of at most 200 characters"
        return None
    if kind == "chart":
        error = _block_fields(block, {"kind", "points"}, {"label"})
        if error:
            return error
        if block["kind"] not in ("bar", "line"):
            return "chart kind must be bar or line"
        if "label" in block:
            error = _block_text(block, "label", 80)
            if error:
                return error
        points = block["points"]
        if not isinstance(points, list) or not 2 <= len(points) <= 60:
            return "chart points must be a list of 2 to 60 points"
        for point in points:
            if not isinstance(point, dict) or set(point) != {"label", "value"}:
                return "chart points need exactly label and value"
            value = point["value"]
            if not _is_finite_number(value):
                return "chart point values must be finite numbers"
            point_error = _block_text(point, "label", 40)
            if point_error:
                return point_error
        return None
    if kind == "code":
        error = _block_fields(block, {"text"}, {"language"})
        if error:
            return error
        if "language" in block:
            error = _block_text(block, "language", 20)
            if error:
                return error
        return _block_text(block, "text", 8000)
    if kind == "button":
        error = _block_fields(block, {"control_id", "label"}, {"tone"})
        if error:
            return error
        if block.get("tone", "primary") not in ("primary", "neutral", "danger"):
            return "button tone must be primary, neutral, or danger"
        return _validate_control_identity(block)
    if kind == "toggle":
        error = _block_fields(block, {"control_id", "label", "value"}, set())
        if error:
            return error
        if not isinstance(block["value"], bool):
            return "toggle value must be true or false"
        return _validate_control_identity(block)
    if kind == "field":
        error = _block_fields(block, {"control_id", "label", "value"}, {"placeholder"})
        if error:
            return error
        identity_error = _validate_control_identity(block)
        if identity_error:
            return identity_error
        value = block["value"]
        if not isinstance(value, str) or len(value) > FIELD_VALUE_LIMIT:
            return f"field value must be a string of at most {FIELD_VALUE_LIMIT} characters"
        if "placeholder" in block:
            placeholder = block["placeholder"]
            if not isinstance(placeholder, str) or len(placeholder) > FIELD_PLACEHOLDER_LIMIT:
                return f"field placeholder must be a string of at most {FIELD_PLACEHOLDER_LIMIT} characters"
        return None
    if kind == "divider":
        return _block_fields(block, set(), set())
    return f"unknown block type {kind!r}; allowed types: {', '.join(VIEW_BLOCK_TYPES)}"


def _block_fields(block: dict[str, Any], required: set[str], optional: set[str]) -> str | None:
    missing = sorted(required - set(block))
    if missing:
        return f"missing field {missing[0]}"
    extra = sorted(set(block) - required - optional - {"type"})
    if extra:
        return f"unsupported field {extra[0]}"
    return None


def _block_text(block: dict[str, Any], key: str, limit: int) -> str | None:
    value = block.get(key)
    if not isinstance(value, str) or not value:
        return f"{key} must be a non-empty string"
    if len(value) > limit:
        return f"{key} must be at most {limit} characters"
    return None


def _validate_control_identity(block: dict[str, Any]) -> str | None:
    return _check_slug(block, "control_id") or _block_text(block, "label", CONTROL_LABEL_LIMIT)


# ---------------------------------------------------------------------------
# Applying actions (context checks + writes, one transaction per task result)


def _forget_run_caches(run: dict[str, Any]) -> None:
    """Drop a terminated run's process-local cache entries. Task and run ids
    never repeat, so leaking these grows the dicts for the process lifetime."""
    with _AGENT_ACTIONS_LOCK:
        _AGENT_ACTIONS_BY_TASK.pop(str(run.get("task_id")), None)
    run_id = run.get("id")
    if run_id is not None:
        _DISPATCH_ATTEMPTS.pop(run_id, None)


def apply_run_result(run: dict[str, Any], task: dict[str, Any]) -> None:
    now = _utc_now()
    status = task.get("status")
    with db.transaction() as cur:
        _set_search_path(cur)
        if status == "completed":
            # Actions were applied live through /agent/actions during the
            # turn; the completed output is the plain chat reply. Clip before
            # storing so nothing past the readable cap becomes dead weight.
            reply = str(task.get("output_message") or "").strip()
            if reply:
                reply, _ = clip_encoded_text(reply, FULL_MESSAGE_BYTES)
                _insert_message(cur, "agent", reply, None, now)
        elif status == "failed":
            detail = str(task.get("error_message") or "task failed")
            detail, _ = clip_encoded_text(detail, FULL_MESSAGE_BYTES)
            _insert_message(cur, "error", f"Agent turn failed: {detail}", None, now)
        elif status == "cancelled":
            _insert_event(cur, f"Stopped {run['task_id']}", {"action": "stop", "task_id": run["task_id"]}, now)
        cur.execute(
            "UPDATE runs SET status = 'done', host_status = %s, updated_at = %s WHERE id = %s",
            (status, now, run["id"]),
        )
    # The turn is over: its per-turn action budget entry is no longer needed,
    # and the terminal task can no longer pass the active-run check anyway.
    _forget_run_caches(run)


def _apply_action(cur: Any, action: dict[str, Any], now: str) -> str | None:
    name = action["action"]
    if name == "set_goal":
        goal = action["goal"].strip()
        cur.execute("UPDATE workspace SET goal = %s, updated_at = %s WHERE singleton = TRUE", (goal, now))
        _insert_event(cur, "Set the goal" if goal else "Cleared the goal", {"action": name}, now)
        return None
    if name == "set_measurement":
        measurement = action["measurement"].strip()
        cur.execute("UPDATE workspace SET measurement = %s, updated_at = %s WHERE singleton = TRUE", (measurement, now))
        _insert_event(cur, "Set the measurement" if measurement else "Cleared the measurement", {"action": name}, now)
        return None
    if name == "remember":
        memory_id = action["memory_id"]
        cur.execute("SELECT 1 FROM memories WHERE memory_id = %s", (memory_id,))
        exists = cur.fetchone() is not None
        if not exists:
            cur.execute("SELECT COUNT(*) FROM memories")
            count_row = cur.fetchone()
            if count_row and count_row[0] >= MAX_MEMORIES:
                return f"memory limit reached ({MAX_MEMORIES}); forget one first"
        cur.execute(
            "INSERT INTO memories (memory_id, content, created_at, updated_at) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (memory_id) DO UPDATE SET content = EXCLUDED.content, updated_at = EXCLUDED.updated_at",
            (memory_id, action["content"].strip(), now, now),
        )
        _insert_event(
            cur,
            f"{'Updated' if exists else 'Stored'} memory {memory_id}",
            {"action": name, "memory_id": memory_id},
            now,
        )
        return None
    if name == "forget":
        cur.execute("DELETE FROM memories WHERE memory_id = %s RETURNING memory_id", (action["memory_id"],))
        if not cur.fetchone():
            return f'memory "{action["memory_id"]}" does not exist'
        _insert_event(cur, f"Forgot memory {action['memory_id']}", {"action": name, "memory_id": action["memory_id"]}, now)
        return None
    if name == "upsert_tool":
        tool_id = action["tool_id"]
        cur.execute("SELECT 1 FROM tools WHERE tool_id = %s", (tool_id,))
        exists = cur.fetchone() is not None
        if not exists:
            cur.execute("SELECT COUNT(*) FROM tools")
            count_row = cur.fetchone()
            if count_row and count_row[0] >= MAX_TOOLS:
                return f"tool limit reached ({MAX_TOOLS}); delete one first"
        title = action["title"].strip()
        cur.execute(
            "INSERT INTO tools (tool_id, title, priority, status, note, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (tool_id) DO UPDATE SET title = EXCLUDED.title, priority = EXCLUDED.priority,"
            " status = EXCLUDED.status, note = EXCLUDED.note, updated_at = EXCLUDED.updated_at",
            (tool_id, title, action["priority"], action["status"], action.get("note", "").strip(), now, now),
        )
        _insert_event(
            cur,
            f'{"Updated" if exists else "Recorded"} tool "{title}" ({action["priority"]}, {action["status"]})',
            {"action": name, "tool_id": tool_id},
            now,
        )
        return None
    if name == "delete_tool":
        cur.execute("DELETE FROM tools WHERE tool_id = %s RETURNING title", (action["tool_id"],))
        row = cur.fetchone()
        if not row:
            return f'tool "{action["tool_id"]}" does not exist'
        _insert_event(cur, f'Deleted tool "{row[0]}"', {"action": name, "tool_id": action["tool_id"]}, now)
        return None
    if name == "create_artifact":
        artifact_id = action["artifact_id"]
        cur.execute("SELECT 1 FROM artifacts WHERE artifact_id = %s", (artifact_id,))
        if cur.fetchone():
            return f'artifact "{artifact_id}" already exists; use update_artifact'
        cur.execute("SELECT COUNT(*) FROM artifacts")
        count_row = cur.fetchone()
        if count_row and count_row[0] >= MAX_ARTIFACTS:
            return f"artifact limit reached ({MAX_ARTIFACTS}); delete one first"
        view = action.get("view")
        cur.execute(
            "INSERT INTO artifacts (artifact_id, title, data, view, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (
                artifact_id,
                action["title"].strip(),
                json.dumps(action.get("data"), sort_keys=True),
                json.dumps(view, sort_keys=True) if view is not None else None,
                now,
                now,
            ),
        )
        _insert_event(cur, f'Created artifact "{action["title"].strip()}"', {"action": name, "artifact_id": artifact_id}, now)
        return None
    if name == "update_artifact":
        artifact_id = action["artifact_id"]
        cur.execute("SELECT title FROM artifacts WHERE artifact_id = %s", (artifact_id,))
        row = cur.fetchone()
        if not row:
            return f'artifact "{artifact_id}" does not exist; use create_artifact'
        title = action["title"].strip() if "title" in action else row[0]
        assignments = ["title = %s", "updated_at = %s"]
        params: list[Any] = [title, now]
        if "data" in action:
            assignments.append("data = %s")
            params.append(json.dumps(action["data"], sort_keys=True))
        if "view" in action:
            assignments.append("view = %s")
            params.append(json.dumps(action["view"], sort_keys=True) if action["view"] is not None else None)
        params.append(artifact_id)
        cur.execute(f"UPDATE artifacts SET {', '.join(assignments)} WHERE artifact_id = %s", tuple(params))
        _insert_event(cur, f'Updated artifact "{title}"', {"action": name, "artifact_id": artifact_id}, now)
        return None
    if name == "delete_artifact":
        cur.execute("DELETE FROM artifacts WHERE artifact_id = %s RETURNING title", (action["artifact_id"],))
        row = cur.fetchone()
        if not row:
            return f'artifact "{action["artifact_id"]}" does not exist'
        _insert_event(cur, f'Deleted artifact "{row[0]}"', {"action": name, "artifact_id": action["artifact_id"]}, now)
        return None
    if name == "create_schedule":
        schedule_id = action["schedule_id"]
        cur.execute("SELECT 1 FROM schedules WHERE schedule_id = %s", (schedule_id,))
        if cur.fetchone():
            return f'schedule "{schedule_id}" already exists; use update_schedule'
        cur.execute("SELECT COUNT(*) FROM schedules")
        count_row = cur.fetchone()
        if count_row and count_row[0] >= MAX_SCHEDULES:
            return f"schedule limit reached ({MAX_SCHEDULES}); delete one first"
        every_minutes = action.get("every_minutes")
        next_run_at = _initial_next_run(action, time.time())
        title = action["title"].strip()
        cur.execute(
            "INSERT INTO schedules (schedule_id, title, prompt, every_minutes, next_run_at, enabled, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s)",
            (schedule_id, title, action["prompt"].strip(), every_minutes, next_run_at, now, now),
        )
        _insert_event(
            cur,
            f'Scheduled "{title}" {cadence_text(every_minutes, next_run_at)}',
            {"action": name, "schedule_id": schedule_id},
            now,
        )
        return None
    if name == "update_schedule":
        schedule_id = action["schedule_id"]
        cur.execute(
            "SELECT title, prompt, every_minutes, next_run_at, enabled FROM schedules WHERE schedule_id = %s",
            (schedule_id,),
        )
        row = cur.fetchone()
        if not row:
            return f'schedule "{schedule_id}" does not exist; use create_schedule'
        title = action["title"].strip() if "title" in action else row[0]
        prompt = action["prompt"].strip() if "prompt" in action else row[1]
        every_minutes = row[2]
        next_run_at = row[3]
        enabled = action["enabled"] if "enabled" in action else row[4]
        if "every_minutes" in action:
            every_minutes = action["every_minutes"]
            next_run_at = format_utc(time.time() + every_minutes * 60)
        elif "at" in action:
            every_minutes = None
            next_run_at = _initial_next_run(action, time.time())
            if "enabled" not in action:
                # Giving a completed one-shot a new time means scheduling it
                # again. An explicit enabled=false still stages it disabled.
                enabled = True
        if enabled and every_minutes is None and next_run_at is None:
            return f'schedule "{schedule_id}" is a finished one-shot; give it a new at time to re-enable'
        cur.execute(
            "UPDATE schedules SET title = %s, prompt = %s, every_minutes = %s, next_run_at = %s,"
            " enabled = %s, updated_at = %s WHERE schedule_id = %s",
            (title, prompt, every_minutes, next_run_at, enabled, now, schedule_id),
        )
        _insert_event(cur, f'Updated schedule "{title}"', {"action": name, "schedule_id": schedule_id}, now)
        return None
    if name == "delete_schedule":
        cur.execute("DELETE FROM schedules WHERE schedule_id = %s RETURNING title", (action["schedule_id"],))
        row = cur.fetchone()
        if not row:
            return f'schedule "{action["schedule_id"]}" does not exist'
        _insert_event(cur, f'Deleted schedule "{row[0]}"', {"action": name, "schedule_id": action["schedule_id"]}, now)
        return None
    return f"unknown action {name!r}"


def _action_label(action: dict[str, Any]) -> str:
    """A small, safe label for rejection metadata. The raw action field is
    agent-controlled and may be huge or non-scalar; meta is returned whole by
    snapshot and full-message reads, so it must stay bounded."""
    value = action.get("action")
    if not isinstance(value, str) or not value:
        return "invalid"
    label, _ = clip_encoded_text(value, ACTION_LABEL_BYTES)
    return label


# ---------------------------------------------------------------------------
# Digest, input composition


def build_digest(
    goal: str,
    measurement: str,
    schedules: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    memories: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    budget: int | None = None,
) -> str:
    """Render the workspace digest. When a budget is given and the digest
    would exceed it, item lines are dropped tail-first per section, in a fixed
    order, down to a per-section floor; goal and measurement always survive
    whole. The agent can always re-read the full state mid-turn through
    GET /agent/workspace."""

    def schedule_line(schedule: dict[str, Any]) -> str:
        state = "enabled" if schedule["enabled"] else "paused"
        cadence = cadence_text(schedule["every_minutes"], schedule["next_run_at"])
        next_part = f", next {schedule['next_run_at']}" if schedule["next_run_at"] else ""
        last_part = f", last {schedule['last_run_status']}" if schedule.get("last_run_status") else ""
        return f"- {schedule['schedule_id']}: \"{schedule['title']}\" {cadence}, {state}{next_part}{last_part}"

    def artifact_line(artifact: dict[str, Any]) -> str:
        surface_part = "renders view" if artifact["has_view"] else "no view"
        return (
            f"- {artifact['artifact_id']}: \"{artifact['title']}\" updated {artifact['updated_at']},"
            f" data {artifact['data_chars']} chars, {surface_part}"
        )

    def tool_line(tool: dict[str, Any]) -> str:
        note_part = f" — {tool['note']}" if tool.get("note") else ""
        return f"- {tool['tool_id']}: \"{tool['title']}\" {tool['priority']}, {tool['status']}{note_part}"

    sections: dict[str, list[str]] = {
        "schedules": [schedule_line(s) for s in schedules[:DIGEST_SCHEDULE_LINES]],
        "tools": [tool_line(t) for t in tools],
        "artifacts": [artifact_line(a) for a in artifacts[:DIGEST_ARTIFACT_LINES]],
        "memories": [f"- {m['memory_id']}: {m['content']}" for m in memories],
    }
    hidden = {
        "schedules": max(0, len(schedules) - DIGEST_SCHEDULE_LINES),
        "tools": 0,
        "artifacts": max(0, len(artifacts) - DIGEST_ARTIFACT_LINES),
        "memories": 0,
    }
    headers = {
        "schedules": f"Schedules ({len(schedules)} of max {MAX_SCHEDULES}):",
        "tools": f"Tools ({len(tools)} of max {MAX_TOOLS}):",
        "artifacts": f"Artifacts ({len(artifacts)} of max {MAX_ARTIFACTS}):",
        "memories": f"Memories ({len(memories)} of max {MAX_MEMORIES}):",
    }

    def render() -> str:
        lines = ["== Workspace state =="]
        lines.append(f"Goal: {goal or '(not set)'}")
        lines.append(f"Measurement: {measurement or '(not set)'}")
        for name in ("schedules", "tools", "artifacts", "memories"):
            lines.append(headers[name])
            lines.extend(sections[name] or ["- (none)"])
            if hidden[name]:
                lines.append(f"- ...and {hidden[name]} more")
        return "\n".join(lines)

    text = render()
    if budget is not None and len(text) > budget:
        for name in ("artifacts", "tools", "memories", "schedules"):
            while len(sections[name]) > DIGEST_SECTION_FLOOR and len(text) > budget:
                sections[name].pop()
                hidden[name] += 1
                text = render()
            if len(text) <= budget:
                break
    return text


def compose_input(
    kind: str,
    digest: str,
    payload: str,
    *,
    setup: bool = False,
    recent_context: str = "",
) -> str:
    if kind == "chat":
        section = f"== Message from the human ==\n{payload}"
    else:
        section = f"== Scheduled run ==\n{payload}"
    parts = []
    if setup:
        parts.append(SETUP_BRIEF)
    if recent_context:
        parts.append(f"== Recent conversation ==\n{recent_context}")
    parts.extend((digest, section))
    return "\n\n".join(parts)


def build_recent_context(cur: Any, before_message_id: int | None) -> str:
    """Return at most two bounded conversational messages, oldest first."""
    if before_message_id is None:
        cur.execute(
            "SELECT role, content FROM messages WHERE role IN ('user', 'agent')"
            " ORDER BY id DESC LIMIT %s",
            (RECENT_CONTEXT_MESSAGES,),
        )
    else:
        cur.execute(
            "SELECT role, content FROM messages WHERE role IN ('user', 'agent') AND id < %s"
            " ORDER BY id DESC LIMIT %s",
            (before_message_id, RECENT_CONTEXT_MESSAGES),
        )
    rows = list(reversed(cur.fetchall()))
    lines = []
    for role, content in rows:
        clipped = content[:RECENT_CONTEXT_MESSAGE_LIMIT]
        if len(content) > RECENT_CONTEXT_MESSAGE_LIMIT:
            clipped = content[: RECENT_CONTEXT_MESSAGE_LIMIT - 1] + "…"
        lines.append(f"{'Human' if role == 'user' else 'Agent'}: {clipped}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Schedule timing


def parse_utc(text: str) -> float | None:
    try:
        return calendar.timegm(time.strptime(text, UTC_FORMAT))
    except (ValueError, TypeError):
        return None


def format_utc(epoch: float) -> str:
    return time.strftime(UTC_FORMAT, time.gmtime(epoch))


def cadence_text(every_minutes: int | None, next_run_at: str | None) -> str:
    if every_minutes is None:
        return f"once at {next_run_at}" if next_run_at else "once (already ran)"
    if every_minutes % (24 * 60) == 0:
        days = every_minutes // (24 * 60)
        return "every day" if days == 1 else f"every {days} days"
    if every_minutes % 60 == 0:
        hours = every_minutes // 60
        return "every hour" if hours == 1 else f"every {hours} hours"
    return f"every {every_minutes} minutes"


def _initial_next_run(action: dict[str, Any], now_epoch: float) -> str:
    if "every_minutes" in action:
        return format_utc(now_epoch + action["every_minutes"] * 60)
    at_epoch = parse_utc(action["at"])
    assert at_epoch is not None  # validated by _check_cadence
    # A past one-shot time means "as soon as possible".
    return format_utc(max(at_epoch, now_epoch))


def schedule_next_run(due_epoch: float, every_minutes: int, now_epoch: float) -> float:
    """Drift-free next fire time: the first due + k*every strictly after now."""
    period = every_minutes * 60
    if now_epoch < due_epoch:
        return due_epoch + period
    steps = math.floor((now_epoch - due_epoch) / period) + 1
    return due_epoch + steps * period


# ---------------------------------------------------------------------------
# The run worker


def run_worker_tick() -> None:
    _reap_active_runs()
    _fire_due_schedules()
    _dispatch_pending_runs()
    _LAST_RUN_WORKER["at"] = time.monotonic()


def _reap_active_runs() -> None:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT id, kind, task_id, host_status, thread_id, agent_runtime, message_id, schedule_id"
            " FROM runs WHERE status = 'active' ORDER BY id ASC"
        )
        rows = cur.fetchall()
    for row in rows:
        run = {
            "id": row[0],
            "kind": row[1],
            "task_id": row[2],
            "host_status": row[3],
            "thread_id": row[4],
            "agent_runtime": row[5],
            "message_id": row[6],
            "schedule_id": row[7],
        }
        try:
            task = call_admin_api("GET", f"/v1/tasks/{quote(str(run['task_id']), safe='')}")
        except AppError as exc:
            if exc.status == HTTPStatus.NOT_FOUND:
                _finish_lost_run(run)
            elif exc.message == "host admin response too large":
                # The task is terminal with an output too large to retrieve.
                # Retrying can never succeed, and dispatch is serialized, so
                # finish the run instead of wedging the queue forever.
                _finish_oversized_run(run)
            continue
        except OSError:
            continue
        status = str(task.get("status") or "")
        try:
            if status in HOST_TASK_STATUSES_TERMINAL:
                apply_run_result(run, task)
                RUN_WORKER_WAKE.set()  # pick up any work created while reaping
            elif status and status != run["host_status"]:
                with db.transaction() as cur:
                    _set_search_path(cur)
                    cur.execute(
                        "UPDATE runs SET host_status = %s, updated_at = %s WHERE id = %s",
                        (status, _utc_now(), run["id"]),
                    )
        except Exception as exc:
            # One bad run must not wedge the loop for schedules and dispatch;
            # it retries on the next tick and stays visible in the busy list.
            print(f"mission_pursuit reap error for run {run['id']}: {exc}", file=sys.stderr, flush=True)


def _finish_oversized_run(run: dict[str, Any]) -> None:
    # Actions were already applied live during the turn; only the chat reply
    # is lost, so this costs the workspace a feed message, not any work.
    now = _utc_now()
    detail = f"The agent's reply for {run['task_id']} was too large to retrieve"
    with db.transaction() as cur:
        _set_search_path(cur)
        _insert_message(cur, "error", detail, None, now)
        cur.execute(
            "UPDATE runs SET status = 'done', host_status = 'oversized', updated_at = %s WHERE id = %s",
            (now, run["id"]),
        )
    _forget_run_caches(run)


def _finish_lost_run(run: dict[str, Any]) -> None:
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        _insert_message(cur, "error", f"Host task {run['task_id']} disappeared; the turn was lost", None, now)
        cur.execute("UPDATE runs SET status = 'done', host_status = 'lost', updated_at = %s WHERE id = %s", (now, run["id"]))
    _forget_run_caches(run)


def _fire_due_schedules() -> None:
    now_epoch = time.time()
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT schedule_id, title, every_minutes, next_run_at FROM schedules"
            " WHERE enabled = TRUE AND next_run_at IS NOT NULL AND next_run_at <= %s"
            " ORDER BY next_run_at ASC",
            (now,),
        )
        due = cur.fetchall()
        for schedule_id, title, every_minutes, next_run_at in due:
            cur.execute(
                "SELECT 1 FROM runs WHERE schedule_id = %s AND status <> 'done' LIMIT 1",
                (schedule_id,),
            )
            overlapping = cur.fetchone() is not None
            if overlapping and every_minutes is None:
                # A due one-shot with its chain still active simply stays due;
                # it fires on the first tick after the chain finishes.
                continue
            if every_minutes is not None:
                due_epoch = parse_utc(next_run_at) or now_epoch
                new_next = format_utc(schedule_next_run(due_epoch, every_minutes, now_epoch))
            else:
                new_next = None
            if overlapping:
                _insert_event(
                    cur,
                    f'Skipped scheduled run "{title}": the previous run is still active',
                    {"action": "schedule_skip", "schedule_id": schedule_id},
                    now,
                )
                cur.execute(
                    "UPDATE schedules SET next_run_at = %s, updated_at = %s WHERE schedule_id = %s",
                    (new_next, now, schedule_id),
                )
                continue
            cur.execute(
                "INSERT INTO runs (kind, status, schedule_id, created_at, updated_at)"
                " VALUES ('schedule', 'pending', %s, %s, %s) RETURNING id",
                (schedule_id, now, now),
            )
            run_row = cur.fetchone()
            cur.execute(
                "UPDATE schedules SET next_run_at = %s, enabled = %s, last_run_id = %s, updated_at = %s"
                " WHERE schedule_id = %s",
                (new_next, every_minutes is not None, run_row[0] if run_row else None, now, schedule_id),
            )
            _insert_event(
                cur,
                f'Schedule "{title}" fired',
                {"action": "schedule_fire", "schedule_id": schedule_id},
                now,
            )


def _dispatch_pending_runs() -> None:
    """Dispatch at most the oldest pending run, and only while nothing is
    active. Runs go out strictly one at a time — /agent/actions only accepts
    the active run's task, so a second in-flight turn could never act — and
    the reap step wakes the loop as soon as the active run finishes."""
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("SELECT 1 FROM runs WHERE status = 'active' LIMIT 1")
        if cur.fetchone():
            return
        cur.execute(
            "SELECT id, kind, thread_id, agent_runtime, message_id, schedule_id FROM runs"
            " WHERE status = 'pending' ORDER BY id ASC LIMIT 1"
        )
        row = cur.fetchone()
    if not row:
        return
    run_id, kind, thread_id, agent_runtime, message_id, schedule_id = row
    monotonic_now = time.monotonic()
    last_attempt = _DISPATCH_ATTEMPTS.get(run_id)
    if last_attempt is not None and monotonic_now - last_attempt < DISPATCH_RETRY_SECONDS:
        return
    _DISPATCH_ATTEMPTS[run_id] = monotonic_now
    try:
        _dispatch_run(run_id, kind, thread_id, agent_runtime, message_id, schedule_id)
        _DISPATCH_ATTEMPTS.pop(run_id, None)
    except AppError as exc:
        _record_dispatch_error(run_id, exc.message)
    except Exception as exc:
        _record_dispatch_error(run_id, str(exc))


def _dispatch_run(
    run_id: int,
    kind: str,
    thread_id: str | None,
    agent_runtime: str | None,
    message_id: int | None,
    schedule_id: str | None,
) -> None:
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT agent_runtime, model, effort, thread_seq, goal, measurement"
            " FROM workspace WHERE singleton = TRUE"
        )
        workspace = cur.fetchone()
        if not workspace or not all(workspace[:3]):
            raise AppError(HTTPStatus.CONFLICT, "workspace agent settings are not set yet")
        runtime = agent_runtime or workspace[0]
        model = workspace[1]
        effort = workspace[2]
        thread = thread_id or f"{WORKSPACE_THREAD_PREFIX}{workspace[3]}"
        goal = workspace[4]
        measurement = workspace[5]
        cur.execute("SELECT 1 FROM runs WHERE thread_id = %s LIMIT 1", (thread,))
        new_thread = cur.fetchone() is None
        # The builder setup guide rides along until the workspace has a goal.
        setup = not goal.strip()
        task_context_length = len(SETUP_BRIEF) + 2 if setup else 0
        schedules, artifacts_index, memories, tools = _digest_rows(cur)
        if kind == "chat":
            cur.execute("SELECT content FROM messages WHERE id = %s", (message_id,))
            message_row = cur.fetchone()
            body_payload = message_row[0] if message_row else ""
        elif kind == "schedule":
            cur.execute("SELECT title, prompt FROM schedules WHERE schedule_id = %s", (schedule_id,))
            schedule_row = cur.fetchone()
            if not schedule_row:
                cur.execute(
                    "UPDATE runs SET status = 'done', host_status = 'orphaned', updated_at = %s WHERE id = %s",
                    (now, run_id),
                )
                return
            body_payload = f'Schedule "{schedule_row[0]}" ({schedule_id}) fired at {now}. Instructions:\n{schedule_row[1]}'
        else:
            # The schema allows only chat and schedule; fail closed if a
            # damaged row still reaches this boundary.
            cur.execute(
                "UPDATE runs SET status = 'done', host_status = 'orphaned', updated_at = %s WHERE id = %s",
                (now, run_id),
            )
            return
        recent_context = build_recent_context(cur, message_id if kind == "chat" else None) if new_thread else ""
        if recent_context:
            task_context_length += len("== Recent conversation ==\n") + len(recent_context) + 2
        # The message is fixed, so the digest flexes to whatever room the
        # setup context and the message leave. Static app instructions are a
        # separate host-owned runtime field and do not consume this user-input
        # budget.
        digest_budget = HOST_INPUT_LIMIT - task_context_length - len(body_payload) - 64
        digest = build_digest(
            goal, measurement, schedules, artifacts_index, memories, tools, budget=digest_budget
        )
    input_message = compose_input(
        kind,
        digest,
        body_payload,
        setup=setup,
        recent_context=recent_context,
    )
    if len(input_message) > HOST_INPUT_LIMIT:
        # The composed sections are individually capped, so this indicates a
        # bug; fail the run visibly instead of hammering the host.
        raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR, "composed task input exceeds the host limit")
    task_body = {"input_message": input_message, "thread_id": thread}
    if new_thread:
        task_body.update({"agent_runtime": runtime, "model": model, "effort": effort})
    response = call_admin_api(
        "POST",
        "/v1/tasks",
        task_body,
    )
    task_id = response.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned an invalid task reference")
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        # The status predicate keeps a concurrent operator discard
        # authoritative: if the row is no longer pending, do not resurrect it.
        cur.execute(
            "UPDATE runs SET status = 'active', task_id = %s, host_status = %s, thread_id = %s,"
            " agent_runtime = %s, last_error = NULL, updated_at = %s"
            " WHERE id = %s AND status = 'pending' RETURNING id",
            (task_id, str(response.get("status") or "queued"), thread, runtime, now, run_id),
        )
        claimed = cur.fetchone() is not None
    if not claimed:
        _cancel_unclaimed_host_task(task_id)


def _cancel_unclaimed_host_task(task_id: str) -> None:
    """Best-effort stop for a host task whose run was discarded mid-dispatch."""
    try:
        call_admin_api("POST", f"/v1/tasks/{quote(task_id, safe='')}/cancel", {})
    except Exception:
        try:
            call_admin_api("POST", f"/v1/tasks/{quote(task_id, safe='')}/kill", {})
        except Exception:
            pass


def _record_dispatch_error(run_id: int, message: str) -> None:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "UPDATE runs SET last_error = %s, updated_at = %s WHERE id = %s AND status = 'pending'",
            (message[:500], _utc_now(), run_id),
        )


def _digest_rows(
    cur: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    cur.execute(
        "SELECT s.schedule_id, s.title, s.every_minutes, s.next_run_at, s.enabled, r.host_status"
        " FROM schedules s LEFT JOIN runs r ON r.id = s.last_run_id"
        " ORDER BY s.created_at ASC, s.schedule_id ASC"
    )
    schedules = [
        {
            "schedule_id": r[0],
            "title": r[1],
            "every_minutes": r[2],
            "next_run_at": r[3],
            "enabled": r[4],
            "last_run_status": r[5],
        }
        for r in cur.fetchall()
    ]
    cur.execute(
        "SELECT artifact_id, title, view IS NOT NULL, LENGTH(data), updated_at"
        " FROM artifacts ORDER BY updated_at DESC, artifact_id ASC"
    )
    artifacts = [
        {"artifact_id": r[0], "title": r[1], "has_view": r[2], "data_chars": r[3], "updated_at": r[4]}
        for r in cur.fetchall()
    ]
    # Newest-updated first, so digest trimming drops the stalest memories.
    cur.execute("SELECT memory_id, content FROM memories ORDER BY updated_at DESC, memory_id ASC")
    memories = [{"memory_id": r[0], "content": r[1]} for r in cur.fetchall()]
    cur.execute(
        "SELECT tool_id, title, priority, status, note FROM tools ORDER BY priority DESC, tool_id ASC"
    )
    tools = [
        {"tool_id": r[0], "title": r[1], "priority": r[2], "status": r[3], "note": r[4]}
        for r in cur.fetchall()
    ]
    return schedules, artifacts, memories, tools


def run_worker_loop() -> None:
    # Start immediately. Thereafter app/UI activity wakes the worker, while
    # the timeout keeps schedules and recovery moving with no browser open.
    RUN_WORKER_WAKE.set()
    while True:
        RUN_WORKER_WAKE.wait(timeout=RUN_WORKER_IDLE_SECONDS)
        RUN_WORKER_WAKE.clear()
        try:
            run_worker_tick()
        except Exception as exc:
            print(f"mission_pursuit run worker error: {exc}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Shared helpers


def _workspace_runtime(cur: Any) -> str | None:
    cur.execute("SELECT agent_runtime FROM workspace WHERE singleton = TRUE")
    row = cur.fetchone()
    return row[0] if row else None


def _ensure_workspace(cur: Any, runtime: str, model: str, effort: str, now: str) -> None:
    cur.execute("SELECT 1 FROM workspace WHERE singleton = TRUE")
    created = cur.fetchone() is None
    cur.execute(
        "INSERT INTO workspace (singleton, agent_runtime, model, effort, thread_seq, goal, created_at, updated_at)"
        " VALUES (TRUE, %s, %s, %s, 1, '', %s, %s)"
        " ON CONFLICT (singleton) DO UPDATE SET agent_runtime = EXCLUDED.agent_runtime, model = EXCLUDED.model,"
        " effort = EXCLUDED.effort, updated_at = EXCLUDED.updated_at",
        (runtime, model, effort, now, now),
    )
    if created:
        _seed_dream_schedule(cur, now)


def _seed_dream_schedule(cur: Any, now: str) -> None:
    """Seed the nightly memory dream cycle as a normal schedule. It is not
    special-cased anywhere else: the operator can pause or delete it and the
    agent can retune it like any schedule it created itself."""
    cur.execute(
        "INSERT INTO schedules (schedule_id, title, prompt, every_minutes, next_run_at, enabled, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s) ON CONFLICT (schedule_id) DO NOTHING",
        (
            DREAM_SCHEDULE_ID,
            "Dream cycle",
            DREAM_PROMPT,
            24 * 60,
            format_utc(_next_dream_epoch(time.time())),
            now,
            now,
        ),
    )
    _insert_event(
        cur,
        "Scheduled the nightly dream cycle: memory cleanup every day at"
        f" {DREAM_HOUR_UTC:02d}:00 UTC (pause or delete it in Schedules)",
        {"action": "create_schedule", "schedule_id": DREAM_SCHEDULE_ID},
        now,
    )


def _next_dream_epoch(now_epoch: float) -> float:
    parts = time.gmtime(now_epoch)
    today = calendar.timegm((parts.tm_year, parts.tm_mon, parts.tm_mday, DREAM_HOUR_UTC, 0, 0, 0, 0, 0))
    return today if now_epoch < today else today + 24 * 3600


def _insert_message(cur: Any, role: str, content: str, meta: dict[str, Any] | None, created_at: str | None = None) -> None:
    cur.execute(
        "INSERT INTO messages (role, content, meta, created_at) VALUES (%s, %s, %s, %s)",
        (role, content, json.dumps(meta, sort_keys=True) if meta else None, created_at or _utc_now()),
    )


def _insert_event(cur: Any, content: str, meta: dict[str, Any], created_at: str | None = None) -> None:
    _insert_message(cur, "event", content, meta, created_at)


def _runtime_label(runtime: str) -> str:
    return "Claude Code" if runtime == "claude_code" else "Codex"


def call_admin_api(method: str, path: str, body: Any = None) -> dict[str, Any]:
    encoded_body = None if body is None else json.dumps(body, sort_keys=True).encode()
    headers = {
        "Host": "trustyclaw-admin-api",
        "X-TrustyClaw-App-Backend": APP_ID,
    }
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
        if sum(len(item) for item in chunks) > MAX_ADMIN_RESPONSE_BYTES:
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


def _utc_now() -> str:
    return time.strftime(UTC_FORMAT, time.gmtime())


def main() -> int:
    threading.Thread(target=run_worker_loop, name="mission_pursuit-run-worker", daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
