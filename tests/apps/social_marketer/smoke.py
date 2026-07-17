"""Social Marketer smoke-test mock backend and UI checks.

Mirrors the workspace_kit operator surface plus this app's posts routes so the
admin UI smoke can drive the bespoke calendar, composer, and draft queue without
a live backend or Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus
import re
from typing import Any, Callable


ApiErrorFactory = Callable[[HTTPStatus, str], Exception]
HostApi = Callable[[str, str, dict[str, list[str]], Any], dict[str, Any]]

# Fixed literal timestamps carry the ~3-week conversation history. Calendar
# entries are positioned relative to the current month (the calendar defaults to
# it) so a full month of chips is always visible; that is the only wall-clock
# read, mirroring the pre-existing scheduled-chip pattern.
_BASE = "2026-06-08T00:0{}:00Z"
_TODAY = datetime.now(timezone.utc)
_MONTH = _TODAY.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
_SCHEDULED = _TODAY.replace(hour=15, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cal(day: int, hour: int) -> str:
    """A fixed hour on a given day of the calendar's current month."""
    return _MONTH.replace(day=day, hour=hour).strftime("%Y-%m-%dT%H:%M:%SZ")


PLATFORM_BODY_BYTES = {"x": 4000, "linkedin": 3000}

from host.session_options import public_session_options, session_config_error  # noqa: E402


