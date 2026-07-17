"""npm packages managed integration: static contract.

Read-only access to the npm registry and Node.js downloads. No guard: the
GET/HEAD-only, path-guarded rules are the whole control.
"""

from __future__ import annotations

from typing import Any

from host.network_integrations.base import (
    IntegrationManifest,
    ManagedIntegration,
    parse_simple_integration,
)

MANIFEST = IntegrationManifest(
    integration_id="npm_packages",
    display_name="npm packages",
    description=(
        "Read-only access to the npm registry (npm install) and Node.js release "
        "downloads."
    ),
    owned_apexes=("npmjs.org", "nodejs.org"),
)


def parse(raw: dict[str, Any]) -> ManagedIntegration:
    return parse_simple_integration(raw, "network_integrations.npm_packages")
