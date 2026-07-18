"""workspace_kit: the shared engine behind resident-workspace apps.

This is an importable package (like ``host.runtime.core.db``), not an installed app:
it has no ``manifest.json``, so the app-platform validator skips it. An app's
``backend.py`` builds one ``WorkspaceAppConfig`` and calls ``serve(config)``.

See docs/architecture/apps/mission-pursuit.md for the reference app and
docs/architecture/apps/apps.md for the platform contract.
"""

from __future__ import annotations

from host.apps.workspace_kit.config import (
    AgentRouteHook,
    DigestSectionsHook,
    DomainAction,
    SeedHook,
    UiRouteHook,
    WorkspaceAppConfig,
)
from host.apps.workspace_kit.server import serve

__all__ = [
    "AgentRouteHook",
    "DigestSectionsHook",
    "DomainAction",
    "SeedHook",
    "UiRouteHook",
    "WorkspaceAppConfig",
    "serve",
]
