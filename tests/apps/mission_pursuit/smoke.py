"""Mission Pursuit smoke-test mock backend and UI checks."""

from __future__ import annotations

from http import HTTPStatus
import json
import re
from typing import Any, Callable

from host.apps.workspace_kit import views as workspace_views
from host.session_options import public_session_options, session_config_error


ApiErrorFactory = Callable[[HTTPStatus, str], Exception]
HostApi = Callable[[str, str, dict[str, list[str]], Any], dict[str, Any]]

# Fixed literal timestamps spanning ~3 weeks of a live launch mission; the most
# recent entries land within the last day (today is 2026-07-16). No wall-clock.
_BASE = "2026-06-08T00:0{}:00Z"
HOSTILE_VIEW_TEXT = '"><img src=x onerror=window.__x=1>'

WORKSPACE: dict[str, Any] = {
    "agent_runtime": "claude_code",
    "model": "opus",
    "effort": "high",
    "thread_seq": 1,
    "goal": "Get the launch tracker ready for Friday",
    "measurement": "All launch-critical work complete by Friday",
    "created_at": "2026-06-29T08:28:00Z",
}
MESSAGES: list[dict[str, Any]] = [
    {"id": 1, "role": "user", "content": "Set up a launch tracker and check on it every morning.", "meta": None, "created_at": "2026-06-29T08:30:00Z"},
    {"id": 2, "role": "agent", "content": "Done. I built the **Launch Tracker** artifact and scheduled a morning check.", "meta": None, "created_at": "2026-06-29T08:33:00Z"},
    {"id": 3, "role": "event", "content": 'Created artifact "Launch Tracker"', "meta": {"action": "create_artifact", "artifact_id": "launch_tracker"}, "created_at": "2026-06-29T08:33:00Z"},
    {"id": 4, "role": "event", "content": 'Scheduled "Morning check" every day', "meta": {"action": "create_schedule", "schedule_id": "morning_check"}, "created_at": "2026-06-29T08:34:00Z"},
    {"id": 5, "role": "error", "content": 'Action rejected: artifact "old_notes" does not exist', "meta": {"action": "update_artifact"}, "created_at": "2026-06-29T08:36:00Z"},
    {"id": 6, "role": "user", "content": "Add the press kit and metrics dashboard as launch-critical items.", "meta": None, "created_at": "2026-07-02T09:00:00Z"},
    {"id": 7, "role": "agent", "content": "Added both to the tracker. Press kit owner is **Sam**; the metrics dashboard is **Alex**. Both are on the critical path for Friday.", "meta": None, "created_at": "2026-07-02T09:03:00Z"},
    {"id": 8, "role": "event", "content": 'Updated artifact "Launch Tracker"', "meta": {"action": "update_artifact", "artifact_id": "launch_tracker"}, "created_at": "2026-07-02T09:03:00Z"},
    {"id": 9, "role": "agent", "content": "Morning check: 5 of 10 done. API review passed; launch copy still needs Alex's sign-off.", "meta": None, "created_at": "2026-07-08T09:00:00Z"},
    {"id": 10, "role": "event", "content": 'Updated artifact "Launch Tracker"', "meta": {"action": "update_artifact", "artifact_id": "launch_tracker"}, "created_at": "2026-07-08T09:00:00Z"},
    {"id": 11, "role": "user", "content": "Slip final QA to Thursday and keep Friday for launch.", "meta": None, "created_at": "2026-07-13T14:00:00Z"},
    {"id": 12, "role": "agent", "content": "Moved final QA to Thursday. The timeline still holds for a Friday launch; the release review is the only *red* item left.", "meta": None, "created_at": "2026-07-13T14:03:00Z"},
    {"id": 13, "role": "event", "content": 'Updated artifact "Launch Tracker"', "meta": {"action": "update_artifact", "artifact_id": "launch_tracker"}, "created_at": "2026-07-13T14:03:00Z"},
    {"id": 14, "role": "agent", "content": "Morning check for today: **7 of 10** done, release candidate on track for Friday.", "meta": None, "created_at": "2026-07-16T09:00:00Z"},
    {"id": 15, "role": "event", "content": 'Updated artifact "Launch Tracker"', "meta": {"action": "update_artifact", "artifact_id": "launch_tracker"}, "created_at": "2026-07-16T09:00:00Z"},
]
SCHEDULES: list[dict[str, Any]] = [
    {
        "schedule_id": "morning_check",
        "title": "Morning check",
        "every_minutes": 1440,
        "next_run_at": "2026-07-17T09:00:00Z",
        "enabled": True,
        "created_at": "2026-06-29T08:34:00Z",
        "updated_at": "2026-06-29T08:34:00Z",
        "last_run_status": "completed",
        "last_run_at": "2026-07-16T09:00:00Z",
    },
    {
        "schedule_id": "stakeholder_digest",
        "title": "Weekly stakeholder digest",
        "every_minutes": 10080,
        "next_run_at": "2026-07-20T16:00:00Z",
        "enabled": False,
        "created_at": "2026-07-02T16:00:00Z",
        "updated_at": "2026-07-14T16:00:00Z",
        "last_run_status": "failed",
        "last_run_at": "2026-07-13T16:00:00Z",
    },
]
ARTIFACTS: dict[str, dict[str, Any]] = {
    "launch_tracker": {
        "artifact_id": "launch_tracker",
        "title": "Launch Tracker",
        "data": {"tasks_done": 7, "tasks_total": 10},
        "view": [
            {"type": "heading", "text": "Launch Tracker", "level": 1},
            {"type": "callout", "title": "Next milestone", "text": "Release candidate by **Friday**.", "tone": "info"},
            {"type": "metrics", "items": [
                {"label": "Tasks done", "value": "7", "delta": "+2"},
                {"label": "Days left", "value": "4"},
            ]},
            {"type": "cards", "items": [
                {"title": "API", "text": "Ready for review", "badge": "Green", "tone": "success"},
                {"title": "Launch copy", "text": "Needs final approval", "badge": "Waiting", "tone": "warning"},
            ]},
            {"type": "details", "items": [{"label": "Owner", "value": "Alex"}, {"label": "Channel", "value": "Direct launch"}]},
            {"type": "list", "style": "number", "items": ["Finish QA", "Publish release notes"]},
            {"type": "progress", "label": "Overall", "value": 70},
            {"type": "timeline", "items": [
                {"title": "Scope locked", "status": "done", "time": "Mon"},
                {"title": "Release review", "status": "current", "text": "Resolve the final findings", "time": "Today"},
                {"title": "Launch", "status": "upcoming", "time": "Fri"},
            ]},
            {"type": "kanban", "columns": [
                {"title": "Doing", "items": ["Final QA"]},
                {"title": "Done", "items": ["API review", "Launch plan"]},
                {"title": "Blocked", "items": []},
            ]},
            {"type": "chart", "kind": "bar", "label": 'Tasks "closed" <img src=x>', "points": [
                {"label": "Mon", "value": 1}, {"label": 'Tue "quoted"', "value": 2},
                {"label": "Wed", "value": 3}, {"label": "Thu", "value": 1},
            ]},
            {"type": "checklist", "items": [
                {"text": "Draft announcement", "done": True},
                {"text": "Book launch review", "done": False},
            ]},
            {"type": "table", "columns": ["Owner", "Item"], "rows": [["sam", "Press kit"], ["alex", "Metrics dashboard"]]},
            {"type": "button", "control_id": "approve_launch", "label": "Approve launch", "tone": "primary"},
            {"type": "toggle", "control_id": "daily_updates", "label": "Daily updates", "value": True},
            {"type": "field", "control_id": "launch_note", "label": "Launch note", "value": "Ready for review", "placeholder": "Add launch guidance"},
        ],
        "created_at": "2026-06-29T08:33:00Z",
        "updated_at": "2026-07-16T09:00:00Z",
    },
    "comms_plan": {
        "artifact_id": "comms_plan",
        "title": "Comms plan",
        "data": {"channels": 3},
        "view": [
            {"type": "heading", "text": "Launch comms", "level": 2},
            {"type": "cards", "items": [
                {"title": "Blog + changelog", "text": "Drafted; Alex to approve copy.", "badge": "Draft", "tone": "warning"},
                {"title": "Customer email", "text": "Segmented list ready; send Friday 10:00 UTC.", "badge": "Ready", "tone": "success"},
                {"title": "Social", "text": "Handed to Social Marketer for scheduling.", "badge": "Delegated", "tone": "info"},
            ]},
            {"type": "details", "items": [
                {"label": "Embargo", "value": "Friday 09:00 UTC"},
                {"label": "Press kit owner", "value": "Sam"},
            ]},
        ],
        "created_at": "2026-07-02T09:03:00Z",
        "updated_at": "2026-07-13T14:03:00Z",
    },
    "raw_notes": {
        "artifact_id": "raw_notes",
        "title": "Raw Notes",
        "data": {"note": "data-only artifact"},
        "view": None,
        "created_at": "2026-06-29T08:33:00Z",
        "updated_at": "2026-07-08T09:00:00Z",
    },
    "hostile_renderer": {
        "artifact_id": "hostile_renderer",
        "title": "Hostile Renderer Test",
        "data": {},
        "view": [
            {"type": "heading", "text": HOSTILE_VIEW_TEXT, "level": 1},
            {"type": "text", "text": HOSTILE_VIEW_TEXT},
            {"type": "callout", "title": HOSTILE_VIEW_TEXT, "text": HOSTILE_VIEW_TEXT, "tone": "info"},
            {"type": "metrics", "items": [
                {"label": HOSTILE_VIEW_TEXT, "value": HOSTILE_VIEW_TEXT, "delta": HOSTILE_VIEW_TEXT},
            ]},
            {"type": "cards", "items": [
                {"title": HOSTILE_VIEW_TEXT, "text": HOSTILE_VIEW_TEXT, "badge": HOSTILE_VIEW_TEXT},
            ]},
            {"type": "details", "items": [{"label": HOSTILE_VIEW_TEXT, "value": HOSTILE_VIEW_TEXT}]},
            {"type": "list", "items": [HOSTILE_VIEW_TEXT]},
            {"type": "table", "columns": [HOSTILE_VIEW_TEXT], "rows": [[HOSTILE_VIEW_TEXT]]},
            {"type": "checklist", "items": [{"text": HOSTILE_VIEW_TEXT, "done": False}]},
            {"type": "progress", "label": HOSTILE_VIEW_TEXT, "value": 50},
            {"type": "timeline", "items": [{
                "title": HOSTILE_VIEW_TEXT,
                "status": "current",
                "text": HOSTILE_VIEW_TEXT,
                "time": HOSTILE_VIEW_TEXT,
            }]},
            {"type": "kanban", "columns": [{"title": HOSTILE_VIEW_TEXT, "items": [HOSTILE_VIEW_TEXT]}]},
            {"type": "chart", "kind": "bar", "label": HOSTILE_VIEW_TEXT, "points": [
                {"label": HOSTILE_VIEW_TEXT, "value": 1},
                {"label": HOSTILE_VIEW_TEXT, "value": 2},
            ]},
            {"type": "code", "language": "html", "text": HOSTILE_VIEW_TEXT},
            {"type": "button", "control_id": "hostile_button", "label": HOSTILE_VIEW_TEXT},
            {"type": "toggle", "control_id": "hostile_toggle", "label": HOSTILE_VIEW_TEXT, "value": False},
            {
                "type": "field",
                "control_id": "hostile_field",
                "label": HOSTILE_VIEW_TEXT,
                "value": HOSTILE_VIEW_TEXT,
                "placeholder": HOSTILE_VIEW_TEXT,
            },
            {"type": "divider"},
        ],
        "created_at": "2026-07-16T09:00:00Z",
        "updated_at": "2026-07-16T09:00:00Z",
    },
}
MEMORIES: list[dict[str, Any]] = [
    {
        "memory_id": "launch_timezone",
        "content": "The launch team plans and reports in UTC.",
        "updated_at": "2026-06-29T08:34:00Z",
    },
    {
        "memory_id": "approval_owner",
        "content": "Alex gives final approval for launch copy; Sam owns the press kit.",
        "updated_at": "2026-07-02T09:03:00Z",
    },
    {
        "memory_id": "launch_date",
        "content": "Target launch is Friday; the release review is the current critical-path risk.",
        "updated_at": "2026-07-13T14:03:00Z",
    },
    {
        "memory_id": "scope_guard",
        "content": "No new scope before launch; log post-launch ideas in a backlog, do not add to the tracker.",
        "updated_at": "2026-07-08T09:00:00Z",
    },
]
TOOLS: list[dict[str, Any]] = [
    {
        "tool_id": "github",
        "title": "GitHub",
        "priority": "must_have",
        "status": "enabled",
        "note": "Track release blockers and pull requests.",
        "updated_at": "2026-07-16T09:00:00Z",
    },
    {
        "tool_id": "slack",
        "title": "Slack",
        "priority": "good_to_have",
        "status": "enabled",
        "note": "Post the morning check into the launch channel.",
        "updated_at": "2026-07-16T09:00:00Z",
    },
    {
        "tool_id": "analytics",
        "title": "Product analytics",
        "priority": "good_to_have",
        "status": "not_implemented",
        "note": "Useful for the post-launch measurement artifact.",
        "updated_at": "2026-07-13T14:03:00Z",
    },
]
_NEXT_MESSAGE_ID = [len(MESSAGES) + 1]
_NEXT_UPDATE_MINUTE = [10]


