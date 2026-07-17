"""The typed contract an app fills in to become a workspace_kit app.

An app's ``backend.py`` builds one frozen ``WorkspaceAppConfig`` and calls
``workspace_kit.serve(config)``. The engine reads the scalar fields (host-derived
id/schema/port, never agent-controlled) and calls the domain hooks. Most apps set
only the required scalars, ``setup_brief``, and ``seed``; the domain hooks let an
app add verbs, routes, and digest lines without forking the engine.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


# One extra agent action verb: ``validate`` checks shape context-free (like the
# generic verbs), ``apply`` runs inside the action transaction and returns an
# error string or None. The engine enforces the per-turn write budget and
# journals the outcome around both, exactly as for the generic verbs. On an
# error return the engine rolls back every write ``apply`` made before
# rejecting, so hooks are free to write first and check afterwards.
@dataclass(frozen=True)
class DomainAction:
    validate: Callable[[dict[str, Any]], str | None]
    apply: Callable[[Any, dict[str, Any], str], str | None]


# Optional extra operator routes. Called after the generic routes miss; return a
# JSON-able dict to handle the request, or None to let the engine fall through
# to its 404. Signature mirrors ``route_ui_request``.
UiRouteHook = Callable[[str, str, Any, dict[str, list[str]]], "dict[str, Any] | None"]

# Optional extra agent routes. ``parts`` is the decoded, slash-validated path
# (e.g. ["agent", "posts"]); ``task_id`` is the attributed active task. Return a
# dict to handle, or None to fall through.
AgentRouteHook = Callable[[str, list[str], Any, str], "dict[str, Any] | None"]

# Optional extra digest sections, computed from the workspace cursor and appended
# after the generic sections as ``(header, lines)`` pairs.
DigestSectionsHook = Callable[[Any], "list[tuple[str, list[str]]]"]

# Seed hook, run once inside the transaction that first creates the workspace
# (search_path already set). Inserts seed schedules and tools inventory.
SeedHook = Callable[[Any, str], None]


@dataclass(frozen=True)
class WorkspaceAppConfig:
    app_id: str
    db_schema: str
    port: int
    title: str
    host: str
    admin_api_socket: str
    # Guided-setup text prepended to task input until the workspace has a goal.
    setup_brief: str
    seed: SeedHook | None = None
    domain_actions: Mapping[str, DomainAction] = field(default_factory=dict)
    domain_ui_routes: UiRouteHook | None = None
    domain_agent_routes: AgentRouteHook | None = None
    digest_sections: DigestSectionsHook | None = None
    # Extra rows for the connections report beyond the tools inventory —
    # entries shaped like the tool rows ({tool_id, title, priority, state,
    # detail}); exceptions are treated as no extra entries.
    extra_connections: Callable[[], list[dict[str, Any]]] | None = None
