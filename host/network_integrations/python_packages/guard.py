"""Request decisions for the Python package integration."""

from __future__ import annotations

from host.network_integrations.base import ManagedIntegration, request_param_denial
from host.runtime.core.network_policy import route_allowed

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
    if not route or not route_allowed(method, path, query, *route):
        return "network_policy_denied"
    if host.lower() == "files.pythonhosted.org":
        # Download URLs come from the simple-index response: their hash
        # segments and filenames are provider-echoed values, exempt like
        # GitHub's signed URLs. The agent-authored surface is the package
        # name on pypi.org, guarded below.
        return None
    # A package name is a classic small-payload exfiltration channel; run
    # the outbound parameter guard over the decoded name and query values.
    return request_param_denial(path, query)
