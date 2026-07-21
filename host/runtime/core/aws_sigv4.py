"""AWS Signature Version 4 request signing, standard library only.

The host's runtime AWS call (the STS identity attestation made by the
``read-aws-account`` root helper) signs with this module instead of an AWS
SDK, per the zero-third-party-runtime-dependency invariant. ``sign_post``
covers that single-shot POST shape.

The proxy's Bedrock re-signing uses the general half of this module. Both
harnesses emit SigV4 requests with fixed dummy values, and the proxy checks the
parsed scope before replacing the Authorization header with a signature made
from the operator's real key. ``parse_authorization`` and
``header_signature`` reproduce botocore's canonicalization byte-for-byte,
verified against pinned botocore vectors in tests, so the forwarded signature
covers the exact request the proxy inspected.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import hashlib
import hmac
import re
import urllib.parse


@dataclass(frozen=True)
class SignedRequest:
    url: str
    headers: dict[str, str]
    body: bytes


def sign_post(
    *,
    host: str,
    region: str,
    service: str,
    access_key_id: str,
    secret_access_key: str,
    body: bytes,
    content_type: str,
    extra_headers: dict[str, str] | None = None,
    now: datetime.datetime | None = None,
) -> SignedRequest:
    """A signed ``POST https://<host>/`` request. ``extra_headers`` (for
    example ``x-amz-target``) are included in the signature; header names must
    be lowercase."""
    when = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = when.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "content-type": content_type,
        "host": host,
        "x-amz-date": amz_date,
        **(extra_headers or {}),
    }
    signed_header_names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name].strip()}\n" for name in sorted(headers))
    canonical_request = "\n".join(
        ("POST", "/", "", canonical_headers, signed_header_names, payload_hash)
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        )
    )
    signing_key = _signing_key(secret_access_key, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_header_names}, Signature={signature}"
    )
    request_headers = dict(headers)
    del request_headers["host"]  # urllib sets Host from the URL
    request_headers["authorization"] = authorization
    return SignedRequest(url=f"https://{host}/", headers=request_headers, body=body)


def _signing_key(secret_access_key: str, date_stamp: str, region: str, service: str) -> bytes:
    key = hmac.new(f"AWS4{secret_access_key}".encode(), date_stamp.encode(), hashlib.sha256).digest()
    key = hmac.new(key, region.encode(), hashlib.sha256).digest()
    key = hmac.new(key, service.encode(), hashlib.sha256).digest()
    return hmac.new(key, b"aws4_request", hashlib.sha256).digest()


@dataclass(frozen=True)
class ParsedAuthorization:
    """The verifiable pieces of a SigV4 Authorization header."""

    access_key_id: str
    date_stamp: str
    region: str
    service: str
    signed_headers: tuple[str, ...]
    signature: str


_AUTHORIZATION_RE = re.compile(
    r"^AWS4-HMAC-SHA256\s+"
    r"Credential=(?P<key>[^/,\s]+)/(?P<date>\d{8})/(?P<region>[^/,\s]+)/(?P<service>[^/,\s]+)/aws4_request\s*,\s*"
    r"SignedHeaders=(?P<headers>[a-z0-9;\-]+)\s*,\s*"
    r"Signature=(?P<signature>[0-9a-f]{64})\s*$",
    re.IGNORECASE,
)


def parse_authorization(value: str) -> ParsedAuthorization | None:
    match = _AUTHORIZATION_RE.match(value.strip())
    if match is None:
        return None
    return ParsedAuthorization(
        access_key_id=match.group("key"),
        date_stamp=match.group("date"),
        region=match.group("region"),
        service=match.group("service"),
        signed_headers=tuple(match.group("headers").lower().split(";")),
        signature=match.group("signature").lower(),
    )


def header_signature(
    *,
    method: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    signed_headers: tuple[str, ...],
    payload_hash: str,
    amz_date: str,
    date_stamp: str,
    region: str,
    service: str,
    access_key_id: str,
    secret_access_key: str,
) -> tuple[str, str]:
    """The (Authorization header value, hex signature) for an arbitrary
    request over an explicit signed-header set, canonicalized the way botocore
    does it (pinned vectors in tests keep this byte-compatible)."""
    value_lists: dict[str, list[str]] = {}
    for name, value in headers:
        value_lists.setdefault(name.lower(), []).append(" ".join(value.split()))
    canonical_headers = "".join(
        f"{name}:{','.join(value_lists.get(name, ['']))}\n" for name in signed_headers
    )
    canonical_request = "\n".join(
        (
            method.upper(),
            _canonical_path(path),
            _canonical_query(query),
            canonical_headers,
            ";".join(signed_headers),
            payload_hash,
        )
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        )
    )
    signing_key = _signing_key(secret_access_key, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={';'.join(signed_headers)}, Signature={signature}"
    )
    return authorization, signature


def payload_hash_for(headers: list[tuple[str, str]], body: bytes) -> str:
    """The payload hash the signer used: the ``x-amz-content-sha256`` header
    when the SDK sent one (smithy clients), else the body's own hash (botocore
    signs the hash without emitting the header for non-S3 services)."""
    for name, value in headers:
        if name.lower() == "x-amz-content-sha256":
            return value.strip()
    return hashlib.sha256(body).hexdigest()


def _canonical_path(path: str) -> str:
    # botocore for non-S3 services: quote(normalize_url_path(path), safe="/")
    # — dot segments removed per RFC 3986, then the already-encoded wire path
    # is percent-encoded once more (the SigV4 double-encoding rule, so a wire
    # ``%3A`` canonicalizes to ``%253A``).
    return urllib.parse.quote(_remove_dot_segments(path or "/"), safe="/")


def _remove_dot_segments(path: str) -> str:
    output: list[str] = []
    for segment in path.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if output:
                output.pop()
            continue
        output.append(segment)
    if path.startswith("/") and (not output or output[0] != ""):
        output.insert(0, "")
    if output == [""] or not output:
        return "/"
    if path.endswith(("/.", "/..")):
        output.append("")
    return "/".join(output)


def _canonical_query(query: str) -> str:
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        name, _, value = part.partition("=")
        pairs.append(
            (
                urllib.parse.quote(urllib.parse.unquote(name), safe=""),
                urllib.parse.quote(urllib.parse.unquote(value), safe=""),
            )
        )
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))
