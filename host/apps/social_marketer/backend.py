"""Social Marketer app backend.

Social Marketer is a workspace_kit app: one persistent marketing workspace the
operator and their agent share. The generic machinery (run worker, action
protocol, artifacts, schedules, memories, tools inventory) lives in
``host.apps.workspace_kit``. This backend supplies the domain config: the
marketing setup brief, a first-open seed (platform tools inventory plus a weekly
planning and a daily engagement schedule), the ``posts`` table that is the single
source of truth for drafted posts, two domain agent actions (``upsert_post`` and
``set_post_status``), and the operator routes the bespoke composer/calendar read
and write.

Every publish to X or LinkedIn is a host-approved platform tool call
made by the agent; this backend never posts to a third party. It only records
draft intent and the outcome the agent reports back.

See docs/architecture/apps/social-marketer.md for the app contract and
docs/architecture/apps/apps.md for the platform boundary.
"""

from __future__ import annotations

from http import HTTPStatus
import os
import time
import uuid
from typing import Any
from urllib.parse import unquote

from host.constants import APP_BACKEND_ADMIN_SOCKET_PATH, LOOPBACK
from host.runtime.core import db
from host.apps import workspace_kit
from host.apps.workspace_kit import engine
from host.apps.workspace_kit.config import DomainAction, WorkspaceAppConfig
from host.apps.workspace_kit.views import SLUG_RE


APP_ID = "social_marketer"
DEFAULT_GOAL = "Plan and publish useful, on-brand content consistently on X and LinkedIn"
DEFAULT_MEASUREMENT = "A complete weekly calendar, an approval-ready draft queue, and tracked engagement learnings"

# Domain caps. Every agent-controlled value written to the posts table is bounded
# by encoded bytes before the write, so malformed or oversized input cannot flood
# the store. The platform enums are also enforced structurally in the migration.
MAX_POSTS = 500
PLATFORMS = ("x", "linkedin")
POST_STATUSES = ("draft", "approved", "posted")
# Per-platform body caps, measured by UTF-8 encoded bytes.
PLATFORM_BODY_BYTES = {"x": 4000, "linkedin": 3000}
# A platform post id or permalink recorded after a successful publish.
EXTERNAL_REF_MAX_BYTES = 200
# List responses clip each body to a short preview so a full 500-post list stays
# well under the admin proxy response cap; the composer reads one full post by id
# when it opens a draft to edit.
POST_PREVIEW_BYTES = 280

WEEKLY_MINUTES = 7 * 24 * 60
DAILY_MINUTES = 24 * 60

SETUP_BRIEF = """== Social Marketer ==
Use the app's existing publishing goal and campaign measurement. Start from the human's
brand, audience, campaign, or draft request; ask only for missing details needed to produce
useful X or LinkedIn work. Keep the calendar, drafts, approvals, and performance learnings
current. The human can change the goal or measurement at any time."""

WEEKLY_PROMPT = """Weekly campaign planning. Review the goal, audience, and channels, then look at
last week's performance. Plan this week's posts across the active channels and
draft each one into the posts table with upsert_post (it starts as a draft): set
its platform, body, and a scheduled_for time. Keep or update a campaign artifact
with the plan and objectives. Do not publish anything here; publishing each post
is a separate, human-approved step."""

DAILY_PROMPT = """Daily engagement and trend check. Scan X trends and LinkedIn
discovery for topics relevant to the brand, and review how recently published
posts are performing (record metrics in a performance artifact). Draft timely
posts with upsert_post when there is a clear opportunity, and flag which drafts
are worth publishing today. Do not publish without human approval."""


# ---------------------------------------------------------------------------
# First-open seed: tools inventory and recurring schedules.


