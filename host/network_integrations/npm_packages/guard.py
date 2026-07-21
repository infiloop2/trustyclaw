"""Request decisions for the npm package integration."""

from __future__ import annotations

from host.network_integrations.base import ManagedIntegration, request_param_denial
from host.runtime.core.network_policy import route_allowed

ROUTES = {
    "registry.npmjs.org": (("GET", "HEAD"), ()),
    "nodejs.org": (("GET", "HEAD"), (r"^/dist(?:/.*)?$",)),
}


def host_allowed(config: ManagedIntegration, host: str) -> bool:
    del config
    return host.lower() in ROUTES


def request_denied(
    config: ManagedIntegration,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> str | None:
    del config, headers, body
    route = ROUTES.get(host.lower())
    if not route or not route_allowed(method, path, query, *route):
        return "network_policy_denied"
    if "/-/" in path:
        # Tarball URLs (/<pkg>/-/<pkg>-<version>.tgz) are provider-returned
        # after metadata resolution, not agent-authored names; date-based
        # prereleases and hash-like build ids in filenames must not deny an
        # npm install. The metadata routes below still guard the name.
        return None
    # A package name is a classic small-payload exfiltration channel; run
    # the outbound parameter guard over the decoded path and query values.
    return request_param_denial(path, query)
