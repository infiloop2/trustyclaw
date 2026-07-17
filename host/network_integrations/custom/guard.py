"""Request decisions for operator-configured custom domains."""

from __future__ import annotations

from host.network_integrations.custom.manifest import CustomIntegration, rule_for_host
from host.runtime.network_policy import route_allowed


def host_allowed(config: CustomIntegration, host: str) -> bool:
    rule = rule_for_host(config, host)
    return bool(rule and rule.allow_http_methods)


def request_denied(
    config: CustomIntegration,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> str | None:
    del headers, body
    rule = rule_for_host(config, host)
    if rule is None or not route_allowed(
        method, path, query, rule.allow_http_methods, rule.path_guards
    ):
        return "network_policy_denied"
    return None
