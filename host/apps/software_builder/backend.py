"""Software Builder app backend.

Software Builder is a workspace_kit app for creating, reviewing, and advancing
pull requests in repositories connected through the host's GitHub integration.
The generic run worker, action protocol, artifacts, schedules, memories, and
tools inventory live in ``host.apps.workspace_kit``. This backend supplies the
product defaults, records optional web research, and reports GitHub connection
health. Each durable artifact represents one pull request and its live review
state; repository work itself is performed by the resident agent through git
and GitHub under the host's network policy.

See docs/architecture/apps/software-builder.md for the app contract and
docs/architecture/apps/apps.md for the platform boundary.
"""

from __future__ import annotations

import os
from typing import Any

from host.constants import APP_BACKEND_ADMIN_SOCKET_PATH, LOOPBACK
from host.apps import workspace_kit
from host.apps.workspace_kit import engine
from host.apps.workspace_kit.config import WorkspaceAppConfig


APP_ID = "software_builder"
DEFAULT_GOAL = "Turn repository requests into focused, reviewed pull requests on connected GitHub repositories"
DEFAULT_MEASUREMENT = "Each pull request is tested, review-complete, and ready for the operator to merge"

# Brave Search is optional research alongside the required GitHub connection.
BRAVE_TOOL_ID = "brave_search"

SETUP_BRIEF = """== Software Builder ==
Use the app's existing pull-request goal and readiness measurement. Start from the human's
repository and requested change. Inspect the repository, implement on a focused branch,
run its verification, push through the connected GitHub integration, open or update one
pull request, and keep that pull request's artifact current with checks and review status.
Ask only for a repository or product choice that cannot be discovered safely."""


def seed(cur: Any, now: str) -> None:
    """Record Brave Search in the tools inventory on first open. It is an
    ordinary inventory row the operator (or agent) can retune or delete; it is
    not special-cased anywhere else."""
    cur.execute(
        "UPDATE workspace SET goal = %s, measurement = %s, updated_at = %s WHERE singleton = TRUE",
        (DEFAULT_GOAL, DEFAULT_MEASUREMENT, now),
    )
    cur.execute(
        "INSERT INTO tools (tool_id, title, priority, status, note, created_at, updated_at)"
        " VALUES (%s, %s, 'good_to_have', 'implemented', %s, %s, %s)"
        " ON CONFLICT (tool_id) DO NOTHING",
        (
            BRAVE_TOOL_ID,
            "Brave Search",
            "Web search for current technical documentation and implementation context"
            " (action brave_search_search_web); enable it in Internet Access and Tools.",
            now,
            now,
        ),
    )
    engine._insert_event(
        cur,
        "Recorded optional Brave Search for technical research (enable it to use it)",
        {"action": "upsert_tool", "tool_id": BRAVE_TOOL_ID},
        now,
    )


def github_connection() -> list[dict[str, Any]]:
    """Software Builder's output lands as commits and PRs, so the GitHub
    integration being enabled is part of its connections health (the same
    simple enabled check as every other row; credential details stay in the
    host's own GitHub panel)."""
    enabled = engine.integration_enabled("github")
    if enabled is None:
        return []
    return [{
        "tool_id": "github",
        "title": "GitHub",
        "priority": "must_have",
        "state": "ready" if enabled else "off",
        "detail": "" if enabled else "enable GitHub in Internet Access and Tools",
    }]


CONFIG = WorkspaceAppConfig(
    app_id=APP_ID,
    db_schema=os.environ.get("TRUSTYCLAW_APP_DB_SCHEMA", "app_software_builder"),
    port=int(os.environ.get("TRUSTYCLAW_APP_PORT", "7455")),
    title="Software Builder",
    host=os.environ.get("TRUSTYCLAW_APP_HOST", LOOPBACK),
    admin_api_socket=os.environ.get(
        "TRUSTYCLAW_APP_ADMIN_API_SOCKET", APP_BACKEND_ADMIN_SOCKET_PATH
    ),
    setup_brief=SETUP_BRIEF,
    seed=seed,
    extra_connections=github_connection,
)


def main() -> int:
    return workspace_kit.serve(CONFIG)


if __name__ == "__main__":
    raise SystemExit(main())
