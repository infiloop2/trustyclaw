"""Mission Pursuit app backend.

Mission Pursuit is the reference workspace_kit app: a single persistent
workspace shared by the operator and their agent. All of its machinery, the run
worker, the action protocol, artifacts, schedules, memories, and the tools
inventory, lives in ``host.apps.workspace_kit``. This backend only supplies the
domain config: its title and host-derived id/schema/port, the generic guided
setup brief, and a first-open seed that schedules the nightly memory dream cycle.
It has no domain tables and no domain actions.

See docs/architecture/apps/mission-pursuit.md for the app contract and
docs/architecture/apps/apps.md for the platform boundary.
"""

from __future__ import annotations

import calendar
import os
import time
from typing import Any

from host.constants import LOOPBACK
from host.apps import workspace_kit
from host.apps.workspace_kit import engine
from host.apps.workspace_kit.config import WorkspaceAppConfig


APP_ID = "mission_pursuit"

# The nightly dream cycle is seeded as a normal schedule the operator (or
# agent) can pause, edit, or delete; 1440-minute cadence on a fixed UTC-hour
# grid keeps it nightly and drift-free.
DREAM_SCHEDULE_ID = "dream_cycle"
DREAM_HOUR_UTC = 3

DREAM_PROMPT = """Nightly dream cycle: clean up and reorganize this workspace's memory.
Review every memory in the digest: merge duplicates, drop stale or incorrect entries,
rewrite unclear ones so each is one small self-contained fact (under 300 characters), and
add memories for anything important from recent work that is not yet recorded. Keep the
tools inventory and the goal/measurement honest while you are at it. Finish with a short
summary of what changed; if nothing needs changing, say so and change nothing."""


def _next_dream_epoch(now_epoch: float) -> float:
    parts = time.gmtime(now_epoch)
    today = calendar.timegm((parts.tm_year, parts.tm_mon, parts.tm_mday, DREAM_HOUR_UTC, 0, 0, 0, 0, 0))
    return today if now_epoch < today else today + 24 * 3600


def seed(cur: Any, now: str) -> None:
    """Seed the nightly memory dream cycle as a normal schedule at activation
    (the activation screen disclosed it before the operator opted in).
    It is not special-cased anywhere else: the operator can pause or delete it
    and the agent can retune it like any schedule it created itself."""
    cur.execute(
        "INSERT INTO schedules (schedule_id, title, prompt, every_minutes, next_run_at, enabled, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s) ON CONFLICT (schedule_id) DO NOTHING",
        (
            DREAM_SCHEDULE_ID,
            "Dream cycle",
            DREAM_PROMPT,
            24 * 60,
            engine.format_utc(_next_dream_epoch(time.time())),
            now,
            now,
        ),
    )
    engine._insert_event(
        cur,
        "Scheduled the nightly dream cycle (memory cleanup at"
        f" {DREAM_HOUR_UTC:02d}:00 UTC) — pause, edit, or delete it in Schedules",
        {"action": "create_schedule", "schedule_id": DREAM_SCHEDULE_ID},
        now,
    )


CONFIG = WorkspaceAppConfig(
    app_id=APP_ID,
    db_schema=os.environ.get("TRUSTYCLAW_APP_DB_SCHEMA", "app_mission_pursuit"),
    port=int(os.environ.get("TRUSTYCLAW_APP_PORT", "7451")),
    title="Mission Pursuit",
    host=os.environ.get("TRUSTYCLAW_APP_HOST", LOOPBACK),
    admin_api_socket=os.environ.get(
        "TRUSTYCLAW_APP_ADMIN_API_SOCKET", "/run/trustyclaw-admin-api/app-backend.sock"
    ),
    setup_brief=engine.GENERIC_SETUP_BRIEF,
    seed=seed,
)


def main() -> int:
    return workspace_kit.serve(CONFIG)


if __name__ == "__main__":
    raise SystemExit(main())
