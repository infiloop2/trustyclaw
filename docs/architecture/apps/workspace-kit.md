# Workspace Kit

Workspace Kit is the shared abstraction for resident apps that pursue an
ongoing goal with a human. It provides the durable workspace concepts, agent
turn lifecycle, and immediate operator controls. An app adds its domain model
and product UI without rebuilding those concepts.

## Concepts

- **Goal and measurement.** One editable goal says what the resident agent is
  pursuing; one editable measurement says how progress is judged. Both enter
  every turn digest and remain visible at the top of the workspace.
- **Conversation and runs.** Messages form a bounded durable feed. A human
  message starts a run, queues behind the active run, or steers the active host
  task. Events and terminal errors use the same feed, so the workspace explains
  what happened without a second activity model.
- **Artifacts.** Artifacts are durable named records with agent-authored data
  and an optional declarative view. Shared blocks cover text, tables, cards,
  metrics, charts, timelines, code, and immediate button, toggle, or field
  controls. A control interaction becomes a new agent input; it never executes
  agent-authored HTML or JavaScript.
- **Schedules.** A schedule is a self-contained prompt, cadence, next-run time,
  and enabled state. The poller starts a fresh run when it is due. The human can
  pause, resume, edit, or remove each schedule immediately. Disabled schedules
  do no work and spend nothing.
- **Memories.** Memories are small durable facts keyed by id. The agent creates,
  updates, or forgets them; the human can edit or remove each one. A bounded
  memory digest enters every turn, so preferences and constraints survive chat
  history clipping.
- **Tool inventory.** The workspace records which capabilities matter to the
  app and why. Actual credentials, enablement, approval, egress, and execution
  remain host-owned tool concerns. Inventory state never grants a capability.
- **Agent settings.** The selected runtime, model, and effort apply to new
  turns. Changing them rotates the thread before the next run instead of trying
  to mutate an in-flight runtime session.

## App hook

An app provides one `WorkspaceAppConfig` with its title, setup brief,
first-activation seed, connection-health report, and optional domain hooks. The
seed creates ordinary artifacts, memories, schedules, and tool inventory rows;
after activation they behave exactly like operator- or agent-created rows.

A domain hook can add validated agent actions, operator routes, digest sections,
and transactional cleanup. Domain tables live beside the shared workspace
tables in that app's schema. The product UI calls the shared routes for common
concepts and its own routes only for domain records. There is one worker and one
turn lifecycle regardless of how many domain hooks an app adds.

The app owns its product composition: Mission Pursuit emphasizes general
artifacts, Alpha Seeker composes research dashboards, Social Marketer adds a
post calendar, Virality Machine adds a render queue, and Software Builder
centers pull-request state. Those surfaces all use the same goal, feed,
artifact, schedule, memory, tool, and run semantics.

## Technical implementation

A Workspace Kit app is a normal package under `host/apps/<app_id>/`. It provides
the standard app manifest, agent instructions, migrations, backend entrypoint,
and UI directory described in [Apps](apps.md). The package name is the app id.
Choose one unused `host_slot`; the host derives the service port, Linux user,
database schema, and routes from those two values.

```text
host/apps/<app_id>/
├── agent.md
├── backend.py
├── manifest.json
├── migrations/
│   ├── 0001_workspace_base.sql
│   └── 0002_<domain>.sql       # only when the app owns domain tables
└── ui/
    ├── index.html
    ├── <app_id>.css
    └── <app_id>.js
```

The manifest enables the attributed agent API and points the host at those
files:

```json
{
  "host_slot": 6,
  "title": "My Workspace App",
  "release_stage": "stable",
  "agent": {"instructions": "agent.md", "api": true},
  "backend": {"entrypoint": "backend.py"},
  "database": {"migrations": "migrations"},
  "ui": {"path": "ui"}
}
```

The app embeds the shared base migration because applied migrations are
per-app immutable history. Copy an existing non-Mission Workspace Kit app's
`migrations/0001_workspace_base.sql` byte for byte into the new app with the
same name. Put domain tables in `0002_<domain>.sql` and later migrations. Tests
require every Workspace Kit app's first migration to create the same base
schema.

The declarative view renderer is one host-owned shared asset, not an embedded
copy. Load it from the app's `index.html`:

```html
<link rel="stylesheet" href="/workspace-kit/view_blocks.css">
<script src="/workspace-kit/view_blocks.js"></script>
```

The app iframe can load these fixed same-origin assets under its existing CSP.
The host serves them directly from `host/apps/workspace_kit/ui/`, so renderer
fixes have one source and adding an app does not duplicate JavaScript or CSS.

The backend imports the shared server and supplies one typed config. A minimal
backend has this shape:

