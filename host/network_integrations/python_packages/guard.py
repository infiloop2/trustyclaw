"""Request decisions for the Python package integration."""

from __future__ import annotations

from host.network_integrations.base import ManagedIntegration
from host.runtime.network_policy import route_allowed

ROUTES = {
    "pypi.org": (("GET", "HEAD"), (r"^/simple(?:/.*)?$", r"^/pypi/[^/]+/json$")),
    "files.pythonhosted.org": (("GET", "HEAD"), (r"^/packages(?:/.*)?$",)),
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
    return None if route and route_allowed(method, path, query, *route) else "network_policy_denied"