WORKSPACE: dict[str, Any] = {
    "agent_runtime": "claude_code",
    "model": "opus",
    "effort": "high",
    "thread_seq": 1,
    "goal": "Grow Acme's launch audience across X and LinkedIn",
    "measurement": "Three posts a week per channel; +10% engagement by launch",
    "created_at": "2026-06-24T10:28:00Z",
}
MESSAGES: list[dict[str, Any]] = [
    {"id": 1, "role": "user", "content": "Plan launch week and draft the first posts.", "meta": None, "created_at": "2026-06-24T10:30:00Z"},
    {"id": 2, "role": "agent", "content": "Drafted the **launch teaser** for X and queued a LinkedIn post. Publishing stays approval-gated on every channel.", "meta": None, "created_at": "2026-06-24T10:33:00Z"},
    {"id": 3, "role": "event", "content": "Drafted x post launch-teaser", "meta": {"action": "upsert_post", "post_id": "launch-teaser"}, "created_at": "2026-06-24T10:33:00Z"},
    {"id": 4, "role": "user", "content": "Space the launch content across both channels for the month.", "meta": None, "created_at": "2026-06-27T09:00:00Z"},
    {"id": 5, "role": "agent", "content": "Built a month calendar: teasers and threads on X, with thought-leadership on LinkedIn.", "meta": None, "created_at": "2026-06-27T09:05:00Z"},
    {"id": 6, "role": "event", "content": 'Updated artifact "Launch campaign"', "meta": {"action": "update_artifact", "artifact_id": "campaign"}, "created_at": "2026-06-27T09:05:00Z"},
    {"id": 7, "role": "agent", "content": "Week 1 recap: the launch-day thread hit **3.2k** impressions and +180 followers. Logged engagement in the campaign artifact.", "meta": None, "created_at": "2026-07-06T16:00:00Z"},
    {"id": 8, "role": "event", "content": 'Updated artifact "Launch campaign"', "meta": {"action": "update_artifact", "artifact_id": "campaign"}, "created_at": "2026-07-06T16:00:00Z"},
    {"id": 9, "role": "user", "content": "Approve the founder note but hold the launch thread for review.", "meta": None, "created_at": "2026-07-10T11:00:00Z"},
    {"id": 10, "role": "agent", "content": "Approved the founder note for LinkedIn; kept the thread in *draft* for review.", "meta": None, "created_at": "2026-07-10T11:03:00Z"},
    {"id": 11, "role": "event", "content": "Updated linkedin post founder-linkedin", "meta": {"action": "upsert_post", "post_id": "founder-linkedin"}, "created_at": "2026-07-10T11:03:00Z"},
    {"id": 12, "role": "error", "content": "Action rejected: X post exceeds the 4000-byte limit for the platform", "meta": {"action": "upsert_post"}, "created_at": "2026-07-13T13:20:00Z"},
    {"id": 13, "role": "agent", "content": "Trimmed the thread under the limit and re-drafted it. Ready for your review.", "meta": None, "created_at": "2026-07-16T09:15:00Z"},
]
SCHEDULES: list[dict[str, Any]] = [
    {
        "schedule_id": "campaign_planning",
        "title": "Weekly campaign planning",
        "every_minutes": 10080,
        "next_run_at": "2026-07-20T14:00:00Z",
        "enabled": True,
        "created_at": "2026-06-24T10:33:00Z",
        "updated_at": "2026-06-24T10:33:00Z",
        "last_run_status": "completed",
        "last_run_at": "2026-07-13T14:00:00Z",
    },
    {
        "schedule_id": "daily_engagement_pull",
        "title": "Daily engagement pull",
        "every_minutes": 1440,
        "next_run_at": "2026-07-17T07:00:00Z",
        "enabled": True,
        "created_at": "2026-06-27T07:00:00Z",
        "updated_at": "2026-06-27T07:00:00Z",
        "last_run_status": "failed",
        "last_run_at": "2026-07-16T07:00:00Z",
    },
]
ARTIFACTS: dict[str, dict[str, Any]] = {
    "campaign": {
        "artifact_id": "campaign",
        "title": "Launch campaign",
        "data": {"channels": ["x", "linkedin"]},
        "view": [
            {"type": "heading", "text": "Launch campaign", "level": 1},
            {"type": "metrics", "items": [
                {"label": "Posts planned", "value": "9", "delta": "+3"},
                {"label": "Posted", "value": "4"},
                {"label": "Channels", "value": "2"},
                {"label": "Wk1 engagement", "value": "+14%", "delta": "+4pts"},
            ]},
            {"type": "chart", "kind": "bar", "label": "Posts per channel", "points": [
                {"label": "X", "value": 5}, {"label": "LinkedIn", "value": 4},
            ]},
            {"type": "heading", "text": "Recent engagement", "level": 3},
            {"type": "table", "columns": ["Post", "Channel", "Impressions", "Engagement"], "rows": [
                ["Launch-day thread", "X", "3,240", "4.9%"],
                ["Onboarding teardown", "LinkedIn", "1,910", "6.1%"],
                ["Founder lesson", "LinkedIn", "2,470", "5.4%"],
                ["What should we build poll", "X", "2,880", "7.2%"],
            ]},
            {"type": "callout", "title": "Learning", "text": "Threads posted **08:00-10:00 local** outperform afternoon posts by ~40% on X.", "tone": "success"},
        ],
        "created_at": "2026-06-24T10:33:00Z",
        "updated_at": "2026-07-06T16:00:00Z",
    },
}
MEMORIES: list[dict[str, Any]] = [
    {"memory_id": "brand_voice", "content": "Confident, concise, never salesy; lead with the customer outcome, not the feature.", "updated_at": "2026-06-24T10:33:00Z"},
    {"memory_id": "posting_windows", "content": "Best windows: X 08:00-10:00 local and LinkedIn Tue/Thu mornings.", "updated_at": "2026-07-06T16:00:00Z"},
    {"memory_id": "approvals", "content": "Every publish waits for the operator's approval; never post without an explicit go.", "updated_at": "2026-06-27T09:05:00Z"},
]
TOOLS: list[dict[str, Any]] = [
    {"tool_id": "twitter", "title": "X (Twitter)", "priority": "must_have", "status": "enabled", "note": "Publishing is approval-gated.", "updated_at": "2026-07-16T09:15:00Z"},
    {"tool_id": "linkedin", "title": "LinkedIn", "priority": "must_have", "status": "enabled", "note": "Approval-gated publishing for pages and profiles.", "updated_at": "2026-07-10T11:03:00Z"},
    {"tool_id": "linkedin_discovery", "title": "LinkedIn Discovery", "priority": "good_to_have", "status": "implemented", "note": "Public post research.", "updated_at": "2026-07-06T16:00:00Z"},
]
POSTS: list[dict[str, Any]] = [
    {"id": "launch-teaser", "platform": "x", "body": "Something big is coming Friday.", "status": "draft", "scheduled_for": _SCHEDULED, "external_ref": None, "created_at": "2026-06-24T10:33:00Z"},
    {"id": "launch-linkedin", "platform": "linkedin", "body": "We are launching next week. Here is why it matters.", "status": "approved", "scheduled_for": None, "external_ref": None, "created_at": "2026-06-24T10:33:00Z"},
    {"id": "launch-day-thread", "platform": "x", "body": "It's live. Here's the story of how we built Acme in 90 days, in 8 posts.", "status": "posted", "scheduled_for": _cal(5, 9), "external_ref": "x:1892034", "created_at": "2026-06-27T09:05:00Z"},
    {"id": "onboarding-teardown", "platform": "linkedin", "body": "How we cut onboarding time 40%: three decisions and one thing we got wrong.", "status": "posted", "scheduled_for": _cal(7, 8), "external_ref": "li:88213", "created_at": "2026-06-27T09:05:00Z"},
    {"id": "founder-lesson", "platform": "linkedin", "body": "Behind the build: five lessons from the launch.", "status": "posted", "scheduled_for": _cal(9, 9), "external_ref": "li:41220", "created_at": "2026-06-27T09:05:00Z"},
    {"id": "build-poll", "platform": "x", "body": "Poll: what should we ship next quarter?", "status": "posted", "scheduled_for": _cal(11, 15), "external_ref": "x:1893551", "created_at": "2026-06-27T09:05:00Z"},
    {"id": "founder-linkedin", "platform": "linkedin", "body": "Founder note: why we built Acme, and who it is really for.", "status": "approved", "scheduled_for": _cal(20, 8), "external_ref": None, "created_at": "2026-07-01T09:00:00Z"},
    {"id": "tips-thread", "platform": "x", "body": "Thread: 6 things we learned shipping v1 (that the roadmap didn't warn us about).", "status": "draft", "scheduled_for": _cal(22, 9), "external_ref": None, "created_at": "2026-07-13T13:25:00Z"},
    {"id": "launch-recap", "platform": "x", "body": "Launch day recap: what worked and what surprised us.", "status": "draft", "scheduled_for": _cal(24, 10), "external_ref": None, "created_at": "2026-07-10T11:03:00Z"},
    {"id": "case-study-linkedin", "platform": "linkedin", "body": "Customer story: from spreadsheet chaos to one dashboard.", "status": "draft", "scheduled_for": None, "external_ref": None, "created_at": "2026-07-14T10:00:00Z"},
]
_NEXT_MESSAGE_ID = [len(MESSAGES) + 1]
_NEXT_POST_ID = [1]


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


