"""Bedrock request guard, proxy re-signing, and usage-meter selection.

Pi and Hermes sign with fixed dummy AWS values that carry no provider
capability. The proxy admits only the configured region's model invocation
routes with an expected dummy access-key id and Bedrock SigV4 scope, then
re-signs the exact request body and signed headers with the active operator
credential. The operator key never enters an agent process. Query-string auth,
temporary session credentials, the Bedrock control plane, other AWS services,
and unconfigured regions fail closed.

Each harness signs with its own routing key id, so an allowed invocation also
selects a response meter attributed to that runtime (see
``host.network_integrations.bedrock.usage``).
"""

from __future__ import annotations

import re

from host.network_integrations.bedrock import usage
from host.network_integrations.bedrock.manifest import (
    BedrockIntegration,
    ROUTING_ACCESS_KEY_IDS,
    SUPPORTED_REGIONS,
    region_host,
)
from host.runtime.core import aws_sigv4
from host.runtime.core.network_policy import normalized_path, route_allowed
from host.runtime.core.state import read_bedrock_proxy_credential

# Pi and Hermes both use Bedrock Converse. Session model ids never contain a
# path separator, so one segment is enough; an encoded slash in the model
# segment normalizes into extra segments and fails closed.
_MODEL_ROUTE_RE = re.compile(r"^/model/([^/]+)/(?:converse|converse-stream)$")
ROUTES = (("POST",), (_MODEL_ROUTE_RE.pattern,))
_RUNTIMES_BY_ROUTING_KEY = {key: runtime for runtime, key in ROUTING_ACCESS_KEY_IDS.items()}
_QUERY_AUTH_RE = re.compile(r"(?:^|&)x-amz-(?:signature|credential)=", re.IGNORECASE)


def host_allowed(config: BedrockIntegration, host: str) -> bool:
    if not config.enabled:
        return False
    credential = read_bedrock_proxy_credential()
    if credential is not None:
        return host.lower() == region_host(credential[2])
    # CONNECT only selects which TLS traffic the proxy may inspect. Before an
    # operator connects a credential there is no configured region, so admit
    # the supported Bedrock hosts to the request guard. It still rejects every
    # request with bedrock_credentials_unavailable before anything reaches AWS.
    return host.lower() in {region_host(region) for region in SUPPORTED_REGIONS}


def request_denied(
    config: BedrockIntegration,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> str | None:
    """Apply the shared region, route, and signing-identity controls."""
    if _QUERY_AUTH_RE.search(query or ""):
        # Before the route check: the route patterns admit no query string at
        # all, and the presigned-auth attempt deserves its specific reason.
        return "bedrock_query_auth_denied"
    if not route_allowed(method, path, query, *ROUTES):
        return "network_policy_denied"
    header_map = {key.lower(): value for key, value in headers}
    if "x-amz-security-token" in header_map:
        return "bedrock_session_credentials_denied"
    parsed = aws_sigv4.parse_authorization(header_map.get("authorization", ""))
    if parsed is None:
        return "bedrock_signature_required"
    if parsed.access_key_id not in _RUNTIMES_BY_ROUTING_KEY:
        return "bedrock_access_key_mismatch"
    if not config.enabled:
        return "bedrock_credentials_unavailable"
    credential = read_bedrock_proxy_credential()
    if credential is None:
        # Only a synchronously validated candidate is stored.
        return "bedrock_credentials_unavailable"
    _access_key_id, _secret_access_key, region = credential
    if (
        host.lower() != region_host(region)
        or parsed.region != region
        or parsed.service.lower() != "bedrock"
    ):
        # The credential scope bounds what a signature is valid for; the proxy
        # must never mint a real-key signature scoped to another service or
        # region.
        return "bedrock_signature_invalid"
    return None


def rewrite_request_headers(
    config: BedrockIntegration,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> list[tuple[str, str]]:
    """Re-sign the already allowed dummy-credential request with the
    operator's real credential. On any inconsistency — a reset racing this
    request, an unparseable header — the request is forwarded unmodified and
    AWS rejects its worthless signature, so failure stays closed."""
    header_map = {key.lower(): value for key, value in headers}
    parsed = aws_sigv4.parse_authorization(header_map.get("authorization", ""))
    if parsed is None:
        return headers
    if parsed.access_key_id not in _RUNTIMES_BY_ROUTING_KEY:
        return headers
    credential = read_bedrock_proxy_credential()
    if credential is None:
        return headers
    access_key_id, secret_access_key, region = credential
    # The credential can be replaced between request_denied and this rewrite.
    # Re-check the fresh row before minting a real signature so an in-flight
    # request cannot be signed for the previous region with the new key.
    if (
        host.lower() != region_host(region)
        or parsed.region != region
        or parsed.service.lower() != "bedrock"
    ):
        return headers
    authorization, _signature = aws_sigv4.header_signature(
        method=method,
        path=path,
        query=query,
        headers=_with_host_header(headers, host),
        signed_headers=parsed.signed_headers,
        payload_hash=aws_sigv4.payload_hash_for(headers, body),
        amz_date=header_map.get("x-amz-date", ""),
        date_stamp=parsed.date_stamp,
        region=parsed.region,
        service=parsed.service,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
    )
    return [
        (name, authorization if name.lower() == "authorization" else value)
        for name, value in headers
    ]


def response_meter(
    config: BedrockIntegration,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> usage.BedrockResponseMeter | None:
    """The usage meter for this allowed model invocation, or None.

    Runs only after ``request_denied`` returned None, so the route and signing
    identity are already vetted; this re-reads both purely to attribute the
    response to the invoking runtime and model."""
    del config, method, host, query, body
    match = _MODEL_ROUTE_RE.match(normalized_path(path))
    if match is None:
        return None
    header_map = {key.lower(): value for key, value in headers}
    parsed = aws_sigv4.parse_authorization(header_map.get("authorization", ""))
    runtime = _RUNTIMES_BY_ROUTING_KEY.get(parsed.access_key_id) if parsed else None
    if runtime is None:
        return None
    return usage.BedrockResponseMeter(runtime, match.group(1)[:256])


def _with_host_header(headers: list[tuple[str, str]], host: str) -> list[tuple[str, str]]:
    # HTTP/1.1 requests carry Host; if a client relied on the URL for it, the
    # signed value is the target host.
    if any(name.lower() == "host" for name, _value in headers):
        return headers
    return [*headers, ("host", host)]