```python
import os
from typing import Any

from host.apps import workspace_kit
from host.apps.workspace_kit.config import WorkspaceAppConfig
from host.constants import LOOPBACK


APP_ID = "my_workspace_app"


def seed(cur: Any, now: str) -> None:
    cur.execute(
        "UPDATE workspace SET goal = %s, measurement = %s, updated_at = %s "
        "WHERE singleton = TRUE",
        ("The durable goal", "How progress is judged", now),
    )


CONFIG = WorkspaceAppConfig(
    app_id=APP_ID,
    db_schema=os.environ.get("TRUSTYCLAW_APP_DB_SCHEMA", f"app_{APP_ID}"),
    port=int(os.environ.get("TRUSTYCLAW_APP_PORT", "7450")),
    title="My Workspace App",
    host=os.environ.get("TRUSTYCLAW_APP_HOST", LOOPBACK),
    admin_api_socket=os.environ.get(
        "TRUSTYCLAW_APP_ADMIN_API_SOCKET",
        "/run/trustyclaw-admin-api/app-backend.sock",
    ),
    setup_brief="Explain how the agent should initialize this workspace.",
    seed=seed,
)


def main() -> int:
    return workspace_kit.serve(CONFIG)


if __name__ == "__main__":
    raise SystemExit(main())
```

The host overwrites the schema, port, bind address, and admin socket in normal
operation; the environment defaults keep the backend directly testable. A
`seed(cur, now)` hook runs once in the workspace-creation transaction. It can
set the initial goal and measurement and insert disclosed schedules, artifacts,
memories, or tool-inventory rows. Seeded rows use the ordinary shared tables and
remain editable or removable through the same controls as later rows.

Most apps stop there and model domain results as artifacts. An app that needs
typed domain state adds its own tables and config hooks:

- `domain_actions` maps each agent mutation name to a `DomainAction`. Its
  context-free validator bounds exact fields and encoded bytes; its apply
  function runs in the kit's journaled transaction.
- `domain_ui_routes` adds operator routes after the shared UI router misses.
  Operator mutations should reuse the same validation and apply functions as
  agent actions.
- `domain_agent_routes` adds attributed, read-oriented agent routes after the
  shared agent router misses. Agent writes should remain actions so write
  budgets and journaling apply.
- `digest_sections` adds bounded domain context to every turn.
- `extra_connections` adds host-derived connection health without granting a
  capability.

The app UI owns its product layout but calls the shared workspace routes for
activation, messages, settings, artifacts, schedules, memories, tools, and
deactivation. It uses its own routes only for domain records. Requests go
through the `trustyclaw-app-api` parent bridge; agent-workspace files open
through `trustyclaw-app-open-file`. Agent-authored values remain data rendered
by text-safe DOM operations or the shared declarative view-block renderer.

The package includes focused backend tests in `tests/test_<app_id>_app.py` and
desktop/mobile browser coverage in `tests/apps/<app_id>/smoke.py`. The shared
Workspace Kit suite checks the embedded base migration and shared view-block
imports, and the app tests cover its seed, domain validation, routes, bounds,
and product UI. Adding an app does not require a host registry entry; manifest
discovery is the integration path.

## Turn lifecycle

Before each turn, the kit composes a bounded digest of goal, measurement,
schedules, artifact index, memories, tool inventory, and domain sections. The
host task receives that digest, the app's static agent instructions, and the
current human or schedule message.

Agent mutations use one action path. Each action has exact fields, enums,
timestamps, slugs, and encoded-byte bounds. A context-free validator runs before
the transactional apply function. A rejected action rolls back any writes its
apply made before erroring, so rejection always leaves state untouched, and the
write transaction re-verifies thread attribution under the workspace row lock,
so a write racing deactivation fails closed. Accepted and rejected actions are
journaled, and a per-turn write budget prevents an agent from flooding the
workspace.
Operator controls reuse the same validation and apply functions when they edit
the same record.

The worker polls the host task to a terminal state, records its result, and
clears every process-local run cache. Completed, failed, cancelled, lost,
orphaned, discarded, and oversized runs converge through that same cleanup.
Retry always starts a new run from durable workspace state.

## Activation and deactivation

Activation is explicit opt-in. It selects agent settings and runs the app's seed,
including any schedules disclosed on the activation screen. No resident agent
work starts before activation.

Every active Workspace Kit app has a top-level Deactivate control. Deactivation
preserves the workspace, pauses every schedule, discards queued runs, rotates
the agent thread so in-flight agents lose app write authority, and cancels or
kills active host tasks. The worker keeps requesting stop until each task is
terminal, and reactivation is blocked while a previous turn is still stopping.
Reactivation uses a fresh thread; schedules remain paused until the human
resumes them, so background spend never restarts implicitly.

## Trust and bounds

All agent-authored values are untrusted data. Views accept only the declared
block schema, escape text before inserting markup, and provide no link, image,
frame, script, or arbitrary HTML block. App frames have no direct network
access; their parent bridge accepts only the app's own API requests and an
absolute agent-workspace path for the host Files viewer. Detailed browser,
process, database, and proxy enforcement lives in [Apps](apps.md) and
[Agent App API](agent-app-api.md).

Feed content, metadata values, validation errors, terminal outputs, digests, and
list responses are clipped by encoded bytes before they cross storage or proxy
boundaries. Shared row caps and per-turn budgets bound total agent-controlled
growth.