def _full_snapshot() -> dict[str, Any]:
    return {
        "workspace": dict(WORKSPACE),
        "messages": [dict(message) for message in MESSAGES],
        "busy": [],
        "schedules": [dict(schedule) for schedule in SCHEDULES],
        "artifacts": [
            {
                "artifact_id": artifact["artifact_id"],
                "title": artifact["title"],
                "has_view": artifact["view"] is not None,
                "data_chars": len(str(artifact["data"])),
                "updated_at": artifact["updated_at"],
            }
            for artifact in ARTIFACTS.values()
        ],
        "memories": [dict(memory) for memory in MEMORIES],
        "tools": [dict(tool) for tool in TOOLS],
    }


def _append_message(role: str, content: str, meta: dict[str, Any] | None = None) -> None:
    MESSAGES.append(
        {"id": _NEXT_MESSAGE_ID[0], "role": role, "content": content, "meta": meta, "created_at": "2026-07-16T14:30:00Z"}
    )
    _NEXT_MESSAGE_ID[0] += 1


# The mock boots unactivated like a real first open: the activation gate
# renders until the operator activates the workspace, then the canned
# workspace story becomes visible.
ACTIVATED = False
EVER_ACTIVATED = False


def _snapshot() -> dict[str, Any]:
    snapshot = _full_snapshot()
    if ACTIVATED:
        return snapshot
    workspace = dict(snapshot["workspace"])
    workspace.update({"agent_runtime": None, "model": None, "effort": None, "goal": "", "measurement": ""})
    if EVER_ACTIVATED:
        snapshot["workspace"] = workspace
        return snapshot
    empty = {key: ([] if isinstance(value, list) else value) for key, value in snapshot.items()}
    empty["workspace"] = workspace
    return empty


