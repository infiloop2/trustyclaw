"""Operator-configured custom-domain network integration."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from host.network_integrations.base import (
    IntegrationConfigError,
    IntegrationManifest,
    reject_extra,
)

ALLOWED_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
EXACT_DOMAIN_RE = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
WILDCARD_DOMAIN_RE = re.compile(r"^\*\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")

MANIFEST = IntegrationManifest(
    integration_id="custom",
    display_name="Custom domains",
    description=(
        "Operator-configured HTTPS domains with explicit methods and optional path patterns. "
        "Use this integration when no built-in provider integration owns the destination."
    ),
    owned_apexes=(),
)


@dataclass(frozen=True)
class CustomDomainRule:
    allow_http_methods: tuple[str, ...]
    path_guards: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {"allow_http_methods": list(self.allow_http_methods)}
        if self.path_guards:
            value["path_guards"] = list(self.path_guards)
        return value


@dataclass(frozen=True)
class CustomIntegration:
    domains: dict[str, CustomDomainRule]

    @property
    def enabled(self) -> bool:
        return bool(self.domains)

    def to_json(self) -> dict[str, Any]:
        return {"domains": {domain: rule.to_json() for domain, rule in sorted(self.domains.items())}}


def parse(raw: dict[str, Any]) -> CustomIntegration:
    reject_extra(raw, {"domains"}, "network_integrations.custom")
    # Absent or null defaults to empty (a disabled custom integration), but a
    # present non-object is an operator error, not a silent policy wipe.
    raw_domains = raw.get("domains")
    if raw_domains is None:
        raw_domains = {}
    if not isinstance(raw_domains, dict):
        raise IntegrationConfigError("network_integrations.custom.domains must be an object")
    domains: dict[str, CustomDomainRule] = {}
    for domain, rule_raw in raw_domains.items():
        if not isinstance(domain, str) or not domain:
            raise IntegrationConfigError("network_integrations.custom.domains keys must be non-empty domain strings")
        if not (EXACT_DOMAIN_RE.fullmatch(domain) or WILDCARD_DOMAIN_RE.fullmatch(domain)):
            raise IntegrationConfigError(
                f"network_integrations.custom.domains[{domain!r}] must be an exact domain or wildcard like '*.example.com'"
            )
        normalized = domain.lower()
        if normalized in domains:
            raise IntegrationConfigError(
                "network_integrations.custom.domains has duplicate domain rules after lowercase normalization: "
                f"{domain!r} conflicts with {normalized!r}"
            )
        if not isinstance(rule_raw, dict):
            raise IntegrationConfigError(f"network_integrations.custom.domains[{domain!r}] must be an object")
        domains[normalized] = _parse_rule(rule_raw, normalized)
    _reject_overlapping_wildcards(domains)
    return CustomIntegration(domains)


def _parse_rule(raw: dict[str, Any], domain: str) -> CustomDomainRule:
    context = f"network_integrations.custom.domains[{domain!r}]"
    reject_extra(raw, {"allow_http_methods", "path_guards"}, context)
    methods = tuple(method.upper() for method in _string_list(raw, "allow_http_methods"))
    for method in methods:
        if method not in ALLOWED_HTTP_METHODS:
            raise IntegrationConfigError(
                f"{context}.allow_http_methods has invalid method {method!r}"
            )
    path_guards = tuple(_string_list(raw, "path_guards", required=False))
    for pattern in path_guards:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise IntegrationConfigError(
                f"{context}.path_guards invalid regex {pattern!r}: {exc}"
            ) from exc
    return CustomDomainRule(methods, path_guards)


def _string_list(raw: dict[str, Any], key: str, *, required: bool = True) -> list[str]:
    value = raw.get(key)
    if value is None and not required:
        return []
    if not isinstance(value, list):
        raise IntegrationConfigError(f"{key} must be a string array")
    values: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise IntegrationConfigError(f"{key} must be a string array")
        values.append(item.strip())
    return values


def _reject_overlapping_wildcards(rules: dict[str, CustomDomainRule]) -> None:
    wildcards = sorted(domain for domain in rules if domain.startswith("*."))
    for index, left in enumerate(wildcards):
        for right in wildcards[index + 1:]:
            if left.endswith(right[1:]) or right.endswith(left[1:]):
                raise IntegrationConfigError(
                    "network_integrations.custom.domains wildcard domains must not overlap: "
                    f"{left!r} and {right!r} can both match the same host"
                )


def domain_matches(pattern: str, host: str) -> bool:
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and host != pattern[2:]
    return host == pattern


def rule_for_host(config: CustomIntegration, host: str) -> CustomDomainRule | None:
    host = host.lower()
    exact = config.domains.get(host)
    if exact is not None:
        return exact
    wildcards = [domain for domain in config.domains if domain_matches(domain, host)]
    return config.domains[max(wildcards, key=len)] if wildcards else None
