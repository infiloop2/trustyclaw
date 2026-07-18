"""Alpha Seeker app backend.

Alpha Seeker is a resident financial-research workspace_kit app: one persistent
workspace the operator shares with a research agent. All of its machinery, the
run worker, the action protocol, artifacts, schedules, memories, and the tools
inventory, lives in ``host.apps.workspace_kit``. This backend only supplies the
domain config: its title and host-derived id/schema/port, a financial-research
setup brief, and a first-open seed that records the read-only market tools it
uses and schedules a daily pre-market brief (disclosed on the activation screen). It has no domain tables and no
domain actions; all research state is ordinary workspace artifacts.

Everything Alpha Seeker reads about the world (portfolio, prediction markets, the web)
comes from the agent calling existing host tools; those tools are read-only and
no order-placement tool exists anywhere. See docs/architecture/apps/alpha-seeker.md for
the app contract and docs/architecture/apps/apps.md for the platform boundary.
"""

from __future__ import annotations

import calendar
import os
import time
from typing import Any

from host.constants import APP_BACKEND_ADMIN_SOCKET_PATH, LOOPBACK
from host.apps import workspace_kit
from host.apps.workspace_kit import engine
from host.apps.workspace_kit.config import WorkspaceAppConfig


APP_ID = "alpha_seeker"
DEFAULT_GOAL = "Find and maintain well-sourced, asymmetric market ideas without placing trades"
DEFAULT_MEASUREMENT = "A current watchlist with entry theses, targets, risks, and a cited daily market brief"

# The daily pre-market brief starts at activation — the activation screen
# discloses it before the operator opts in — and it is a normal schedule the
# operator (or agent) can pause, edit, or delete; a 1440-minute cadence on a fixed UTC-hour grid keeps
# it daily and drift-free, like Mission Pursuit's dream cycle.
BRIEF_SCHEDULE_ID = "pre_market_brief"
BRIEF_HOUR_UTC = 12

BRIEF_PROMPT = """Daily pre-market brief. Refresh the workspace against live reads, then write it down:
1. Read live portfolio state with ibkr_get_positions and ibkr_get_account_summary; rewrite the
   positions artifact (symbol, quantity, mark, value, unrealized PnL) from that read.
2. Walk the watchlist artifact: for each tracked idea recheck the thesis, price, and target, and
   note anything that moved. Use brave_search_search_web for news when a name needs context.
3. Scan Polymarket for the markets tracked in the prediction_markets artifact and any newly
   relevant ones (polymarket_search / polymarket_list_markets, then polymarket_get_market and
   polymarket_price_history for prices); update that artifact.
4. Append one dated note to the research artifact summarizing portfolio state, watchlist moves,
   notable prediction-market shifts, and any new ideas or risks.
Cite every figure with the tool it came from and the UTC timestamp of the read. Do not place
trades: there is no order tool. Propose entries and exits in research/watchlist only."""


# Seeded read-only market tools. These are metadata rows in the workspace tools
# inventory, not host tool enablement: the operator still enables each tool
# host-wide in the tools admin UI. Status "implemented" means the tool exists on
# the host but must be enabled before the agent can call it.
SEEDED_TOOLS = (
    (
        "ibkr",
        "Interactive Brokers",
        "must_have",
        "implemented",
        "Read-only live portfolio: positions, account summary, completed trades. No order placement exists.",
    ),
    (
        "polymarket",
        "Polymarket",
        "must_have",
        "implemented",
        "Read-only public prediction-market data: markets, events, prices, order book, price history.",
    ),
    (
        "brave_search",
        "Brave Search",
        "good_to_have",
        "implemented",
        "Public-web research for news and context behind watchlist names and market moves.",
    ),
)


ALPHA_SETUP_BRIEF = """== Alpha Seeker ==
Use the app's existing research mandate and measurement. Start from the human's request,
keep the positions, watchlist, research, and prediction-markets surfaces current, and ask
only for details needed to do the requested research. The human can change the mandate or
measurement at any time."""


def _next_brief_epoch(now_epoch: float) -> float:
    parts = time.gmtime(now_epoch)
    today = calendar.timegm((parts.tm_year, parts.tm_mon, parts.tm_mday, BRIEF_HOUR_UTC, 0, 0, 0, 0, 0))
    return today if now_epoch < today else today + 24 * 3600


def seed(cur: Any, now: str) -> None:
    """Seed the read-only market tools inventory and the daily pre-market brief
    on first open. Both are ordinary workspace state: the operator can edit or
    delete the tool rows and pause or delete the schedule, and the agent can
    retune them like anything it created itself."""
    cur.execute(
        "UPDATE workspace SET goal = %s, measurement = %s, updated_at = %s WHERE singleton = TRUE",
        (DEFAULT_GOAL, DEFAULT_MEASUREMENT, now),
    )
    for tool_id, title, priority, status, note in SEEDED_TOOLS:
        cur.execute(
            "INSERT INTO tools (tool_id, title, priority, status, note, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (tool_id) DO NOTHING",
            (tool_id, title, priority, status, note, now, now),
        )
    engine._insert_event(
        cur,
        "Seeded the read-only market tools: Interactive Brokers, Polymarket, and Brave Search"
        " (enable each host-wide in Tools to let the agent use them)",
        {"action": "seed_tools"},
        now,
    )
    cur.execute(
        "INSERT INTO schedules (schedule_id, title, prompt, every_minutes, next_run_at, enabled, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s) ON CONFLICT (schedule_id) DO NOTHING",
        (
            BRIEF_SCHEDULE_ID,
            "Pre-market brief",
            BRIEF_PROMPT,
            24 * 60,
            engine.format_utc(_next_brief_epoch(time.time())),
            now,
            now,
        ),
    )
    engine._insert_event(
        cur,
        "Scheduled the daily pre-market brief (refresh positions, watchlist, and Polymarket at"
        f" {BRIEF_HOUR_UTC:02d}:00 UTC) — pause, edit, or delete it in Schedules",
        {"action": "create_schedule", "schedule_id": BRIEF_SCHEDULE_ID},
        now,
    )


CONFIG = WorkspaceAppConfig(
    app_id=APP_ID,
    db_schema=os.environ.get("TRUSTYCLAW_APP_DB_SCHEMA", "app_alpha_seeker"),
    port=int(os.environ.get("TRUSTYCLAW_APP_PORT", "7452")),
    title="Alpha Seeker",
    host=os.environ.get("TRUSTYCLAW_APP_HOST", LOOPBACK),
    admin_api_socket=os.environ.get(
        "TRUSTYCLAW_APP_ADMIN_API_SOCKET", APP_BACKEND_ADMIN_SOCKET_PATH
    ),
    setup_brief=ALPHA_SETUP_BRIEF,
    seed=seed,
)


def main() -> int:
    return workspace_kit.serve(CONFIG)


if __name__ == "__main__":
    raise SystemExit(main())