def seed(cur: Any, now: str) -> None:
    cur.execute(
        "UPDATE workspace SET goal = %s, measurement = %s, updated_at = %s WHERE singleton = TRUE",
        (DEFAULT_GOAL, DEFAULT_MEASUREMENT, now),
    )
    tools = [
        ("twitter", "X (Twitter)", "must_have", "Search, read, and publish X posts, replies, and quotes; publishing is approval-gated."),
        ("linkedin", "LinkedIn", "must_have", "Read your profile and publish text-only LinkedIn posts; publishing is approval-gated."),
        ("brave_search", "Brave Search", "must_have", "Research brand, competitors, and topics on the web."),
        ("linkedin_discovery", "LinkedIn Discovery", "good_to_have", "Research public LinkedIn posts via SerpApi."),
    ]
    for tool_id, title, priority, note in tools:
        cur.execute(
            "INSERT INTO tools (tool_id, title, priority, status, note, created_at, updated_at)"
            " VALUES (%s, %s, %s, 'implemented', %s, %s, %s) ON CONFLICT (tool_id) DO NOTHING",
            (tool_id, title, priority, note, now, now),
        )
    schedules = [
        ("campaign_planning", "Weekly campaign planning", WEEKLY_PROMPT, WEEKLY_MINUTES),
        ("engagement_check", "Daily engagement and trend check", DAILY_PROMPT, DAILY_MINUTES),
    ]
    for schedule_id, title, prompt, minutes in schedules:
        cur.execute(
            "INSERT INTO schedules (schedule_id, title, prompt, every_minutes, next_run_at, enabled, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s) ON CONFLICT (schedule_id) DO NOTHING",
            (schedule_id, title, prompt, minutes, engine.format_utc(time.time() + minutes * 60), now, now),
        )
    engine._insert_event(
        cur,
        "Seeded the Social Marketer workspace: the platform tools inventory and"
        " weekly planning + daily engagement schedules — pause, edit, or delete them in Schedules.",
        {"action": "seed"},
        now,
    )


# ---------------------------------------------------------------------------
# Domain agent actions: upsert_post (draft content) and set_post_status
# (lifecycle + external_ref). Both share their validation with the operator
# composer so the two writers enforce one contract.


def _validate_post_id(action: dict[str, Any]) -> str | None:
    value = action.get("id")
    if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
        return f"id must match {SLUG_RE.pattern}"
    return None


def _validate_upsert_post(action: dict[str, Any]) -> str | None:
    error = engine._check_fields(action, required={"id", "platform", "body"}, optional={"scheduled_for"})
    if error:
        return error
    error = _validate_post_id(action)
    if error:
        return error
    platform = action["platform"]
    if platform not in PLATFORMS:
        return f"platform must be one of: {', '.join(PLATFORMS)}"
    body = action["body"]
    if not isinstance(body, str) or not body.strip():
        return "body must be a non-empty string"
    cap = PLATFORM_BODY_BYTES[platform]
    if len(body.encode("utf-8")) > cap:
        return f"body for {platform} must be at most {cap} encoded bytes"
    if "scheduled_for" in action:
        scheduled_for = action["scheduled_for"]
        if scheduled_for is not None and (
            not isinstance(scheduled_for, str) or engine.parse_utc(scheduled_for) is None
        ):
            return "scheduled_for must be null or a UTC timestamp like 2026-07-09T15:00:00Z"
    return None


def _apply_upsert_post(cur: Any, action: dict[str, Any], now: str) -> str | None:
    post_id = action["id"]
    # Serialize draft edits against lifecycle changes. Once this lock is held,
    # set_post_status cannot approve or publish different bytes concurrently.
    cur.execute("SELECT status FROM posts WHERE id = %s FOR UPDATE", (post_id,))
    existing = cur.fetchone()
    exists = existing is not None
    if existing is not None and existing[0] != "draft":
        return f'post "{post_id}" is {existing[0]}; only drafts can be edited'
    if not exists:
        cur.execute("SELECT COUNT(*) FROM posts")
        count_row = cur.fetchone()
        if count_row and count_row[0] >= MAX_POSTS:
            return f"post limit reached ({MAX_POSTS}); delete one first"
    platform = action["platform"]
    body = action["body"].strip()
    scheduled_for = action.get("scheduled_for")
    if exists:
        # Approval and publishing bind the exact platform/body/schedule. Only
        # drafts remain mutable, so stored lifecycle state cannot describe
        # content different from what the operator approved or published.
        cur.execute(
            "UPDATE posts SET platform = %s, body = %s, scheduled_for = %s, updated_at = %s WHERE id = %s",
            (platform, body, scheduled_for, now, post_id),
        )
    else:
        cur.execute(
            "INSERT INTO posts (id, platform, body, status, scheduled_for, external_ref, created_at, updated_at)"
            " VALUES (%s, %s, %s, 'draft', %s, NULL, %s, %s)",
            (post_id, platform, body, scheduled_for, now, now),
        )
    engine._insert_event(
        cur,
        f'{"Updated" if exists else "Drafted"} {platform} post {post_id}',
        {"action": "upsert_post", "post_id": post_id},
        now,
    )
    return None


