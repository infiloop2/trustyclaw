"""Virality Machine app backend.

Virality Machine is a workspace_kit app: a single persistent short-form-video
workspace shared by the operator and their agent. All generic machinery, the run
worker, the action protocol, artifacts, schedules, memories, and the tools
inventory, lives in ``host.apps.workspace_kit``. This backend supplies only the
domain config:

- a guided-setup brief that steers the operator to define channel/niche, style,
  and a cadence objective;
- a first-open seed that records the tools inventory (Runway, Instagram,
  discovery, X, Brave) and a daily Instagram-discovery trend scan;
- one domain table, ``render_jobs`` (the Runway render queue), with a domain
  action ``upsert_render_job`` the agent calls as it polls ``runway_get_task``,
  a ``GET /render_jobs`` operator read route for the render-queue UI, and a
  digest section that reminds the agent of jobs still in flight.

Runway generation is asynchronous: every generate returns a ``task_id`` the
agent polls to a terminal status, and the output is a temporary download URL that
expires in about 24-48 hours. Publishing a finished Reel to Instagram is
host-approval-gated. See docs/architecture/apps/virality-machine.md for the full
contract and agent.md for the async-generate and publish pipelines.
"""

from __future__ import annotations

import calendar
import json
import os
import time
from typing import Any

from host.constants import APP_BACKEND_ADMIN_SOCKET_PATH, LOOPBACK
from host.runtime.core import db
from host.apps import workspace_kit
from host.apps.workspace_kit import engine
from host.apps.workspace_kit.config import DomainAction, WorkspaceAppConfig


APP_ID = "virality_machine"
DEFAULT_GOAL = "Turn timely ideas into finished short-form videos and publish the best to Instagram"
DEFAULT_MEASUREMENT = "Three publish-ready videos per week with clear review and publishing status"

# render_jobs bounds. Every value below is agent-controlled, so each is bounded
# by its JSON-encoded byte size (AGENTS.md: unbounded agent values flood the
# store). The row cap keeps the whole table, and the digest/UI reads built from
# it, comfortably under the host proxy response limit.
MAX_RENDER_JOBS = 200
RENDER_JOB_KINDS = ("video", "edit", "image", "speech")
RENDER_JOB_STATUSES = ("pending", "running", "succeeded", "failed", "cancelled")
RENDER_JOB_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
RENDER_JOB_PROMPT_BYTES = 4_000
RENDER_JOB_TASK_ID_BYTES = 512
RENDER_JOB_URL_BYTES = 4_096
RENDER_JOB_PATH_BYTES = 4_096
# The operator render-queue UI reads the most recently updated jobs. Clipping the
# prompt and capping the row count keeps this read far below the 1 MiB proxy cap
# even with maximum-length output URLs.
RENDER_QUEUE_UI_LIMIT = 60
RENDER_QUEUE_PROMPT_CLIP = 200
# The digest reminds the agent of jobs still in flight so it resumes polling
# runway_get_task instead of forgetting them across turns.
DIGEST_ACTIVE_JOB_LINES = 12

# The daily Instagram-discovery trend scan, seeded as an ordinary schedule the
# operator or agent can pause, edit, or delete. A fixed UTC hour keeps it daily
# and drift-free, matching Mission Pursuit's dream-cycle pattern.
TREND_SCHEDULE_ID = "trend_scan"
TREND_HOUR_UTC = 13
TREND_PROMPT = """Daily trend scan for Virality Machine. Use instagram_discovery to survey what is
working right now in this channel's niche: call instagram_discovery_get_trending_reels and
instagram_discovery_search_hashtag (and instagram_discovery_get_reels_by_audio for trending
audio) for the niche and cadence the operator set. Distil three to six fresh reel concepts,
each with a hook and an angle, and append them to the "ideas" artifact (create it if it does
not exist) without discarding still-relevant earlier ideas. Note any trending audio or format
worth reusing. Keep it concise; do not generate any media in this run."""