def route_app_api(
    method: str,
    relative: str,
    _query: dict[str, list[str]],
    body: Any,
    api_error: ApiErrorFactory,
    host_api: HostApi,
) -> dict[str, Any]:
    if method == "GET" and relative == "health":
        return {"status": "ok", "app": "mission_pursuit", "mock_backend": True}
    if method == "GET" and relative == "workspace":
        return _snapshot()
    if method == "GET" and relative == "session-options":
        return {
            "session_options": public_session_options(),
            "active_runtimes": ["codex", "claude_code"],
        }
    if method == "POST" and relative == "activate":
        global ACTIVATED, EVER_ACTIVATED
        if not isinstance(body, dict) or not all(
            isinstance(body.get(key), str) and body[key] for key in ("agent_runtime", "model", "effort")
        ):
            raise api_error(HTTPStatus.BAD_REQUEST, "activate requires agent_runtime, model, and effort")
        error = session_config_error(body["agent_runtime"], body["model"], body["effort"])
        if error is not None:
            raise api_error(HTTPStatus.BAD_REQUEST, error)
        if not ACTIVATED:
            for key in ("agent_runtime", "model", "effort"):
                WORKSPACE[key] = body[key]
            ACTIVATED = True
            EVER_ACTIVATED = True
        return {"activated": True}
    if method == "POST" and relative == "deactivate":
        ACTIVATED = False
        for schedule in SCHEDULES:
            schedule["enabled"] = False
        return {"activated": False, "stopping_tasks": 0}
    if method == "POST" and relative == "messages":
        if not isinstance(body, dict) or not isinstance(body.get("content"), str) or not body["content"].strip():
            raise api_error(HTTPStatus.BAD_REQUEST, "content must be a non-empty string")
        content = body["content"].strip()
        if not ACTIVATED:
            settings = ("agent_runtime", "model", "effort")
            if not all(isinstance(body.get(key), str) and body[key] for key in settings):
                raise api_error(
                    HTTPStatus.BAD_REQUEST,
                    "agent_runtime, model, and effort are required for the first message",
                )
            for key in settings:
                WORKSPACE[key] = body[key]
            # The operator's brief becomes the first message of the story.
            if MESSAGES and MESSAGES[0].get("role") == "user":
                MESSAGES[0] = {**MESSAGES[0], "content": content}
            ACTIVATED = True
            EVER_ACTIVATED = True
            return {"message_id": 1, "steered": False}
        _append_message("user", content)
        _append_message("agent", f"Mock reply: noted — {content[:60]}")
        return {"message_id": _NEXT_MESSAGE_ID[0] - 2}
    if method == "POST" and relative == "settings":
        if not isinstance(body, dict) or set(body) != {"agent_runtime", "model", "effort"}:
            raise api_error(HTTPStatus.BAD_REQUEST, "settings require agent_runtime, model, and effort")
        error = session_config_error(body["agent_runtime"], body["model"], body["effort"])
        if error is not None:
            raise api_error(HTTPStatus.BAD_REQUEST, error)
        if all(WORKSPACE[field] == body[field] for field in ("agent_runtime", "model", "effort")):
            return {**body, "thread_seq": WORKSPACE["thread_seq"], "changed": False}
        WORKSPACE.update(body)
        WORKSPACE["thread_seq"] += 1
        runtime_label = "Claude Code" if body["agent_runtime"] == "claude_code" else "Codex"
        _append_message(
            "event",
            f'Switched to {runtime_label} · {body["model"]} · {str(body["effort"]).title()}',
            {"action": "update_agent_settings"},
        )
        return {**body, "thread_seq": WORKSPACE["thread_seq"], "changed": True}
    if method == "POST" and relative == "interactions":
        if not isinstance(body, dict) or set(body) != {"artifact_id", "control_id", "value"}:
            raise api_error(HTTPStatus.BAD_REQUEST, "interaction requires artifact_id, control_id, and value")
        artifact = ARTIFACTS.get(body["artifact_id"])
        if artifact is None:
            raise api_error(HTTPStatus.NOT_FOUND, "artifact not found")
        control = next(
            (block for block in artifact.get("view") or [] if block.get("control_id") == body["control_id"]),
            None,
        )
        if control is None:
            raise api_error(HTTPStatus.CONFLICT, "control is not present in the current artifact view")
        if control["type"] == "button" and body["value"] is not True:
            raise api_error(HTTPStatus.CONFLICT, "button value must be true")
        if control["type"] == "toggle" and not isinstance(body["value"], bool):
            raise api_error(HTTPStatus.CONFLICT, "toggle value must be true or false")
        if control["type"] == "field" and not isinstance(body["value"], str):
            raise api_error(HTTPStatus.CONFLICT, "field value must be a string")
        event = {
            "type": "artifact_interaction",
            "artifact_id": artifact["artifact_id"],
            "control_id": control["control_id"],
            "control_type": control["type"],
            "value": body["value"],
        }
        _append_message(
            "user",
            json.dumps(event, sort_keys=True, separators=(",", ":")),
            {
                "action": "artifact_interaction",
                "artifact_id": artifact["artifact_id"],
                "artifact_title": artifact["title"],
                "control_id": control["control_id"],
                "control_label": control["label"],
                "control_type": control["type"],
                "value": body["value"],
            },
        )
        _append_message("agent", f'Mock agent received "{control["label"]}".')
        # Simulate the agent applying an ordinary update_artifact action.
        if control["type"] in {"toggle", "field"}:
            control["value"] = body["value"]
            artifact["updated_at"] = f"2026-07-16T14:{_NEXT_UPDATE_MINUTE[0]:02}:00Z"
            _NEXT_UPDATE_MINUTE[0] += 1
        return {"message_id": _NEXT_MESSAGE_ID[0] - 2, "steered": False}
    match = re.fullmatch(r"artifacts/([^/]+)", relative)
    if match:
        artifact = ARTIFACTS.get(match.group(1))
        if artifact is None:
            raise api_error(HTTPStatus.NOT_FOUND, "artifact not found")
        if method == "GET":
            return {"artifact": dict(artifact)}
        if method == "DELETE":
            del ARTIFACTS[match.group(1)]
            _append_message("event", f'Removed artifact "{artifact["title"]}"', {"action": "delete_artifact"})
            return {"deleted": match.group(1)}
    match = re.fullmatch(r"schedules/([^/]+)/(enable|disable)", relative)
    if method == "POST" and match:
        schedule_id, action = match.groups()
        for schedule in SCHEDULES:
            if schedule["schedule_id"] == schedule_id:
                schedule["enabled"] = action == "enable"
                return {"schedule_id": schedule_id, "enabled": schedule["enabled"]}
        raise api_error(HTTPStatus.NOT_FOUND, "schedule not found")
    match = re.fullmatch(r"schedules/([^/]+)", relative)
    if method == "DELETE" and match:
        schedule_id = match.group(1)
        remaining = [schedule for schedule in SCHEDULES if schedule["schedule_id"] != schedule_id]
        if len(remaining) == len(SCHEDULES):
            raise api_error(HTTPStatus.NOT_FOUND, "schedule not found")
        SCHEDULES[:] = remaining
        return {"deleted": schedule_id}
    match = re.fullmatch(r"memories/([^/]+)", relative)
    if match:
        memory_id = match.group(1)
        memory = next((entry for entry in MEMORIES if entry["memory_id"] == memory_id), None)
        if memory is None:
            raise api_error(HTTPStatus.NOT_FOUND, "memory not found")
        if method == "POST":
            if not isinstance(body, dict) or set(body) != {"content"}:
                raise api_error(HTTPStatus.BAD_REQUEST, "memory update requires content")
            content = body["content"]
            if not isinstance(content, str) or not content.strip():
                raise api_error(HTTPStatus.BAD_REQUEST, "content must be a non-empty string")
            memory["content"] = content.strip()
            memory["updated_at"] = "2026-07-16T14:30:00Z"
            _append_message("event", f'Updated memory "{memory_id}"', {"action": "update_memory"})
            return {"memory_id": memory_id}
        if method == "DELETE":
            MEMORIES.remove(memory)
            _append_message("event", f'Removed memory "{memory_id}"', {"action": "delete_memory"})
            return {"deleted": memory_id}
    match = re.fullmatch(r"tools/([^/]+)", relative)
    if method == "DELETE" and match:
        tool_id = match.group(1)
        tool = next((entry for entry in TOOLS if entry["tool_id"] == tool_id), None)
        if tool is None:
            raise api_error(HTTPStatus.NOT_FOUND, "tool not found")
        TOOLS.remove(tool)
        _append_message("event", f'Removed tool "{tool["title"]}"', {"action": "delete_tool"})
        return {"deleted": tool_id}
    match = re.fullmatch(r"tasks/([^/]+)/stop", relative)
    if method == "POST" and match:
        return {"task_id": match.group(1), "was": "running"}
    raise api_error(HTTPStatus.NOT_FOUND, "mock app route not found")


