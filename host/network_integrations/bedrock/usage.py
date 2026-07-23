"""Passive token-usage metering for allowed Bedrock responses.

AWS reports authoritative token usage in every model invocation response:
Converse returns a JSON body with a top-level ``usage`` object, and
ConverseStream ends with a ``metadata`` event carrying the same shape inside
its vnd.amazon.eventstream framing. The proxy feeds the raw upstream response
bytes through a meter while relaying them unchanged; when the relay ends, the
meter parses the buffered response and adds one increment to the
(model, UTC day) counter row.

Metering is strictly passive and the relay is never affected: a non-200
response, an unrecognized shape, or a response larger than the metering
buffer records the request with no tokens, which surfaces as ``requests``
without ``metered_requests`` instead of vanishing from the meter.
"""

from __future__ import annotations

import json
import re
import zlib
from typing import Any

from host.network_integrations.bedrock import manifest
from host.runtime.core.state import record_bedrock_usage

# Metering buffer bound. A Converse body or ConverseStream event stream for
# the largest catalog turn (~164k output tokens) is under 2 MiB even with
# event framing; past this cap the request is recorded unmetered rather than
# holding an unbounded copy of the relay.
MAX_METERED_RESPONSE_BYTES = 32 * 1024 * 1024

_STATUS_RE = re.compile(rb"^HTTP/1\.[01] (\d{3}) ")


class BedrockResponseMeter:
    """Buffers one upstream response and records its usage when it ends."""

    def __init__(self, model_id: str) -> None:
        self._model_id = model_id
        self._buffer = bytearray()
        self._overflowed = False
        self._finished = False

    def feed(self, data: bytes) -> None:
        """Consume raw upstream bytes (status line, headers, and body)."""
        if self._overflowed:
            return
        if len(self._buffer) + len(data) > MAX_METERED_RESPONSE_BYTES:
            self._overflowed = True
            self._buffer.clear()
            return
        self._buffer.extend(data)

    def finish(self) -> None:
        """Record the request once the relay ends (normally or not).

        Usage recording is display metadata for a response that has already
        been relayed, so a database failure here is swallowed: the same outage
        denies the next request loudly at the policy load."""
        if self._finished:
            return
        self._finished = True
        usage = None if self._overflowed else _parse_usage(bytes(self._buffer))
        self._buffer.clear()
        # Price the response now and store the resulting cost: the record is
        # final, so a later rate edit does not rewrite history. An unpriced
        # model contributes 0 and collapses into the shared bucket, keeping the
        # row count bounded to the catalog.
        cost_usd = 0.0
        if usage is not None:
            cost_usd = manifest.estimate_cost_usd(
                self._model_id,
                usage["input_tokens"],
                usage["output_tokens"],
                usage["cache_read_tokens"],
                usage["cache_write_tokens"],
            ) or 0.0
        try:
            record_bedrock_usage(
                manifest.catalog_model_id(self._model_id),
                usage,
                cost_usd,
            )
        except Exception:
            pass


def _parse_usage(raw: bytes) -> dict[str, int] | None:
    """The usage counters from one raw HTTP/1.1 response, or None."""
    raw = _skip_interim_responses(raw)
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        return None
    status = _STATUS_RE.match(head)
    if status is None or status.group(1) != b"200":
        return None
    headers = _response_headers(head)
    decoded = _decoded_body(headers, body)
    if decoded is None:
        return None
    if "vnd.amazon.eventstream" in headers.get("content-type", ""):
        payload = _metadata_event_payload(decoded)
        return None if payload is None else _usage_counters(payload)
    try:
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _usage_counters(value) if isinstance(value, dict) else None


def _skip_interim_responses(raw: bytes) -> bytes:
    """Drop any leading 1xx interim responses so the final response is parsed.

    When a client sends ``Expect: 100-continue`` (the proxy does not strip
    that header) a conforming upstream may prepend ``HTTP/1.1 100 Continue``
    before the ``200 OK``. Each interim response is a status line and headers
    terminated by a blank line, with no body; skip them so a metered call is
    not misread as unmetered."""
    while True:
        status = _STATUS_RE.match(raw)
        if status is None or not status.group(1).startswith(b"1"):
            return raw
        _, separator, rest = raw.partition(b"\r\n\r\n")
        if not separator:
            return raw
        raw = rest


