"""Software Builder app smoke-test mock backend and UI checks.

The mock serves a repository change with one active pull-request artifact,
review state, verification, GitHub connection health, and optional research.
Desktop and mobile smokes exercise the PR lifecycle and artifact view.
"""

from __future__ import annotations

from http import HTTPStatus
import re
from typing import Any, Callable

from host.session_options import public_session_options, session_config_error


ApiErrorFactory = Callable[[HTTPStatus, str], Exception]
HostApi = Callable[[str, str, dict[str, list[str]], Any], dict[str, Any]]

# Fixed literal timestamps spanning ~3 weeks of a mid-build session; the most
# recent entries land within the last day (today is 2026-07-16).
_BASE = "2026-06-08T00:0{}:00Z"

WORKSPACE: dict[str, Any] = {
    "agent_runtime": "claude_code",
    "model": "opus",
    "effort": "high",
    "thread_seq": 1,
    "goal": "Turn repository requests into focused, reviewed pull requests on connected GitHub repositories",
    "measurement": "Each pull request is tested, review-complete, and ready for the operator to merge",
    "created_at": "2026-06-28T10:58:00Z",
}
MESSAGES: list[dict[str, Any]] = [
    {"id": 1, "role": "user", "content": "Fix the parser regression in infiloop2/trustyclaw and open a PR.", "meta": None, "created_at": "2026-06-28T11:00:00Z"},
    {"id": 2, "role": "agent", "content": "Reproduced the **parser regression**, implemented the focused fix, and opened PR #142.", "meta": None, "created_at": "2026-06-28T11:04:00Z"},
    {"id": 3, "role": "event", "content": 'Created artifact "trustyclaw#142 parser regression"', "meta": {"action": "create_artifact", "artifact_id": "trustyclaw_pr_142"}, "created_at": "2026-06-28T11:04:00Z"},
    {"id": 4, "role": "event", "content": "Recorded Brave Search in the tools inventory for research (enable it to use it)", "meta": {"action": "upsert_tool", "tool_id": "brave_search"}, "created_at": "2026-06-28T11:05:00Z"},
    {"id": 5, "role": "agent", "content": "Local unit and type checks pass. GitHub CI is green; one review thread remains.", "meta": None, "created_at": "2026-07-01T10:00:00Z"},
    {"id": 6, "role": "event", "content": 'Updated artifact "trustyclaw#142 parser regression"', "meta": {"action": "update_artifact", "artifact_id": "trustyclaw_pr_142"}, "created_at": "2026-07-01T10:00:00Z"},
    {"id": 7, "role": "user", "content": "Address the review and make sure nothing is stale.", "meta": None, "created_at": "2026-07-04T15:00:00Z"},
    {"id": 8, "role": "agent", "content": "Pushed the review fix, replied inline, and completed a clean full-diff sweep. PR #142 is ready for your merge decision.", "meta": None, "created_at": "2026-07-16T10:00:00Z"},
]
SCHEDULES: list[dict[str, Any]] = []
ARTIFACTS: dict[str, dict[str, Any]] = {
    "trustyclaw_pr_142": {
        "artifact_id": "trustyclaw_pr_142",
        "title": "trustyclaw#142 parser regression",
        "data": {"repository": "infiloop2/trustyclaw", "pr": 142, "state": "ready"},
        "view": [
            {"type": "heading", "text": "PR #142 · parser regression", "level": 1},
            {"type": "details", "items": [
                {"label": "Repository", "value": "infiloop2/trustyclaw"},
                {"label": "Branch", "value": "software-builder/parser-regression"},
                {"label": "Pull request", "value": "#142"},
                {"label": "Checks", "value": "Green"},
                {"label": "Review", "value": "All comments replied"},
            ]},
            {"type": "checklist", "items": [
                {"text": "Focused implementation and tests committed", "done": True},
                {"text": "Relevant local verification passes", "done": True},
                {"text": "GitHub checks are green", "done": True},
                {"text": "Every review comment has a reply", "done": True},
                {"text": "Operator has made the merge decision", "done": False},
            ]},
        ],
        "created_at": "2026-06-28T11:04:00Z",
        "updated_at": "2026-07-16T10:00:00Z",
    },
}
MEMORIES: list[dict[str, Any]] = [
    {
        "memory_id": "repository_rules",
        "content": "infiloop2/trustyclaw requires focused branches, local unit checks, and replies to every review comment.",
        "updated_at": "2026-07-04T15:02:00Z",
    },
    {
        "memory_id": "merge_boundary",
        "content": "The agent prepares a green, review-complete pull request; the operator decides whether to merge.",
        "updated_at": "2026-06-28T11:04:00Z",
    },
]
TOOLS: list[dict[str, Any]] = [
    {
        "tool_id": "brave_search",
        "title": "Brave Search",
        "priority": "good_to_have",
        "status": "implemented",
        "note": "Optional current technical documentation (action brave_search_search_web).",
        "updated_at": "2026-06-28T11:05:00Z",
    },
    {
        "tool_id": "github",
        "title": "GitHub",
        "priority": "must_have",
        "status": "enabled",
        "note": "Read repositories, push focused branches, and create or update pull requests.",
        "updated_at": "2026-07-04T15:02:00Z",
    },
]
_NEXT_MESSAGE_ID = [len(MESSAGES) + 1]


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
        return {"status": "ok", "app": "software_builder", "mock_backend": True}
    if method == "GET" and relative == "connections":
        return {
            "status": "degraded",
            "providers": [
                {"provider": "openai", "agent_runtime": "codex", "enabled": True},
                {"provider": "claude", "agent_runtime": "claude_code", "enabled": True},
            ],
            "tools": [
                {"tool_id": "brave_search", "title": "Brave Search", "priority": "good_to_have", "state": "off", "detail": "enable it in Internet Access and Tools"},
                {"tool_id": "github_credential", "title": "GitHub", "priority": "must_have", "state": "ready", "detail": ""},
            ],
        }
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
    match = re.fullmatch(r"tools/([^/]+)", relative)
    if method == "DELETE" and match:
        tool_id = match.group(1)
        tool = next((entry for entry in TOOLS if entry["tool_id"] == tool_id), None)
        if tool is None:
            raise api_error(HTTPStatus.NOT_FOUND, "tool not found")
        TOOLS.remove(tool)
        _append_message("event", f'Removed tool "{tool["title"]}"', {"action": "delete_tool"})
        return {"deleted": tool_id}
    match = re.fullmatch(r"memories/([^/]+)", relative)
    if match:
        memory_id = match.group(1)
        memory = next((entry for entry in MEMORIES if entry["memory_id"] == memory_id), None)
        if memory is None:
            raise api_error(HTTPStatus.NOT_FOUND, "memory not found")
        if method == "DELETE":
            MEMORIES.remove(memory)
            _append_message("event", f'Removed memory "{memory_id}"', {"action": "delete_memory"})
            return {"deleted": memory_id}
    raise api_error(HTTPStatus.NOT_FOUND, "mock app route not found")