def _validate_set_post_status(action: dict[str, Any]) -> str | None:
    error = engine._check_fields(action, required={"id", "status"}, optional={"external_ref"})
    if error:
        return error
    error = _validate_post_id(action)
    if error:
        return error
    if action["status"] not in POST_STATUSES:
        return f"status must be one of: {', '.join(POST_STATUSES)}"
    if "external_ref" in action:
        external_ref = action["external_ref"]
        if not isinstance(external_ref, str) or not external_ref.strip():
            return "external_ref must be a non-empty string"
        if len(external_ref.encode("utf-8")) > EXTERNAL_REF_MAX_BYTES:
            return f"external_ref must be at most {EXTERNAL_REF_MAX_BYTES} encoded bytes"
    return None


def _apply_set_post_status(cur: Any, action: dict[str, Any], now: str) -> str | None:
    post_id = action["id"]
    cur.execute("SELECT status FROM posts WHERE id = %s FOR UPDATE", (post_id,))
    existing = cur.fetchone()
    if existing is None:
        return f'post "{post_id}" does not exist; use upsert_post first'
    if existing[0] == "posted":
        return f'post "{post_id}" is posted; posted records are immutable'
    status = action["status"]
    if POST_STATUSES.index(status) < POST_STATUSES.index(existing[0]):
        return f'post "{post_id}" cannot move from {existing[0]} back to {status}'
    if "external_ref" in action:
        cur.execute(
            "UPDATE posts SET status = %s, external_ref = %s, updated_at = %s WHERE id = %s",
            (status, action["external_ref"].strip(), now, post_id),
        )
    else:
        cur.execute(
            "UPDATE posts SET status = %s, updated_at = %s WHERE id = %s",
            (status, now, post_id),
        )
    engine._insert_event(
        cur,
        f"Set post {post_id} to {status}",
        {"action": "set_post_status", "post_id": post_id},
        now,
    )
    return None


DOMAIN_ACTIONS = {
    "upsert_post": DomainAction(validate=_validate_upsert_post, apply=_apply_upsert_post),
    "set_post_status": DomainAction(validate=_validate_set_post_status, apply=_apply_set_post_status),
}


# ---------------------------------------------------------------------------
# Posts reads and the operator composer.


def _post_row(r: tuple[Any, ...], *, preview: bool) -> dict[str, Any]:
    body = r[2]
    truncated = False
    if preview:
        body, truncated = engine.clip_encoded_text(body, POST_PREVIEW_BYTES)
    return {
        "id": r[0],
        "platform": r[1],
        "body": body,
        "truncated": truncated,
        "body_bytes": r[7],
        "status": r[3],
        "scheduled_for": r[4],
        "external_ref": r[5],
        "created_at": r[6],
    }


def _list_posts() -> list[dict[str, Any]]:
    with db.transaction() as cur:
        engine._set_search_path(cur)
        cur.execute(
            "SELECT id, platform, body, status, scheduled_for, external_ref, created_at,"
            " OCTET_LENGTH(body) FROM posts"
            " ORDER BY scheduled_for ASC NULLS LAST, created_at ASC, id ASC"
        )
        return [_post_row(r, preview=True) for r in cur.fetchall()]