def _response_headers(head: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in head.split(b"\r\n")[1:]:
        name, separator, value = line.partition(b":")
        if separator:
            headers[name.strip().decode("iso-8859-1").lower()] = (
                value.strip().decode("iso-8859-1").lower()
            )
    return headers


def _decoded_body(headers: dict[str, str], body: bytes) -> bytes | None:
    if "chunked" in headers.get("transfer-encoding", ""):
        dechunked = _dechunked(body)
        if dechunked is None:
            return None
        body = dechunked
    encoding = headers.get("content-encoding", "")
    if encoding in ("", "identity"):
        return body
    if encoding in ("gzip", "deflate"):
        try:
            # wbits=47 auto-detects the gzip and zlib wrappers.
            return zlib.decompress(body, wbits=47)
        except zlib.error:
            return None
    return None


def _dechunked(body: bytes) -> bytes | None:
    decoded = bytearray()
    offset = 0
    while True:
        line_end = body.find(b"\r\n", offset)
        if line_end < 0:
            return None
        try:
            size = int(body[offset:line_end].split(b";")[0].strip() or b"0", 16)
        except ValueError:
            return None
        if size == 0:
            return bytes(decoded)
        chunk_start = line_end + 2
        if chunk_start + size > len(body):
            return None
        decoded += body[chunk_start : chunk_start + size]
        offset = chunk_start + size + 2  # CRLF after each chunk


def _metadata_event_payload(body: bytes) -> dict[str, Any] | None:
    """The JSON payload of the stream's ``metadata`` event, or None.

    vnd.amazon.eventstream framing: each message is a 12-byte prelude (total
    length, headers length, prelude CRC, all big-endian), the encoded headers,
    the payload, and a 4-byte message CRC. The metadata event arrives last;
    scanning every complete message keeps the parser free of ordering
    assumptions."""
    offset = 0
    while offset + 12 <= len(body):
        total_length = int.from_bytes(body[offset : offset + 4], "big")
        headers_length = int.from_bytes(body[offset + 4 : offset + 8], "big")
        if total_length < 16 or headers_length > total_length - 16 or offset + total_length > len(body):
            return None
        headers = _event_headers(body[offset + 12 : offset + 12 + headers_length])
        if headers is None:
            return None
        if headers.get(":event-type") == "metadata":
            payload = body[offset + 12 + headers_length : offset + total_length - 4]
            try:
                value = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return value if isinstance(value, dict) else None
        offset += total_length
    return None


# Value lengths for the fixed-size eventstream header types: bool true/false,
# byte, short, int, long, timestamp, uuid. Types 6 (bytearray) and 7 (string)
# carry a 2-byte length prefix instead.
_FIXED_HEADER_VALUE_LENGTHS = {0: 0, 1: 0, 2: 1, 3: 2, 4: 4, 5: 8, 8: 8, 9: 16}


def _event_headers(raw: bytes) -> dict[str, str] | None:
    """The string-valued headers of one eventstream message, or None when the
    header block is malformed."""
    headers: dict[str, str] = {}
    offset = 0
    while offset < len(raw):
        name_length = raw[offset]
        offset += 1
        name = raw[offset : offset + name_length]
        offset += name_length
        if offset >= len(raw):
            return None
        value_type = raw[offset]
        offset += 1
        if value_type in (6, 7):
            if offset + 2 > len(raw):
                return None
            value_length = int.from_bytes(raw[offset : offset + 2], "big")
            offset += 2
            value = raw[offset : offset + value_length]
            offset += value_length
            if value_type == 7:
                try:
                    headers[name.decode("utf-8")] = value.decode("utf-8")
                except UnicodeDecodeError:
                    return None
        elif value_type in _FIXED_HEADER_VALUE_LENGTHS:
            offset += _FIXED_HEADER_VALUE_LENGTHS[value_type]
        else:
            return None
        if offset > len(raw):
            return None
    return headers


def _usage_counters(value: dict[str, Any]) -> dict[str, int] | None:
    """The non-negative token counters from a Converse ``usage`` object."""
    usage = value.get("usage")
    if not isinstance(usage, dict):
        return None
    counters: dict[str, int] = {}
    for column, field, required in (
        ("input_tokens", "inputTokens", True),
        ("output_tokens", "outputTokens", True),
        ("cache_read_tokens", "cacheReadInputTokens", False),
        ("cache_write_tokens", "cacheWriteInputTokens", False),
    ):
        raw = usage.get(field, None if required else 0)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return None
        counters[column] = raw
    return counters