def desktop_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    expect(page.locator("#sidebar-apps")).to_contain_text("Apps")
    page.locator("#app-tabs").get_by_role("button", name="Mission Pursuit", exact=True).click()
    expect(page.locator("#panel-app-mission_pursuit")).to_be_visible()
    frame = page.frame_locator('iframe[title="Mission Pursuit"]')

    # A fresh open lands on the activation gate: the app explains itself and
    # nothing exists until the operator activates the workspace.
    expect(frame.locator("#hero")).to_be_visible()
    expect(frame.locator(".hero-about-block")).to_have_count(3)
    frame.locator("#hero-send").click()
    expect(frame.locator("#workspace")).to_be_visible()

    expect(frame.locator(".app-frame-title")).to_have_text("Mission Pursuit")
    expect(frame.locator("#goal-banner")).to_contain_text("Get the launch tracker ready for Friday")
    expect(frame.locator("#agent-settings-toggle")).to_contain_text("Codex · gpt-5.6-terra · High")

    # Agent settings keep one visible conversation while changing the
    # immutable runtime/model/effort triple underneath it.
    workspace_top = frame.locator("#workspace").evaluate("element => element.getBoundingClientRect().top")
    frame.locator("#agent-settings-toggle").click()
    expect(frame.locator("#agent-settings-popover")).to_be_visible()
    workspace_top_with_settings = frame.locator("#workspace").evaluate(
        "element => element.getBoundingClientRect().top"
    )
    if abs(workspace_top_with_settings - workspace_top) > 1:
        raise AssertionError("opening agent settings shifted the workspace")
    expect(frame.locator("#agent-settings-warning")).to_be_hidden()
    # Activation defaulted to Codex, so switching runtimes means Claude Code.
    frame.locator("#agent-runtime").select_option("claude_code")
    expect(frame.locator("#agent-settings-warning")).to_be_visible()
    expect(frame.locator("#agent-settings-warning")).to_contain_text("short-term memory")
    frame.locator("#agent-model").select_option("opus")
    frame.locator("#agent-effort").select_option("max")
    frame.get_by_role("button", name="Apply changes", exact=True).click()
    expect(frame.locator("#agent-settings-toggle")).to_contain_text("Claude Code · Opus · Max")
    expect(frame.locator("#feed")).to_contain_text("Switched to Claude Code")

    # Feed renders every role: user, agent (with inline markup), event, error.
    expect(frame.locator("#feed")).to_contain_text("Set up a launch tracker")
    expect(frame.locator("#feed .msg.agent strong").first).to_have_text("Launch Tracker")
    expect(frame.locator("#feed .event-chip").first).to_contain_text('Created artifact "Launch Tracker"')
    expect(frame.locator("#feed .msg.error").first).to_contain_text("Action rejected")
    expect(frame.locator("#memories")).to_contain_text("launch_timezone")
    expect(frame.locator("#memories")).to_contain_text("plans and reports in UTC")
    expect(frame.locator("#tools")).to_contain_text("Product analytics")
    expect(frame.locator("#tools")).to_contain_text("not implemented")

    # Schedule row with immediate controls: pause, then resume. A second weekly
    # digest schedule is seeded (paused, last run failed), so the interaction is
    # scoped to the morning-check row.
    expect(frame.locator("#schedules")).to_contain_text("Morning check")
    expect(frame.locator("#schedules")).to_contain_text("every day")
    expect(frame.locator("#schedules")).to_contain_text("last completed")
    expect(frame.locator("#schedules")).to_contain_text("last failed")
    check_row = frame.locator(".rail-item.schedule", has_text="Morning check")
    check_row.get_by_role("button", name="Pause").click()
    expect(check_row).to_contain_text("paused")
    check_row.get_by_role("button", name="Resume").click()
    expect(check_row).not_to_contain_text("paused")

    # Artifact opens with natively rendered blocks, including the chart.
    frame.locator("#artifacts .rail-item", has_text="Launch Tracker").click()
    overlay = frame.locator("#artifact-overlay")
    expect(overlay).to_be_visible()
    expect(overlay.locator(".b-heading")).to_have_text("Launch Tracker")
    expect(overlay.locator(".b-callout")).to_contain_text("Release candidate by Friday")
    expect(overlay.locator(".metric-tile").first).to_contain_text("Tasks done")
    expect(overlay.locator(".artifact-card").first).to_contain_text("Ready for review")
    expect(overlay.locator(".b-details")).to_contain_text("Direct launch")
    expect(overlay.locator(".b-list li")).to_have_count(2)
    expect(overlay.locator(".b-timeline li.current")).to_contain_text("Release review")
    expect(overlay.locator(".kanban-column")).to_have_count(3)
    expect(overlay.locator(".kanban-column", has_text="Blocked")).to_contain_text("Empty")
    expect(overlay.locator(".b-chart svg")).to_be_visible()
    expect(overlay.locator(".b-chart figcaption")).to_have_text('Tasks "closed" <img src=x>')
    expect(overlay.locator("img")).to_have_count(0)
    chart_point_count = overlay.locator(".b-chart").evaluate(
        "figure => JSON.parse(figure.dataset.points).length"
    )
    if chart_point_count != 4:
        raise AssertionError(f"chart data attribute decoded {chart_point_count} points instead of 4")
    expect(overlay.locator(".b-checklist li.done")).to_contain_text("Draft announcement")
    expect(overlay.locator(".progress-value")).to_have_text("70%")
    expect(overlay.locator(".b-control-button")).to_have_text("Approve launch")
    expect(overlay.locator(".b-toggle-control")).to_contain_text("Daily updates")
    expect(overlay.locator(".b-field-control input")).to_have_value("Ready for review")
    expect(overlay.locator(".artifact-head #artifact-delete")).to_be_visible()
    expect(overlay.locator(".artifact-actions #artifact-delete")).to_have_count(0)

    # A background artifact update must not erase a focused, half-written
    # field. Trigger another control through the bridge while focus remains in
    # the field, then run the normal poll refresh.
    field = overlay.locator(".b-field-control input")
    field.fill("Half-written operator note")
    field.evaluate(
        """async element => {
          element.focus();
          await api("POST", "/interactions", {
            artifact_id: "launch_tracker",
            control_id: "launch_note",
            value: "Agent background update",
          });
          await refresh();
        }"""
    )
    expect(field).to_have_value("Half-written operator note")

    # Native controls send constrained structured human events. The mock then
    # simulates the agent applying ordinary typed artifact updates.
    overlay.locator(".b-control-button").click()
    expect(frame.locator("#feed")).to_contain_text("Pressed Approve launch")
    # Click the visible operating surface, not its visually hidden native
    # checkbox. The artifact refresh after the button resets the scroll pane,
    # and browsers cannot reliably scroll a 1px absolutely positioned input.
    overlay.locator(".b-toggle-control").click()
    expect(overlay.locator(".b-toggle-control input")).not_to_be_checked()
    expect(frame.locator("#feed")).to_contain_text("Daily updates: Off")
    overlay.locator(".b-field-control input").fill("Hold for final approval")
    overlay.locator(".b-field-control button").click()
    expect(frame.locator("#feed")).to_contain_text("Launch note: Hold for final approval")
    overlay.get_by_role("button", name="Close", exact=True).click()
    expect(overlay).to_be_hidden()

    # Data-only artifacts fall back to pretty-printed JSON.
    frame.locator("#artifacts .rail-item", has_text="Raw Notes").click()
    expect(overlay.locator(".artifact-json")).to_contain_text("data-only artifact")
    overlay.get_by_role("button", name="Close", exact=True).click()

    # Every shared view-block branch treats agent-authored markup as inert
    # text. The CSP separately denies inline script and event handlers, so a
    # future escaping regression still cannot execute the payload.
    hostile_view = ARTIFACTS["hostile_renderer"]["view"]
    assert hostile_view is not None
    hostile_types = {block["type"] for block in hostile_view}
    if hostile_types != set(workspace_views.VIEW_BLOCK_TYPES):
        raise AssertionError("hostile renderer fixture does not cover every supported view block")
    frame.locator("body").evaluate("() => { window.__x = 0; }")
    frame.locator("#artifacts .rail-item", has_text="Hostile Renderer Test").click()
    expect(overlay).to_be_visible()
    expect(overlay.locator(".artifact-body")).to_contain_text(HOSTILE_VIEW_TEXT)
    expect(overlay.locator("img, script, a, iframe, object, embed")).to_have_count(0)
    expect(overlay.locator("[onerror], [onload], [onclick]")).to_have_count(0)
    expect(overlay.locator(".b-control-button")).to_have_count(1)
    if frame.locator("body").evaluate("() => window.__x") != 0:
        raise AssertionError("agent-authored view content executed as inline script")
    overlay.get_by_role("button", name="Close", exact=True).click()

    # Sending a message round-trips through the app backend.
    frame.locator("#chat-input").fill("mission pursuit smoke message")
    frame.get_by_role("button", name="Send", exact=True).click()
    expect(frame.locator("#feed")).to_contain_text("mission pursuit smoke message")
    expect(frame.locator("#feed")).to_contain_text("Mock reply: noted")

    # The product explanation is anchored to its trigger and dismisses when
    # the operator clicks back into the workspace.
    info_trigger = frame.get_by_role("button", name="How it works")
    info_trigger.click()
    info_popover = frame.locator("#info-popover")
    expect(info_popover).to_be_visible()
    expect(info_popover).to_contain_text("Set the mission together")
    expect(info_popover).to_contain_text("Work through artifacts")
    trigger_box = info_trigger.bounding_box()
    popover_box = info_popover.bounding_box()
    if not trigger_box or not popover_box:
        raise AssertionError("How it works trigger or popover has no layout box")
    if popover_box["y"] < trigger_box["y"] + trigger_box["height"] - 1:
        raise AssertionError("How it works popover is not anchored beneath its trigger")
    frame.locator("#workspace").click(position={"x": 4, "y": 4})
    expect(info_popover).to_be_hidden()

    # Agent-authored artifact and message URLs remain inert text.
    expect(frame.locator("a")).to_have_count(0)
    _assert_single_scroll_desktop(page, frame, "Mission Pursuit app (desktop)")