def _read_post(post_id: str) -> dict[str, Any]:
    with db.transaction() as cur:
        engine._set_search_path(cur)
        cur.execute(
            "SELECT id, platform, body, status, scheduled_for, external_ref, created_at,"
            " OCTET_LENGTH(body) FROM posts WHERE id = %s",
            (post_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise engine.AppError(HTTPStatus.NOT_FOUND, "post not found")
    return {"post": _post_row(row, preview=False)}


def _operator_upsert_post(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise engine.AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "post body must be an object")
    action: dict[str, Any] = {"action": "upsert_post"}
    post_id = body.get("id")
    action["id"] = post_id if isinstance(post_id, str) and post_id else _new_post_id()
    if "platform" in body:
        action["platform"] = body["platform"]
    if "body" in body:
        action["body"] = body["body"]
    scheduled_for = body.get("scheduled_for")
    # The composer sends an empty string for "no schedule"; treat that as unset.
    if isinstance(scheduled_for, str) and scheduled_for.strip():
        action["scheduled_for"] = scheduled_for
    error = _validate_upsert_post(action)
    if error is not None:
        raise engine.AppError(HTTPStatus.UNPROCESSABLE_ENTITY, error)
    with db.transaction() as cur:
        engine._set_search_path(cur)
        error = _apply_upsert_post(cur, action, engine._utc_now())
        if error is not None:
            raise engine.AppError(HTTPStatus.UNPROCESSABLE_ENTITY, error)
    return {"post_id": action["id"]}


def _operator_delete_post(post_id: str) -> dict[str, Any]:
    with db.transaction() as cur:
        engine._set_search_path(cur)
        cur.execute("SELECT platform, status FROM posts WHERE id = %s FOR UPDATE", (post_id,))
        row = cur.fetchone()
        if row is None:
            raise engine.AppError(HTTPStatus.NOT_FOUND, "post not found")
        if row[1] != "draft":
            raise engine.AppError(HTTPStatus.CONFLICT, "only drafts can be deleted")
        cur.execute("DELETE FROM posts WHERE id = %s", (post_id,))
        engine._insert_event(
            cur,
            f"Removed {row[0]} post {post_id}",
            {"action": "delete_post", "post_id": post_id},
            engine._utc_now(),
        )
    return {"deleted": post_id}


def _new_post_id() -> str:
    # A slug-shaped id (SLUG_RE) so operator- and agent-created posts share one
    # key space and one validation path.
    return "post-" + uuid.uuid4().hex[:16]


def _domain_ui_routes(
    method: str, path: str, body: Any, query: dict[str, list[str]]
) -> dict[str, Any] | None:
    parts = [unquote(part) for part in path.strip("/").split("/")]
    if any("/" in part or "\\" in part or not part for part in parts):
        return None
    if parts[:2] != ["api", "posts"]:
        return None
    if method == "GET" and len(parts) == 2:
        return {"posts": _list_posts()}
    if method == "POST" and len(parts) == 2:
        return _operator_upsert_post(body)
    if method == "GET" and len(parts) == 3:
        return _read_post(parts[2])
    if method == "DELETE" and len(parts) == 3:
        return _operator_delete_post(parts[2])
    return None


def _domain_agent_routes(
    method: str, parts: list[str], body: Any, task_id: str
) -> dict[str, Any] | None:
    if method == "GET" and parts == ["agent", "posts"]:
        # The list clips bodies to a preview, so the agent reads the full body
        # of a specific post by id before publishing it.
        return {"posts": _list_posts()}
    if method == "GET" and len(parts) == 3 and parts[:2] == ["agent", "posts"]:
        return _read_post(parts[2])
    return None


CONFIG = WorkspaceAppConfig(
    app_id=APP_ID,
    db_schema=os.environ.get("TRUSTYCLAW_APP_DB_SCHEMA", "app_social_marketer"),
    port=int(os.environ.get("TRUSTYCLAW_APP_PORT", "7453")),
    title="Social Marketer",
    host=os.environ.get("TRUSTYCLAW_APP_HOST", LOOPBACK),
    admin_api_socket=os.environ.get(
        "TRUSTYCLAW_APP_ADMIN_API_SOCKET", APP_BACKEND_ADMIN_SOCKET_PATH
    ),
    setup_brief=SETUP_BRIEF,
    seed=seed,
    domain_actions=DOMAIN_ACTIONS,
    domain_ui_routes=_domain_ui_routes,
    domain_agent_routes=_domain_agent_routes,
)


def main() -> int:
    return workspace_kit.serve(CONFIG)


if __name__ == "__main__":
    raise SystemExit(main())