REEL_SETUP_BRIEF = """== Virality Machine ==
Use the app's existing creation goal and weekly output measurement. Start from the human's
idea, audience, or source material; ask only for creative details needed for the current
video. Keep ideas, storyboard, render queue, finished videos, and publishing status current.
The human can change the goal or measurement at any time."""


def _next_trend_epoch(now_epoch: float) -> float:
    parts = time.gmtime(now_epoch)
    today = calendar.timegm((parts.tm_year, parts.tm_mon, parts.tm_mday, TREND_HOUR_UTC, 0, 0, 0, 0, 0))
    return today if now_epoch < today else today + 24 * 3600


# Seeded tools inventory: metadata rows the agent starts with. Enabling a tool is
# a host-global operator action in the Tools admin UI; the app never edits host
# config, so every tool is seeded as "implemented" (exists, awaiting enable).
SEED_TOOLS: tuple[tuple[str, str, str, str], ...] = (
    ("runway", "Runway Media Generation", "must_have", "Async video, image, and speech generation; poll runway_get_task."),
    ("instagram", "Instagram", "must_have", "Publish Reels (approval-gated) and read recent media and publishing limits."),
    ("instagram_discovery", "Instagram Discovery", "good_to_have", "Trending Reels, hashtag, and audio research."),
    ("twitter", "X (Twitter)", "good_to_have", "Optional approval-gated cross-post of a published Reel."),
    ("brave_search", "Brave Search", "good_to_have", "Topical and competitive web research."),
)


def seed(cur: Any, now: str) -> None:
    """Record the tools inventory and the daily trend scan at activation.

    The activation screen disclosed both before the operator opted in; the
    scan is an ordinary schedule the operator can pause or delete."""
    cur.execute(
        "UPDATE workspace SET goal = %s, measurement = %s, updated_at = %s WHERE singleton = TRUE",
        (DEFAULT_GOAL, DEFAULT_MEASUREMENT, now),
    )
    for tool_id, title, priority, note in SEED_TOOLS:
        cur.execute(
            "INSERT INTO tools (tool_id, title, priority, status, note, created_at, updated_at)"
            " VALUES (%s, %s, %s, 'implemented', %s, %s, %s) ON CONFLICT (tool_id) DO NOTHING",
            (tool_id, title, priority, note, now, now),
        )
    cur.execute(
        "INSERT INTO schedules (schedule_id, title, prompt, every_minutes, next_run_at, enabled, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s) ON CONFLICT (schedule_id) DO NOTHING",
        (
            TREND_SCHEDULE_ID,
            "Daily trend scan",
            TREND_PROMPT,
            24 * 60,
            engine.format_utc(_next_trend_epoch(time.time())),
            now,
            now,
        ),
    )
    engine._insert_event(
        cur,
        "Recorded the Virality Machine tools inventory and scheduled a daily Instagram trend scan"
        f" for {TREND_HOUR_UTC:02d}:00 UTC — pause, edit, or delete it in Schedules",
        {"action": "seed", "schedule_id": TREND_SCHEDULE_ID},
        now,
    )


# ---------------------------------------------------------------------------
# Domain action: upsert_render_job


def _encoded_bytes(value: str) -> int:
    # The admin bridge and host proxy count JSON-encoded bytes, not characters,
    # so bound agent-controlled text by its encoded size (excluding the quotes).
    return len(json.dumps(value).encode()) - 2


def _check_bounded_text(action: dict[str, Any], key: str, limit: int, *, required: bool) -> str | None:
    if key not in action or action[key] is None:
        return f"{key} is required" if required else None
    value = action[key]
    if not isinstance(value, str):
        return f"{key} must be a string"
    if required and not value.strip():
        return f"{key} must not be empty"
    if _encoded_bytes(value) > limit:
        return f"{key} must be at most {limit} encoded bytes"
    return None


