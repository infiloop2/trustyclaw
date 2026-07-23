"""Virality Machine smoke-test mock backend and UI checks.

The mock backend answers the same routes the real backend serves (the generic
workspace surface plus the domain GET /render_jobs read) with fixed fixtures, so
the admin UI smoke can drive the bespoke storyboard, render-queue, and
publish-queue panels without a live Runway or Instagram account.
"""

from __future__ import annotations

from http import HTTPStatus
import re
from typing import Any, Callable

from host.session_options import public_session_options, session_config_error


ApiErrorFactory = Callable[[HTTPStatus, str], Exception]
HostApi = Callable[[str, str, dict[str, list[str]], Any], dict[str, Any]]

# Fixed literal timestamps spanning ~3 weeks of genuine use; the most recent
# entries land within the last day (today is 2026-07-16). No wall-clock reads.
_BASE = "2026-06-08T00:0{}:00Z"

WORKSPACE: dict[str, Any] = {
    "agent_runtime": "claude_code",
    "model": "opus",
    "effort": "high",
    "thread_seq": 1,
    "goal": "Publish three fitness Reels a week",
    "measurement": "Published Reels per week",
    "created_at": "2026-06-26T09:58:00Z",
}
MESSAGES: list[dict[str, Any]] = [
    {"id": 1, "role": "user", "content": "Make short fitness reels and post the best ones.", "meta": None, "created_at": "2026-06-26T10:00:00Z"},
    {"id": 2, "role": "agent", "content": "On it. I drafted a **storyboard** and started the first render. Nothing publishes without your approval.", "meta": None, "created_at": "2026-06-26T10:03:00Z"},
    {"id": 3, "role": "event", "content": 'Created artifact "Storyboard"', "meta": {"action": "create_artifact", "artifact_id": "storyboard"}, "created_at": "2026-06-26T10:03:00Z"},
    {"id": 4, "role": "event", "content": 'Recorded render job "shot_1" (video, running)', "meta": {"action": "upsert_render_job", "id": "shot_1"}, "created_at": "2026-06-26T10:04:00Z"},
    {"id": 5, "role": "agent", "content": "Two shots came back. **shot_2** (sunrise run) is ready, so I staged the Morning Grind reel in the publish queue for your approval.", "meta": None, "created_at": "2026-06-30T14:20:00Z"},
    {"id": 6, "role": "event", "content": 'Recorded render job "shot_2" (video, succeeded)', "meta": {"action": "upsert_render_job", "id": "shot_2"}, "created_at": "2026-06-30T14:20:00Z"},
    {"id": 7, "role": "event", "content": 'Updated artifact "Publish queue"', "meta": {"action": "update_artifact", "artifact_id": "publish_queue"}, "created_at": "2026-06-30T14:21:00Z"},
    {"id": 8, "role": "user", "content": "Post the Morning Grind reel Friday at 9am if it looks good.", "meta": None, "created_at": "2026-07-03T09:10:00Z"},
    {"id": 9, "role": "agent", "content": "Marked it *approved* and scheduled for Friday 09:00. Instagram publishing is always approval-gated, so it will stage and wait for the final go.", "meta": None, "created_at": "2026-07-03T09:12:00Z"},
    {"id": 10, "role": "error", "content": "Render failed: Runway rejected shot_4 because the prompt implied a real public figure's likeness. Rewriting it as a generic silhouette.", "meta": {"action": "upsert_render_job", "id": "shot_4"}, "created_at": "2026-07-08T16:40:00Z"},
    {"id": 11, "role": "agent", "content": "Re-queued shot_4 without the likeness and refreshed the **ideas** list with three fresh hooks.", "meta": None, "created_at": "2026-07-10T11:05:00Z"},
    {"id": 12, "role": "event", "content": 'Updated artifact "Ideas"', "meta": {"action": "update_artifact", "artifact_id": "ideas"}, "created_at": "2026-07-10T11:05:00Z"},
    {"id": 13, "role": "agent", "content": "Daily trend scan: 'quiet-luxury gym' is spiking. Drafted a hook and **shot_5** rendered clean, ready to stage.", "meta": None, "created_at": "2026-07-16T08:30:00Z"},
    {"id": 14, "role": "event", "content": 'Recorded render job "shot_5" (video, succeeded)', "meta": {"action": "upsert_render_job", "id": "shot_5"}, "created_at": "2026-07-16T08:30:00Z"},
]
SCHEDULES: list[dict[str, Any]] = [
    {
        "schedule_id": "trend_scan",
        "title": "Daily trend scan",
        "every_minutes": 1440,
        "next_run_at": "2026-07-17T13:00:00Z",
        "enabled": True,
        "created_at": "2026-06-26T10:04:00Z",
        "updated_at": "2026-06-26T10:04:00Z",
        "last_run_status": "completed",
        "last_run_at": "2026-07-16T08:30:00Z",
    },
    {
        "schedule_id": "weekly_recap",
        "title": "Weekly performance recap",
        "every_minutes": 10080,
        "next_run_at": "2026-07-20T15:00:00Z",
        "enabled": False,
        "created_at": "2026-07-01T15:00:00Z",
        "updated_at": "2026-07-14T15:00:00Z",
        "last_run_status": "skipped",
        "last_run_at": "2026-07-13T15:00:00Z",
    },
]
RENDER_JOBS: list[dict[str, Any]] = [
    {
        "id": "shot_5",
        "task_id": "runway_task_eee",
        "kind": "video",
        "prompt": "Cooldown stretch on a mat, golden-hour window light, portrait",
        "prompt_truncated": False,
        "status": "succeeded",
        "output_url": "https://cdn.example.com/reels/shot_5.mp4",
        "created_at": "2026-07-16T08:22:00Z",
        "updated_at": "2026-07-16T08:30:00Z",
    },
    {
        "id": "shot_4",
        "task_id": "runway_task_ddd",
        "kind": "video",
        "prompt": "Gym mirror flex, generic silhouette, high-contrast portrait",
        "prompt_truncated": False,
        "status": "queued",
        "output_url": None,
        "created_at": "2026-07-10T11:04:00Z",
        "updated_at": "2026-07-10T11:05:00Z",
    },
    {
        "id": "shot_3",
        "task_id": "runway_task_ccc",
        "kind": "video",
        "prompt": "Protein shake pour, macro slow-motion, condensation on the glass",
        "prompt_truncated": False,
        "status": "failed",
        "output_url": None,
        "created_at": "2026-07-08T16:30:00Z",
        "updated_at": "2026-07-08T16:40:00Z",
    },
    {
        "id": "shot_2",
        "task_id": "runway_task_bbb",
        "kind": "video",
        "prompt": "Sunrise beach run, portrait",
        "prompt_truncated": False,
        "status": "succeeded",
        "output_url": "https://cdn.example.com/reels/shot_2.mp4",
        "video_path": "/workspace/videos/morning-grind.mp4",
        "created_at": "2026-06-26T10:04:00Z",
        "updated_at": "2026-06-30T14:20:00Z",
    },
    {
        "id": "shot_1",
        "task_id": "runway_task_aaa",
        "kind": "video",
        "prompt": "Kettlebell swing, portrait, dramatic lighting",
        "prompt_truncated": False,
        "status": "running",
        "output_url": None,
        "created_at": "2026-06-26T10:04:00Z",
        "updated_at": "2026-07-16T08:31:00Z",
    },
]
MEMORIES = [
    {"memory_id": "visual_style", "content": "Warm natural light, quick first-second hook, no licensed logos.", "updated_at": "2026-07-16T08:30:00Z"},
]
ARTIFACTS: dict[str, dict[str, Any]] = {
    "storyboard": {
        "artifact_id": "storyboard",
        "title": "Storyboard",
        "data": {"shots": 4},
        "view": [
            {"type": "heading", "text": "Reel: Morning grind", "level": 1},
            {"type": "timeline", "items": [
                {"title": "Shot 1 - Kettlebell swing", "status": "current", "text": "gen4.5 · 720:1280 · 5s · rendering", "time": "0-3s"},
                {"title": "Shot 2 - Sunrise run", "status": "done", "text": "gen4.5 · 720:1280 · 5s · ready", "time": "3-8s"},
                {"title": "Shot 3 - Protein pour", "status": "done", "text": "gen4.5 · 720:1280 · 4s · re-render after a failed pass", "time": "8-12s"},
                {"title": "Shot 4 - Mirror flex", "status": "upcoming", "text": "gen4.5 · 720:1280 · 4s · queued (rewritten without likeness)", "time": "12-15s"},
            ]},
            {"type": "details", "items": [
                {"label": "Aspect", "value": "720:1280 (9:16)"},
                {"label": "Track", "value": "licensed loop #204, 118 BPM"},
                {"label": "Shot 2 output", "value": "https://cdn.example.com/reels/shot_2.mp4"},
                {"label": "Shot 5 output", "value": "https://cdn.example.com/reels/shot_5.mp4"},
            ]},
        ],
        "created_at": "2026-06-26T10:03:00Z",
        "updated_at": "2026-07-16T08:30:00Z",
    },
    "publish_queue": {
        "artifact_id": "publish_queue",
        "title": "Publish queue",
        "data": {"pending": 2},
        "view": [
            {"type": "cards", "items": [
                {"title": "Morning grind", "text": "Ready to stage and publish (needs approval). Scheduled Friday 09:00.", "badge": "Approved", "tone": "success"},
                {"title": "Quiet-luxury gym", "text": "Draft caption written; waiting on shot_5 color pass before approval.", "badge": "Pending", "tone": "warning"},
                {"title": "Sunrise 5am routine", "text": "Held: audio track flagged for licensing review.", "badge": "Blocked", "tone": "danger"},
            ]},
        ],
        "created_at": "2026-06-30T14:21:00Z",
        "updated_at": "2026-07-16T08:30:00Z",
    },
    "ideas": {
        "artifact_id": "ideas",
        "title": "Ideas",
        "data": {"count": 5},
        "view": [
            {"type": "list", "items": [
                "Kettlebell flow - hook: 'the only 4 moves you need'",
                "5am routine - hook: 'what 5am actually looks like'",
                "Protein myth-busting - hook: 'you're wasting your shake'",
                "Quiet-luxury gym - hook: 'gymwear that isn't screaming'",
                "Cooldown stretch - hook: 'the 60 seconds everyone skips'",
            ]},
        ],
        "created_at": "2026-06-26T10:03:00Z",
        "updated_at": "2026-07-16T08:30:00Z",
    },
}
TOOLS: list[dict[str, Any]] = [
    {"tool_id": "runway", "title": "Runway Media Generation", "priority": "must_have", "status": "implemented", "note": "Async video/image/voice generation.", "updated_at": "2026-07-16T08:30:00Z"},
    {"tool_id": "instagram", "title": "Instagram", "priority": "must_have", "status": "implemented", "note": "Approval-gated Reel publishing.", "updated_at": "2026-07-03T09:12:00Z"},
    {"tool_id": "elevenlabs", "title": "ElevenLabs Voice", "priority": "good_to_have", "status": "enabled", "note": "Voiceover for hooks and captions.", "updated_at": "2026-07-10T11:05:00Z"},
    {"tool_id": "brave_search", "title": "Brave Search", "priority": "good_to_have", "status": "not_implemented", "note": "Topical research.", "updated_at": "2026-07-16T08:30:00Z"},
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
        return {"status": "ok", "app": "virality_machine", "mock_backend": True}
    if method == "GET" and relative == "connections":
        return {
            "status": "blocked",
            "providers": [
                {"provider": "openai", "agent_runtime": "codex", "enabled": True},
                {"provider": "claude", "agent_runtime": "claude_code", "enabled": True},
            ],
            "tools": [
                {"tool_id": "runway", "title": "Runway Media Generation", "priority": "must_have", "state": "off", "detail": "enable it in Internet Access and Tools"},
                {"tool_id": "instagram", "title": "Instagram", "priority": "must_have", "state": "ready", "detail": ""},
                {"tool_id": "brave_search", "title": "Brave Search", "priority": "good_to_have", "state": "ready", "detail": ""},
            ],
        }
    if method == "GET" and relative == "workspace":
        return _snapshot()
    if method == "GET" and relative == "render_jobs":
        return {"render_jobs": [dict(job) for job in RENDER_JOBS], "total": len(RENDER_JOBS), "max": 200}
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
        _append_message("agent", f"Mock reply: noted - {content[:60]}")
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
    page.locator("#beta-app-tabs").get_by_role("button", name="Virality Machine", exact=True).click()
    expect(page.locator("#panel-app-virality_machine")).to_be_visible()
    frame = page.frame_locator('iframe[title="Virality Machine"]')

    # A fresh open lands on the activation gate: the app explains itself and
    # nothing exists until the operator activates the workspace.
    expect(frame.locator("#hero")).to_be_visible()
    expect(frame.locator(".hero-about-block")).to_have_count(3)
    frame.locator("#hero-send").click()
    expect(frame.locator("#workspace")).to_be_visible()

    expect(frame.locator(".app-frame-title")).to_have_text("Virality Machine")
    expect(frame.locator("#goal-banner")).to_contain_text("Publish three fitness Reels a week")
    expect(frame.locator("#agent-settings-toggle")).to_contain_text("Codex · gpt-5.6-terra · High")

    # Feed renders user, agent (with inline markup), event, and error roles.
    expect(frame.locator("#feed")).to_contain_text("Make short fitness reels")
    expect(frame.locator("#feed .msg.agent strong").first).to_have_text("storyboard")
    expect(frame.locator("#feed .event-chip").first).to_contain_text('Created artifact "Storyboard"')
    expect(frame.locator("#feed .msg.error")).to_contain_text("Runway rejected shot_4")

    # The render queue shows live job status across the pipeline; the succeeded
    # A saved video opens the host Files viewer by workspace path while an
    # unsaved output retains only a copy affordance; external navigation never appears.
    render = frame.locator("#render-queue")
    expect(render).to_contain_text("shot_1")
    expect(render.locator(".render-job", has_text="shot_1")).to_contain_text("running")
    expect(render.locator(".render-job", has_text="shot_2")).to_contain_text("succeeded")
    expect(render.locator(".render-job", has_text="shot_3")).to_contain_text("failed")
    expect(render.locator(".render-job", has_text="shot_4")).to_contain_text("queued")
    view_video = render.locator(".render-job", has_text="shot_2").get_by_role("button", name="View finished video")
    expect(view_video).to_be_visible()
    expect(render.locator(".render-job", has_text="shot_2")).to_contain_text("/workspace/videos/morning-grind.mp4")
    expect(render.locator(".render-job", has_text="shot_5").get_by_role("button", name="Copy temporary URL")).to_be_visible()
    expect(render.locator("a")).to_have_count(0)
    expect(frame.locator("#render-queue img")).to_have_count(0)
    expect(frame.locator("#render-queue video")).to_have_count(0)

    # Review reuses the authenticated host Files viewer; the app never receives
    # a blob or navigates to an agent-authored URL.
    view_video.click()
    expect(page.locator("#panel-files")).to_be_visible()
    expect(page.locator("#file-viewer-title")).to_have_text("/workspace/videos/morning-grind.mp4")
    expect(page.locator("#file-video")).to_be_visible()
    page.locator("#beta-app-tabs").get_by_role("button", name="Virality Machine", exact=True).click()
    expect(page.locator("#panel-app-virality_machine")).to_be_visible()

    # The storyboard opens as a natively rendered timeline; the publish queue
    # and ideas artifacts render their own views.
    frame.locator("#storyboard .rail-item").click()
    overlay = frame.locator("#artifact-overlay")
    expect(overlay).to_be_visible()
    expect(overlay.locator(".b-timeline li.current")).to_contain_text("Kettlebell swing")
    expect(overlay.locator("img")).to_have_count(0)
    overlay.get_by_role("button", name="Close", exact=True).click()
    expect(overlay).to_be_hidden()

    frame.locator("#publish-queue .rail-item").click()
    expect(overlay.locator(".artifact-card").first).to_contain_text("Ready to stage and publish")
    overlay.get_by_role("button", name="Close", exact=True).click()

    expect(frame.locator("#artifacts")).to_contain_text("Ideas")
    expect(frame.locator("#tools")).to_contain_text("Runway Media Generation")
    expect(frame.locator("#tools")).to_contain_text("not implemented")
    expect(frame.locator("#memories")).to_contain_text("visual_style")
    expect(frame.locator("#memories").get_by_role("button", name="Edit")).to_be_visible()
    expect(frame.locator("#memories").get_by_role("button", name="Forget")).to_be_visible()

    # Schedule row with immediate controls: pause, then resume. A second weekly
    # recap schedule is seeded (paused, last run skipped), so the interaction is
    # scoped to the daily trend-scan row.
    expect(frame.locator("#schedules")).to_contain_text("Daily trend scan")
    expect(frame.locator("#schedules")).to_contain_text("Weekly performance recap")
    scan_row = frame.locator(".rail-item.schedule", has_text="Daily trend scan")
    scan_row.get_by_role("button", name="Pause").click()
    expect(scan_row).to_contain_text("paused")
    scan_row.get_by_role("button", name="Resume").click()
    expect(scan_row).not_to_contain_text("paused")

    # Sending a message round-trips through the app backend.
    frame.locator("#chat-input").fill("virality machine smoke message")
    frame.get_by_role("button", name="Send", exact=True).click()
    expect(frame.locator("#feed")).to_contain_text("virality machine smoke message")
    expect(frame.locator("#feed")).to_contain_text("Mock reply: noted")

    expect(frame.locator("#connections-pill")).to_have_text("Connections needed")
    _assert_single_scroll_desktop(page, frame, "Virality Machine app (desktop)")


def mobile_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    expect(page.locator("#sidebar-apps")).to_contain_text("Apps")
    page.locator("#mobile-nav-toggle").click()
    expect(page.locator("#nav-backdrop")).to_be_visible()
    page.locator("#beta-app-tabs").get_by_role("button", name="Virality Machine", exact=True).click()
    expect(page.locator("#nav-backdrop")).to_be_hidden()
    expect(page.locator("#panel-app-virality_machine")).to_be_visible()
    app_frame = page.locator('iframe[title="Virality Machine"]')
    app_frame.scroll_into_view_if_needed()
    frame = app_frame.content_frame
    expect(frame.locator("#feed")).to_contain_text("Make short fitness reels")
    expect(frame.locator("#render-queue")).to_contain_text("shot_1")
    frame.locator("#storyboard .rail-item").click()
    expect(frame.locator("#artifact-overlay .b-timeline")).to_be_visible()
    frame.get_by_role("button", name="Close").click()
    _assert_frame_no_horizontal_overflow(frame, "Virality Machine app")
    _assert_outer_page_locked(page, "Virality Machine app (mobile)")
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