def _post_view(post: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": post["id"],
        "platform": post["platform"],
        "body": post["body"],
        "truncated": False,
        "body_bytes": len(post["body"].encode("utf-8")),
        "status": post["status"],
        "scheduled_for": post["scheduled_for"],
        "external_ref": post["external_ref"],
        "created_at": post["created_at"],
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
        return {"status": "ok", "app": "social_marketer", "mock_backend": True}
    if method == "GET" and relative == "connections":
        return {
            "status": "degraded",
            "providers": [
                {"provider": "openai", "agent_runtime": "codex", "enabled": True},
                {"provider": "claude", "agent_runtime": "claude_code", "enabled": True},
            ],
            "tools": [
                {"tool_id": "twitter", "title": "X (Twitter)", "priority": "must_have", "state": "ready", "detail": ""},
                {"tool_id": "linkedin", "title": "LinkedIn", "priority": "must_have", "state": "ready", "detail": ""},
                {"tool_id": "linkedin_discovery", "title": "LinkedIn Discovery", "priority": "good_to_have", "state": "off", "detail": "enable it in Internet Access and Tools"},
            ],
        }
    if method == "GET" and relative == "workspace":
        return _snapshot()
    if method == "GET" and relative == "session-options":
        return {
            "session_options": public_session_options(),
            "active_runtimes": ["codex", "claude_code"],
        }
    if method == "GET" and relative == "api/posts":
        return {"posts": [_post_view(post) for post in POSTS]}
    if method == "POST" and relative == "api/posts":
        return _upsert_post(body, api_error)
    match = re.fullmatch(r"api/posts/([^/]+)", relative)
    if match:
        post = next((entry for entry in POSTS if entry["id"] == match.group(1)), None)
        if post is None:
            raise api_error(HTTPStatus.NOT_FOUND, "post not found")
        if method == "GET":
            return {"post": _post_view(post)}
        if method == "DELETE":
            POSTS.remove(post)
            _append_message("event", f"Removed {post['platform']} post {post['id']}", {"action": "delete_post"})
            return {"deleted": post["id"]}
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
        _append_message("event", f'Switched to {runtime_label} · {body["model"]} · {str(body["effort"]).title()}', {"action": "update_agent_settings"})
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
        remaining = [schedule for schedule in SCHEDULES if schedule["schedule_id"] != match.group(1)]
        if len(remaining) == len(SCHEDULES):
            raise api_error(HTTPStatus.NOT_FOUND, "schedule not found")
        SCHEDULES[:] = remaining
        return {"deleted": match.group(1)}
    match = re.fullmatch(r"memories/([^/]+)", relative)
    if match:
        memory = next((entry for entry in MEMORIES if entry["memory_id"] == match.group(1)), None)
        if memory is None:
            raise api_error(HTTPStatus.NOT_FOUND, "memory not found")
        if method == "POST":
            if not isinstance(body, dict) or not isinstance(body.get("content"), str) or not body["content"].strip():
                raise api_error(HTTPStatus.BAD_REQUEST, "content must be a non-empty string")
            memory["content"] = body["content"].strip()
            return {"memory_id": memory["memory_id"]}
        if method == "DELETE":
            MEMORIES.remove(memory)
            return {"deleted": memory["memory_id"]}
    match = re.fullmatch(r"tools/([^/]+)", relative)
    if method == "DELETE" and match:
        tool = next((entry for entry in TOOLS if entry["tool_id"] == match.group(1)), None)
        if tool is None:
            raise api_error(HTTPStatus.NOT_FOUND, "tool not found")
        TOOLS.remove(tool)
        return {"deleted": tool["tool_id"]}
    match = re.fullmatch(r"tasks/([^/]+)/stop", relative)
    if method == "POST" and match:
        return {"task_id": match.group(1), "was": "running"}
    raise api_error(HTTPStatus.NOT_FOUND, "mock app route not found")


def _upsert_post(body: Any, api_error: ApiErrorFactory) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise api_error(HTTPStatus.UNPROCESSABLE_ENTITY, "post body must be an object")
    platform = body.get("platform")
    text = body.get("body")
    if platform not in PLATFORM_BODY_BYTES:
        raise api_error(HTTPStatus.UNPROCESSABLE_ENTITY, "platform must be one of: x, linkedin")
    if not isinstance(text, str) or not text.strip():
        raise api_error(HTTPStatus.UNPROCESSABLE_ENTITY, "body must be a non-empty string")
    if len(text.encode("utf-8")) > PLATFORM_BODY_BYTES[platform]:
        raise api_error(HTTPStatus.UNPROCESSABLE_ENTITY, "body too long for platform")
    post_id = body.get("id")
    scheduled = body.get("scheduled_for") or None
    existing = next((entry for entry in POSTS if entry["id"] == post_id), None) if isinstance(post_id, str) else None
    if existing is not None:
        if existing["status"] != "draft":
            raise api_error(HTTPStatus.UNPROCESSABLE_ENTITY, "only drafts can be edited")
        existing.update({"platform": platform, "body": text.strip(), "scheduled_for": scheduled})
        _append_message("event", f"Updated {platform} post {existing['id']}", {"action": "upsert_post"})
        return {"post_id": existing["id"]}
    new_id = post_id if isinstance(post_id, str) and post_id else f"post-mock{_NEXT_POST_ID[0]}"
    _NEXT_POST_ID[0] += 1
    POSTS.append(
        {"id": new_id, "platform": platform, "body": text.strip(), "status": "draft", "scheduled_for": scheduled, "external_ref": None, "created_at": "2026-07-16T14:30:00Z"}
    )
    _append_message("event", f"Drafted {platform} post {new_id}", {"action": "upsert_post"})
    return {"post_id": new_id}


def desktop_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    expect(page.locator("#sidebar-apps")).to_contain_text("Apps")
    page.locator("#app-tabs").get_by_role("button", name="Social Marketer", exact=True).click()
    expect(page.locator("#panel-app-social_marketer")).to_be_visible()
    frame = page.frame_locator('iframe[title="Social Marketer"]')

    # A fresh open lands on the activation gate: the app explains itself and
    # nothing exists until the operator activates the workspace.
    expect(frame.locator("#hero")).to_be_visible()
    expect(frame.locator(".hero-about-block")).to_have_count(3)
    frame.locator("#hero-send").click()
    expect(frame.locator("#workspace")).to_be_visible()

    expect(frame.locator(".app-frame-title")).to_have_text("Social Marketer")
    expect(frame.locator("#goal-banner")).to_contain_text("Grow Acme's launch audience")
    expect(frame.locator("#agent-settings-toggle")).to_contain_text("Codex · gpt-5.6-terra · High")

    # Feed renders user, agent (with inline markup), event, and error roles.
    expect(frame.locator("#feed")).to_contain_text("Plan launch week")
    expect(frame.locator("#feed .msg.agent strong").first).to_have_text("launch teaser")
    expect(frame.locator("#feed .msg.error")).to_contain_text("exceeds the 4000-byte limit")

    # Calendar renders a month grid with a spread of scheduled chips across
    # platforms; the launch teaser lands on today.
    expect(frame.locator("#calendar .cal-weekday")).to_have_count(7)
    expect(frame.locator("#calendar .cal-chip.plat-x", has_text="Something big")).to_have_count(1)
    expect(frame.locator("#calendar .cal-chip.plat-linkedin").first).to_be_visible()

    # Draft queue groups posts by status.
    expect(frame.locator("#queue")).to_contain_text("Draft")
    expect(frame.locator("#queue")).to_contain_text("Approved")
    expect(frame.locator("#queue")).to_contain_text("We are launching next week")
    approved_group = frame.locator(".queue-group", has_text="Approved")
    expect(approved_group.get_by_role("button", name="Edit")).to_have_count(0)
    expect(approved_group.get_by_role("button", name="Delete")).to_have_count(0)
    expect(frame.locator('[data-edit-post="founder-linkedin"]')).to_have_count(0)

    # Composer: byte counter tracks the body, then a saved draft appears.
    frame.locator("#post-platform").select_option("linkedin")
    frame.locator("#post-body").fill("Fresh LinkedIn announcement for the smoke run.")
    expect(frame.locator("#byte-counter")).to_contain_text("/ 3000 bytes")
    frame.get_by_role("button", name="Save draft", exact=True).click()
    expect(frame.locator("#queue")).to_contain_text("Fresh LinkedIn announcement")

    # Campaign artifact opens with a natively rendered chart.
    frame.locator("#artifacts .rail-item", has_text="Launch campaign").click()
    overlay = frame.locator("#artifact-overlay")
    expect(overlay).to_be_visible()
    expect(overlay.locator(".b-heading").first).to_have_text("Launch campaign")
    expect(overlay.locator(".b-chart svg")).to_be_visible()
    overlay.get_by_role("button", name="Close", exact=True).click()
    expect(overlay).to_be_hidden()

    # Sending a message round-trips through the app backend.
    frame.locator("#chat-input").fill("social marketer smoke message")
    frame.get_by_role("button", name="Send", exact=True).click()
    expect(frame.locator("#feed")).to_contain_text("social marketer smoke message")
    expect(frame.locator("#feed")).to_contain_text("Mock reply: noted")

    expect(frame.locator("#connections-pill")).to_have_text("Connections degraded")
    # Agent-authored post bodies and references remain inert text.
    expect(frame.locator("a")).to_have_count(0)
    _assert_single_scroll_desktop(page, frame, "Social Marketer app (desktop)")


def mobile_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    expect(page.locator("#sidebar-apps")).to_contain_text("Apps")
    page.locator("#mobile-nav-toggle").click()
    expect(page.locator("#nav-backdrop")).to_be_visible()
    page.locator("#app-tabs").get_by_role("button", name="Social Marketer", exact=True).click()
    expect(page.locator("#nav-backdrop")).to_be_hidden()
    expect(page.locator("#panel-app-social_marketer")).to_be_visible()
    frame = page.frame_locator('iframe[title="Social Marketer"]')
    expect(frame.locator("#feed")).to_contain_text("Plan launch week")
    expect(frame.locator("#calendar .cal-weekday")).to_have_count(7)
    _assert_frame_no_horizontal_overflow(frame, "Social Marketer app")
    _assert_outer_page_locked(page, "Social Marketer app (mobile)")
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
        raise AssertionError(f"{label} overflows horizontally by {overflow}px")


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