def mobile_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    expect(page.locator("#sidebar-apps")).to_contain_text("Apps")
    page.locator("#mobile-nav-toggle").click()
    expect(page.locator("#nav-backdrop")).to_be_visible()
    page.locator("#app-tabs").get_by_role("button", name="Mission Pursuit", exact=True).click()
    expect(page.locator("#nav-backdrop")).to_be_hidden()
    expect(page.locator("#panel-app-mission_pursuit")).to_be_visible()
    app_frame = page.locator('iframe[title="Mission Pursuit"]')
    app_frame.scroll_into_view_if_needed()
    frame = app_frame.content_frame
    expect(frame.locator("#feed")).to_contain_text("Set up a launch tracker")
    frame.locator("#artifacts .rail-item", has_text="Launch Tracker").click()
    expect(frame.locator("#artifact-overlay .b-chart svg")).to_be_visible()
    frame.get_by_role("button", name="Close").click()
    _assert_frame_no_horizontal_overflow(frame, "Mission Pursuit app")
    _assert_outer_page_locked(page, "Mission Pursuit app (mobile)")
    page.once("dialog", lambda dialog: dialog.accept())
    frame.locator("#deactivate-app").click()
    expect(frame.locator("#hero")).to_be_visible()
    expect(frame.locator("#workspace")).to_be_hidden()
    expect(frame.locator("#hero-send")).to_contain_text("Reactivate")
    expect(frame.locator("#hero-hint")).to_contain_text("schedules stay paused")


