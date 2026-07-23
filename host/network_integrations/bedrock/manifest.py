"""AWS Bedrock used by the Hermes runtime.

Bedrock is one operator-facing network integration: one enablement, region,
credential, account, billing record, and status. Enabling it makes Hermes
available as a task runtime.

The agent receives only a fixed dummy AWS identity. The proxy enforces the
configured Bedrock host, region, service, and invocation routes, then re-signs
an allowed request with the operator credential, which never enters an agent
process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from host.network_integrations.base import (
    DenialReason,
    IntegrationConfigError,
    IntegrationManifest,
    reject_extra,
)

# The regions a harness integration can be configured for. The session model
# catalogs use us.-prefixed cross-region inference profiles and US-only
# serverless model ids, so only US commercial regions are supported; adding a
# region is a reviewed edit here (owned apexes stay in lockstep via the
# derivation below).
SUPPORTED_REGIONS = ("us-east-1", "us-east-2", "us-west-2")


def region_host(region: str) -> str:
    return f"bedrock-runtime.{region}.amazonaws.com"


# Fixed dummy values handed to Hermes's Bedrock SDK. AKIA format keeps the SDK
# on its ordinary long-term-key path. They carry no AWS
# capability; the proxy replaces their signature only after the request
# passes the Bedrock guard.
ROUTING_ACCESS_KEY_ID = "AKIATRUSTYCLAWHERMES"
ROUTING_SECRET_ACCESS_KEY = "trustyclaw-bedrock-dummy-secret"

# Hardcoded on-demand Bedrock inference rates for the session model catalog,
# in USD per one million (input, output) tokens: US-region serverless pricing
# for the supported regions (us-east-1, us-east-2, us-west-2), checked 2026-07.
# The proxy prices each metered response with these rates when it records it
# and stores the resulting cost, so editing a rate only affects subsequently
# metered requests -- recorded cost is final. Like SUPPORTED_REGIONS, a rate
# change or a new catalog model is a reviewed edit here.
MODEL_PRICING_PER_MILLION: dict[str, tuple[float, float]] = {
    "deepseek.v3.2": (0.62, 1.85),
    "qwen.qwen3-coder-next": (0.50, 1.20),
    "moonshotai.kimi-k2.5": (0.60, 3.00),
}

# Where usage for a model outside the price table is recorded. Collapsing
# unknown ids into one bucket keeps bedrock_usage bounded to the catalog even
# if an agent invokes arbitrary model segments; its tokens still count, but it
# is unpriced so it contributes 0 cost.
UNKNOWN_MODEL_BUCKET = "other"


def catalog_model_id(model_id: str) -> str:
    """The model id to record usage under: the id itself when it is in the
    price table, otherwise the shared unknown-model bucket."""
    return model_id if model_id in MODEL_PRICING_PER_MILLION else UNKNOWN_MODEL_BUCKET


def estimate_cost_usd(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> float | None:
    """The USD estimate for one usage total, or None for an unpriced model.

    Bedrock prompt caching covers only the Claude and Nova families, none of
    which is in this catalog, so the cached-token counters stay zero today.
    Should AWS ever report them for a catalog model, they are priced at the
    full input rate — a conservative over-estimate — rather than silently
    free; a discounted cache rate would be a reviewed edit here."""
    rates = MODEL_PRICING_PER_MILLION.get(model_id)
    if rates is None:
        return None
    input_rate, output_rate = rates
    return (
        (input_tokens + cache_read_tokens + cache_write_tokens) * input_rate
        + output_tokens * output_rate
    ) / 1_000_000


MANIFEST = IntegrationManifest(
    integration_id="bedrock",
    display_name="AWS Bedrock",
    description=(
        "AWS Bedrock inference used by the Hermes task runtime. It owns the "
        "Bedrock Runtime hosts and admits only model invocation requests addressed to "
        "the configured region with the fixed dummy SDK identity. The proxy re-signs "
        "allowed requests with the operator's connected IAM key."
    ),
    owned_apexes=tuple(region_host(region) for region in SUPPORTED_REGIONS),
    denial_reasons=(
        DenialReason(
            "bedrock_credentials_unavailable",
            "The Hermes connection has no active operator-connected AWS credential (not "
            "connected yet, or its last validation failed), so Bedrock requests fail closed. "
            "Ask the operator to connect or refresh an AWS access key under AWS Bedrock AI "
            "inference in the admin UI.",
        ),
        DenialReason(
            "bedrock_signature_required",
            "Requests to Bedrock must carry an AWS Signature Version 4 Authorization header. "
            "Use the managed Hermes runtime, which supplies the dummy SDK identity "
            "automatically.",
        ),
        DenialReason(
            "bedrock_access_key_mismatch",
            "The request is not signed with Hermes's Bedrock routing identity, so no "
            "integration authorizes it. Bedrock is reachable only through the managed "
            "Hermes runtime, which signs with a fixed routing key; other AWS "
            "credentials are denied.",
        ),
        DenialReason(
            "bedrock_signature_invalid",
            "The request's SigV4 scope does not match the configured Bedrock region and "
            "service. Use the managed Hermes runtime unmodified.",
        ),
        DenialReason(
            "bedrock_query_auth_denied",
            "Query-string (presigned) AWS authentication is denied on this host because it "
            "bypasses the Authorization-header signing controls. Sign requests with the "
            "Authorization header instead.",
        ),
        DenialReason(
            "bedrock_session_credentials_denied",
            "Temporary AWS session credentials (X-Amz-Security-Token) are denied on this host. "
            "Only Hermes's Authorization-header signing is accepted.",
        ),
    ),
)


@dataclass(frozen=True)
class BedrockIntegration:
    """The one Bedrock provider/network configuration."""

    enabled: bool

    def to_json(self) -> dict[str, Any]:
        return {"enabled": self.enabled}


def parse(raw: dict[str, Any]) -> BedrockIntegration:
    if not raw:
        return BedrockIntegration(False)
    context = "network_integrations.bedrock"
    reject_extra(raw, {"enabled"}, context)
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise IntegrationConfigError(f"{context}.enabled must be true or false")
    return BedrockIntegration(enabled=enabled)
