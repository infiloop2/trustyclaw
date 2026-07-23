from __future__ import annotations

import gzip
import json
import unittest
from unittest.mock import patch

from host.network_integrations.bedrock import usage
from host.network_integrations.bedrock.manifest import MODEL_PRICING_PER_MILLION, estimate_cost_usd
from host.runtime.network_proxy import service as proxy_service
from host.session_options import SESSION_OPTIONS

USAGE = {
    "inputTokens": 1200,
    "outputTokens": 345,
    "totalTokens": 1545,
    "cacheReadInputTokens": 100,
    "cacheWriteInputTokens": 50,
}
COUNTERS = {
    "input_tokens": 1200,
    "output_tokens": 345,
    "cache_read_tokens": 100,
    "cache_write_tokens": 50,
}
# The USD the meter prices USAGE at for a deepseek response; computed with the
# same catalog function the meter uses, so the recorded value matches exactly.
COST = estimate_cost_usd("deepseek.v3.2", 1200, 345, 100, 50)


def http_response(
    body: bytes,
    *,
    status: bytes = b"200 OK",
    content_type: bytes = b"application/json",
    extra_headers: bytes = b"",
    chunked: bool = False,
) -> bytes:
    if chunked:
        framing = b"Transfer-Encoding: chunked\r\n"
        body = b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)
    else:
        framing = b"Content-Length: %d\r\n" % len(body)
    return (
        b"HTTP/1.1 %s\r\nContent-Type: %s\r\n%s%s\r\n%s"
        % (status, content_type, framing, extra_headers, body)
    )


def eventstream_message(event_type: str, payload: dict[str, object]) -> bytes:
    """One vnd.amazon.eventstream message with string headers, as AWS frames
    it (CRCs are not validated by the passive parser and are zeroed here)."""
    headers = b""
    for name, value in ((":event-type", event_type), (":content-type", "application/json"), (":message-type", "event")):
        encoded_name = name.encode()
        encoded_value = value.encode()
        headers += bytes([len(encoded_name)]) + encoded_name + b"\x07"
        headers += len(encoded_value).to_bytes(2, "big") + encoded_value
    body = json.dumps(payload).encode()
    total = 12 + len(headers) + len(body) + 4
    return total.to_bytes(4, "big") + len(headers).to_bytes(4, "big") + b"\x00" * 4 + headers + body + b"\x00" * 4


class ParseUsageTests(unittest.TestCase):
    def parse(self, raw: bytes) -> dict[str, int] | None:
        return usage._parse_usage(raw)

    def test_converse_json_usage_is_parsed(self) -> None:
        raw = http_response(json.dumps({"output": {}, "usage": USAGE}).encode())
        self.assertEqual(self.parse(raw), COUNTERS)

    def test_chunked_and_gzip_bodies_are_decoded(self) -> None:
        body = json.dumps({"usage": USAGE}).encode()
        self.assertEqual(self.parse(http_response(body, chunked=True)), COUNTERS)
        self.assertEqual(
            self.parse(
                http_response(
                    gzip.compress(body), extra_headers=b"Content-Encoding: gzip\r\n"
                )
            ),
            COUNTERS,
        )

    def test_converse_stream_metadata_event_is_parsed(self) -> None:
        stream = (
            eventstream_message("messageStart", {"role": "assistant"})
            + eventstream_message("contentBlockDelta", {"delta": {"text": "hi"}})
            + eventstream_message("messageStop", {"stopReason": "end_turn"})
            + eventstream_message("metadata", {"usage": USAGE, "metrics": {"latencyMs": 5}})
        )
        raw = http_response(
            stream, content_type=b"application/vnd.amazon.eventstream", chunked=True
        )
        self.assertEqual(self.parse(raw), COUNTERS)

    def test_missing_cache_counters_default_to_zero(self) -> None:
        raw = http_response(
            json.dumps({"usage": {"inputTokens": 5, "outputTokens": 3}}).encode()
        )
        self.assertEqual(
            self.parse(raw),
            {"input_tokens": 5, "output_tokens": 3, "cache_read_tokens": 0, "cache_write_tokens": 0},
        )

    def test_unusable_responses_yield_no_usage(self) -> None:
        error_body = json.dumps({"message": "throttled"}).encode()
        for raw in (
            http_response(error_body, status=b"429 Too Many Requests"),
            http_response(b"not json"),
            http_response(json.dumps({"usage": {"inputTokens": "many", "outputTokens": 1}}).encode()),
            http_response(json.dumps({"usage": {"inputTokens": -1, "outputTokens": 1}}).encode()),
            http_response(json.dumps({"outputs": []}).encode()),
            http_response(b"\xff\xfe", content_type=b"application/vnd.amazon.eventstream"),
            b"HTTP/1.1 200 OK\r\nContent-Length: 4",  # head never completed
        ):
            self.assertIsNone(self.parse(raw), raw[:60])

    def test_stream_without_a_metadata_event_yields_no_usage(self) -> None:
        stream = eventstream_message("messageStop", {"stopReason": "end_turn"})
        raw = http_response(stream, content_type=b"application/vnd.amazon.eventstream")
        self.assertIsNone(self.parse(raw))

    def test_leading_100_continue_is_skipped_before_the_final_response(self) -> None:
        # An upstream may prepend a 1xx interim response when the client sent
        # Expect: 100-continue; the final 200 must still be metered.
        raw = b"HTTP/1.1 100 Continue\r\n\r\n" + http_response(
            json.dumps({"usage": USAGE}).encode()
        )
        self.assertEqual(self.parse(raw), COUNTERS)