def _assert_frame_no_horizontal_overflow(frame: Any, label: str) -> None:
    overflow = frame.locator("html").evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    if overflow > 1:
        raise AssertionError(f"{label} frame overflows horizontally by {overflow}px")


def _assert_single_scroll_desktop(page: Any, frame: Any, label: str) -> None:
    """With the app tab open on a wide viewport, neither the outer page nor
    the iframe document may scroll vertically -- only the app's internal
    panes do. Guards the nested page + iframe double-scrollbar regression."""
    outer = page.evaluate(
        "() => document.documentElement.scrollHeight - document.documentElement.clientHeight"
    )
    if outer > 1:
        raise AssertionError(f"{label}: outer page scrolls vertically by {outer}px with an app tab open")
    inner = frame.locator("html").evaluate(
        "() => document.documentElement.scrollHeight - document.documentElement.clientHeight"
    )
    if inner > 1:
        raise AssertionError(f"{label}: app iframe document scrolls vertically by {inner}px")


def _assert_outer_page_locked(page: Any, label: str) -> None:
    """On a phone the app document may scroll, but the host page must stay
    locked so there is only one scrollbar."""
    outer = page.evaluate(
        "() => document.documentElement.scrollHeight - document.documentElement.clientHeight"
    )
    if outer > 1:
        raise AssertionError(f"{label}: outer page scrolls vertically by {outer}px with an app tab open")
