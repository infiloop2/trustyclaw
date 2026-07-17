"""The network integration registry (pure: manifests only).

Hand-written, not discovered: integrations run inside the proxy with the
proxy's privileges, so adding one is a reviewed edit here and in ``runtime.py``.
Unit tests discover package manifests and validate the registry's unique ids,
disjoint apex claims, denial codes, and guard pairing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from host.network_integrations.base import (
    DenialReason,
    IntegrationConfig,
    IntegrationManifest,
    PROXY_DENIAL_REASONS,
)
from host.network_integrations.claude import manifest as claude
from host.network_integrations.custom import manifest as custom
from host.network_integrations.github import manifest as github
from host.network_integrations.npm_packages import manifest as npm_packages
from host.network_integrations.openai import manifest as openai
from host.network_integrations.python_packages import manifest as python_packages


@dataclass(frozen=True)
class RegisteredIntegration:
    """One integration's pure manifest and typed config parser."""

    manifest: IntegrationManifest
    parse: Callable[[dict[str, Any]], IntegrationConfig]


def _build_registry(modules: tuple[Any, ...]) -> dict[str, RegisteredIntegration]:
    """Collapse the manifest modules into the id-keyed registry, rejecting a
    duplicate ``integration_id`` before it could silently overwrite an earlier
    entry (and with it that entry's apex claims and guard pairing)."""
    registry: dict[str, RegisteredIntegration] = {}
    for module in modules:
        integration_id = module.MANIFEST.integration_id
        if integration_id in registry:
            raise ValueError(f"duplicate integration id {integration_id!r}")
        registry[integration_id] = RegisteredIntegration(manifest=module.MANIFEST, parse=module.parse)
    return registry


# Registry order is the operator- and agent-facing serialization order of
# ``network_integrations``; ``custom`` (the catch-all for hosts no fixed apex
# claims) is last.
INTEGRATION_MODULES = (openai, claude, github, python_packages, npm_packages, custom)
NETWORK_INTEGRATIONS: dict[str, RegisteredIntegration] = _build_registry(
    INTEGRATION_MODULES
)
def managed_domain_owner(domain: str) -> str | None:
    """The managed integration owning ``domain``, or None. A wildcard rule is
    owned both when it sits under a managed apex (``*.openai.com``) and when
    it would cover one (``*.com`` matches ``api.openai.com``), so a broad
    wildcard cannot smuggle unguarded access to managed domains. The same
    ownership drives the proxy's guard dispatch: requests to an owned host go
    to exactly that integration's guard."""
    wildcard = domain.startswith("*.")
    suffix = domain[2:] if wildcard else domain
    for integration_id, registered in NETWORK_INTEGRATIONS.items():
        for apex in registered.manifest.owned_apexes:
            if suffix == apex or suffix.endswith(f".{apex}"):
                return integration_id
            if wildcard and apex.endswith(f".{suffix}"):
                return integration_id
    return None


def denial_reason_catalog() -> dict[str, DenialReason]:
    """Every denial code the proxy can emit — core reasons plus each
    integration's — keyed by code, for guidance lookups."""
    catalog = {reason.code: reason for reason in PROXY_DENIAL_REASONS}
    for registered in NETWORK_INTEGRATIONS.values():
        for reason in registered.manifest.denial_reasons:
            catalog[reason.code] = reason
    return catalog