class BedrockResponseMeterTests(unittest.TestCase):
    def test_finish_records_parsed_usage_once(self) -> None:
        meter = usage.BedrockResponseMeter("deepseek.v3.2")
        raw = http_response(json.dumps({"usage": USAGE}).encode())
        with patch.object(usage, "record_bedrock_usage") as record:
            meter.feed(raw[:20])
            meter.feed(raw[20:])
            meter.finish()
            meter.finish()  # idempotent: one relay records once
        record.assert_called_once_with("deepseek.v3.2", COUNTERS, COST)

    def test_unparsed_response_still_counts_the_request(self) -> None:
        meter = usage.BedrockResponseMeter("qwen.qwen3-coder-next")
        with patch.object(usage, "record_bedrock_usage") as record:
            meter.feed(http_response(b"boom", status=b"500 Internal Server Error"))
            meter.finish()
        record.assert_called_once_with("qwen.qwen3-coder-next", None, 0.0)

    def test_unknown_model_records_under_the_other_bucket_at_zero_cost(self) -> None:
        # A model outside the catalog collapses into 'other' (bounding the row
        # count) and is unpriced, so its tokens still count but cost is 0.
        meter = usage.BedrockResponseMeter("some.unlisted-model")
        raw = http_response(json.dumps({"usage": USAGE}).encode())
        with patch.object(usage, "record_bedrock_usage") as record:
            meter.feed(raw)
            meter.finish()
        record.assert_called_once_with("other", COUNTERS, 0.0)

    def test_oversized_response_is_dropped_not_buffered(self) -> None:
        meter = usage.BedrockResponseMeter("deepseek.v3.2")
        with patch.object(usage, "MAX_METERED_RESPONSE_BYTES", 64):
            meter.feed(b"x" * 65)
            self.assertEqual(len(meter._buffer), 0)
            with patch.object(usage, "record_bedrock_usage") as record:
                meter.finish()
        record.assert_called_once_with("deepseek.v3.2", None, 0.0)

    def test_a_recording_failure_never_escapes_finish(self) -> None:
        meter = usage.BedrockResponseMeter("deepseek.v3.2")
        with patch.object(usage, "record_bedrock_usage", side_effect=RuntimeError("db down")):
            meter.finish()  # must not raise: the response was already relayed


class PricingTests(unittest.TestCase):
    def test_every_catalog_model_has_a_hardcoded_rate(self) -> None:
        # A catalog model missing from the price table would silently record
        # $0 cost for its traffic; pin the two lists together.
        catalog = set(SESSION_OPTIONS["hermes"])
        self.assertEqual(catalog, set(MODEL_PRICING_PER_MILLION))

    def test_estimate_prices_input_output_and_conservative_cache(self) -> None:
        # deepseek.v3.2: $0.62/M input, $1.85/M output; cached tokens (zero
        # today — no catalog model supports Bedrock prompt caching) would be
        # priced at the input rate, never silently free.
        cost = estimate_cost_usd("deepseek.v3.2", 1_000_000, 100_000, 0, 0)
        assert cost is not None
        self.assertAlmostEqual(cost, 0.805)
        cached = estimate_cost_usd("deepseek.v3.2", 1_000_000, 100_000, 500_000, 500_000)
        assert cached is not None
        self.assertAlmostEqual(cached, 0.805 + 0.62)
        self.assertIsNone(estimate_cost_usd("unlisted.model", 1, 1, 0, 0))


class _Socket:
    """recv/sendall/close over canned bytes for the relay loop."""

    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self._chunks = list(chunks or [])
        self.sent = b""
        self.closed = False

    def recv(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def close(self) -> None:
        self.closed = True


class ForwardUntilCloseMeterTests(unittest.TestCase):
    def test_relay_feeds_the_meter_and_finishes_after_close(self) -> None:
        raw = http_response(json.dumps({"usage": USAGE}).encode())
        upstream = _Socket([raw[:33], raw[33:]])
        client = _Socket()
        meter = usage.BedrockResponseMeter("deepseek.v3.2")
        with patch.object(usage, "record_bedrock_usage") as record:
            proxy_service.forward_until_close(upstream, client, meter)  # type: ignore[arg-type]
        self.assertEqual(client.sent, raw)  # the relayed bytes are untouched
        self.assertTrue(upstream.closed)
        record.assert_called_once_with("deepseek.v3.2", COUNTERS, COST)

    def test_an_aborted_relay_still_counts_the_request(self) -> None:
        class Failing(_Socket):
            def sendall(self, data: bytes) -> None:
                raise OSError("client went away")

        upstream = _Socket([b"HTTP/1.1 200 OK\r\n"])
        meter = usage.BedrockResponseMeter("deepseek.v3.2")
        with patch.object(usage, "record_bedrock_usage") as record:
            with self.assertRaises(OSError):
                proxy_service.forward_until_close(upstream, Failing(), meter)  # type: ignore[arg-type]
        record.assert_called_once_with("deepseek.v3.2", None, 0.0)

    def test_relay_without_a_meter_is_unchanged(self) -> None:
        upstream = _Socket([b"data"])
        client = _Socket()
        proxy_service.forward_until_close(upstream, client)  # type: ignore[arg-type]
        self.assertEqual(client.sent, b"data")


if __name__ == "__main__":
    unittest.main()