def validate_upsert_render_job(action: dict[str, Any]) -> str | None:
    """Context-free shape check for upsert_render_job (mirrors the generic verbs)."""
    error = engine._check_fields(
        action,
        required={"id", "kind", "prompt", "status"},
        optional={"task_id", "output_url", "video_path"},
    )
    if error:
        return error
    slug_error = engine._check_slug(action, "id")
    if slug_error:
        return slug_error
    if action["kind"] not in RENDER_JOB_KINDS:
        return f"kind must be one of: {', '.join(RENDER_JOB_KINDS)}"
    if action["status"] not in RENDER_JOB_STATUSES:
        return f"status must be one of: {', '.join(RENDER_JOB_STATUSES)}"
    return (
        _check_bounded_text(action, "prompt", RENDER_JOB_PROMPT_BYTES, required=True)
        or _check_bounded_text(action, "task_id", RENDER_JOB_TASK_ID_BYTES, required=False)
        or _check_bounded_text(action, "output_url", RENDER_JOB_URL_BYTES, required=False)
        or _check_bounded_text(action, "video_path", RENDER_JOB_PATH_BYTES, required=False)
    )


def apply_upsert_render_job(cur: Any, action: dict[str, Any], now: str) -> str | None:
    """Record or advance one render job. Search_path is already set and the
    per-turn write budget is enforced by the engine around this call."""
    job_id = action["id"]
    cur.execute("SELECT 1 FROM render_jobs WHERE id = %s", (job_id,))
    exists = cur.fetchone() is not None
    if not exists:
        cur.execute("SELECT COUNT(*) FROM render_jobs")
        count_row = cur.fetchone()
        if count_row and count_row[0] >= MAX_RENDER_JOBS:
            return f"render job limit reached ({MAX_RENDER_JOBS}); reuse an existing job id"
    status = action["status"]
    cur.execute(
        "INSERT INTO render_jobs (id, task_id, kind, prompt, status, output_url, video_path, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (id) DO UPDATE SET task_id = COALESCE(EXCLUDED.task_id, render_jobs.task_id),"
        " kind = EXCLUDED.kind, prompt = EXCLUDED.prompt, status = EXCLUDED.status,"
        " output_url = CASE WHEN EXCLUDED.status = 'succeeded'"
        " THEN COALESCE(EXCLUDED.output_url, render_jobs.output_url) ELSE NULL END,"
        " video_path = CASE WHEN EXCLUDED.status = 'succeeded'"
        " THEN COALESCE(EXCLUDED.video_path, render_jobs.video_path) ELSE NULL END,"
        " updated_at = EXCLUDED.updated_at",
        (
            job_id,
            action.get("task_id"),
            action["kind"],
            action["prompt"].strip(),
            status,
            action.get("output_url"),
            action.get("video_path"),
            now,
            now,
        ),
    )
    engine._insert_event(
        cur,
        f'{"Updated" if exists else "Recorded"} render job "{job_id}" ({action["kind"]}, {status})',
        {"action": "upsert_render_job", "id": job_id},
        now,
    )
    return None


# ---------------------------------------------------------------------------
# Domain operator route: GET /render_jobs


def _render_job_rows(cur: Any) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT id, task_id, kind, prompt, status, output_url, video_path, created_at, updated_at"
        " FROM render_jobs ORDER BY updated_at DESC, id ASC LIMIT %s",
        (RENDER_QUEUE_UI_LIMIT,),
    )
    jobs = []
    for r in cur.fetchall():
        prompt = r[3]
        clipped = prompt[:RENDER_QUEUE_PROMPT_CLIP]
        jobs.append(
            {
                "id": r[0],
                "task_id": r[1],
                "kind": r[2],
                "prompt": clipped,
                "prompt_truncated": len(prompt) > RENDER_QUEUE_PROMPT_CLIP,
                "status": r[4],
                "output_url": r[5],
                "video_path": r[6],
                "created_at": r[7],
                "updated_at": r[8],
            }
        )
    return jobs