def desktop_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    expect(page.locator("#sidebar-apps")).to_contain_text("Apps")
    page.locator("#app-tabs").get_by_role("button", name="Software Builder", exact=True).click()
    expect(page.locator("#panel-app-software_builder")).to_be_visible()
    frame = page.frame_locator('iframe[title="Software Builder"]')

    # A fresh open lands on the activation gate: the app explains itself and
    # nothing exists until the operator activates the workspace.
    expect(frame.locator("#hero")).to_be_visible()
    expect(frame.locator(".hero-about-block")).to_have_count(3)
    frame.locator("#hero-send").click()
    expect(frame.locator("#workspace")).to_be_visible()

    expect(frame.locator(".app-frame-title")).to_have_text("Software Builder")
    expect(frame.locator("#goal-banner")).to_contain_text("focused, reviewed pull requests")
    expect(frame.locator("#agent-settings-toggle")).to_contain_text("Codex · gpt-5.6-terra · High")

    expect(frame.locator(".ready-panel")).to_contain_text("You keep the merge decision")
    expect(frame.locator("#pr-workflow .workflow-step")).to_have_count(6)
    expect(frame.locator("#pr-workflow")).to_contain_text("Request")
    expect(frame.locator("#pr-workflow")).to_contain_text("Review")

    # The feed renders every role, including the seeded Brave Search event.
    expect(frame.locator("#feed")).to_contain_text("Fix the parser regression")
    expect(frame.locator("#feed .msg.agent strong", has_text="parser regression")).to_contain_text("parser regression")
    expect(frame.locator("#feed .event-chip").first).to_contain_text('Created artifact "trustyclaw#142 parser regression"')
    expect(frame.locator("#tools")).to_contain_text("Brave Search")
    expect(frame.locator("#tools")).to_contain_text("good to have")
    expect(frame.locator("#memories")).to_contain_text("repository_rules")

    # Software Builder is interactive: no schedules are seeded.
    expect(frame.locator("#schedules")).to_contain_text("No follow-ups scheduled")

    frame.locator("#artifacts .rail-item", has_text="trustyclaw#142").click()
    overlay = frame.locator("#artifact-overlay")
    expect(overlay).to_be_visible()
    expect(overlay.locator(".b-details")).to_contain_text("infiloop2/trustyclaw")
    expect(overlay.locator(".b-checklist li.done").first).to_contain_text("Focused implementation")
    expect(overlay.locator(".b-checklist li:not(.done)").first).to_contain_text("merge decision")
    overlay.get_by_role("button", name="Close", exact=True).click()

    # Sending a message round-trips through the app backend.
    frame.locator("#chat-input").fill("software builder smoke message")
    frame.get_by_role("button", name="Send", exact=True).click()
    expect(frame.locator("#feed")).to_contain_text("software builder smoke message")
    expect(frame.locator("#feed")).to_contain_text("Mock reply: noted")

    # The product explanation states the PR and merge boundary.
    frame.get_by_role("button", name="How it works").click()
    info_popover = frame.locator("#info-popover")
    expect(info_popover).to_be_visible()
    expect(info_popover).to_contain_text("Follow the pull request")
    expect(info_popover).to_contain_text("final merge decision")
    frame.locator("#workspace").click(position={"x": 4, "y": 4})
    expect(info_popover).to_be_hidden()

    expect(frame.locator("#connections-pill")).to_have_text("Connections degraded")
    # Agent-authored repository and PR URLs remain inert text.
    expect(frame.locator("a")).to_have_count(0)
    _assert_single_scroll_desktop(page, frame, "Software Builder app (desktop)")


def mobile_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    expect(page.locator("#sidebar-apps")).to_contain_text("Apps")
    page.locator("#mobile-nav-toggle").click()
    expect(page.locator("#nav-backdrop")).to_be_visible()
    page.locator("#app-tabs").get_by_role("button", name="Software Builder", exact=True).click()
    expect(page.locator("#nav-backdrop")).to_be_hidden()
    expect(page.locator("#panel-app-software_builder")).to_be_visible()
    app_frame = page.locator('iframe[title="Software Builder"]')
    app_frame.scroll_into_view_if_needed()
    frame = app_frame.content_frame
    expect(frame.locator("#feed")).to_contain_text("Fix the parser regression")
    expect(frame.locator("#pr-workflow")).to_contain_text("checks and comments resolved")
    frame.locator("#artifacts .rail-item", has_text="trustyclaw#142").click()
    expect(frame.locator("#artifact-overlay")).to_contain_text("GitHub checks are green")
    frame.get_by_role("button", name="Close").click()
    _assert_frame_no_horizontal_overflow(frame, "Software Builder app")
    _assert_outer_page_locked(page, "Software Builder app (mobile)")
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