def read_render_jobs() -> dict[str, Any]:
    with db.transaction() as cur:
        engine._set_search_path(cur)
        cur.execute("SELECT COUNT(*) FROM render_jobs")
        count_row = cur.fetchone()
        total = count_row[0] if count_row else 0
        jobs = _render_job_rows(cur)
    return {"render_jobs": jobs, "total": total, "max": MAX_RENDER_JOBS}


def domain_ui_routes(
    method: str, path: str, body: Any, query: dict[str, list[str]]
) -> dict[str, Any] | None:
    del body, query
    if method == "GET" and path == "/render_jobs":
        return read_render_jobs()
    return None


def domain_agent_routes(
    method: str, parts: list[str], body: Any, task_id: str
) -> dict[str, Any] | None:
    del body, task_id
    if method != "GET" or parts != ["agent", "render_jobs"]:
        return None
    with db.transaction() as cur:
        engine._set_search_path(cur)
        cur.execute(
            "SELECT id, task_id, kind, prompt, status, output_url, video_path, updated_at FROM render_jobs"
            " WHERE status IN ('pending', 'running') ORDER BY updated_at DESC, id ASC"
        )
        rows = cur.fetchall()
    return {
        "render_jobs": [
            {
                "id": row[0],
                "task_id": row[1],
                "kind": row[2],
                "prompt": row[3],
                "status": row[4],
                "output_url": row[5],
                "video_path": row[6],
                "updated_at": row[7],
            }
            for row in rows
        ]
    }


# ---------------------------------------------------------------------------
# Domain digest section: render jobs still in flight


def digest_sections(cur: Any) -> list[tuple[str, list[str]]]:
    cur.execute(
        "SELECT id, kind, prompt, status, task_id FROM render_jobs"
        " WHERE status IN ('pending', 'running') ORDER BY updated_at DESC, id ASC LIMIT %s",
        (DIGEST_ACTIVE_JOB_LINES,),
    )
    rows = cur.fetchall()
    if not rows:
        return []
    cur.execute("SELECT COUNT(*) FROM render_jobs WHERE status IN ('pending', 'running')")
    count_row = cur.fetchone()
    active = count_row[0] if count_row else len(rows)
    lines = [
        f"- {r[0]}: {r[1]}, {r[3]}, prompt {r[2]!r}"
        + (f", task {r[4]}" if r[4] else ", no task id yet")
        for r in rows
    ]
    if active > len(rows):
        lines.append(f"- ...and {active - len(rows)} more in flight")
    header = f"Render jobs in flight ({active}); poll runway_get_task and record with upsert_render_job:"
    return [(header, lines)]


CONFIG = WorkspaceAppConfig(
    app_id=APP_ID,
    db_schema=os.environ.get("TRUSTYCLAW_APP_DB_SCHEMA", "app_virality_machine"),
    port=int(os.environ.get("TRUSTYCLAW_APP_PORT", "7454")),
    title="Virality Machine",
    host=os.environ.get("TRUSTYCLAW_APP_HOST", LOOPBACK),
    admin_api_socket=os.environ.get(
        "TRUSTYCLAW_APP_ADMIN_API_SOCKET", APP_BACKEND_ADMIN_SOCKET_PATH
    ),
    setup_brief=REEL_SETUP_BRIEF,
    seed=seed,
    domain_actions={
        "upsert_render_job": DomainAction(
            validate=validate_upsert_render_job,
            apply=apply_upsert_render_job,
        )
    },
    domain_ui_routes=domain_ui_routes,
    domain_agent_routes=domain_agent_routes,
    digest_sections=digest_sections,
)


def main() -> int:
    return workspace_kit.serve(CONFIG)


if __name__ == "__main__":
    raise SystemExit(main())
