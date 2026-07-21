"""Unit tests for the Polymarket tool package (all third-party calls mocked)."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from host.tools.json_types import JSONObject
from host.tools.results import ActionExecuted, ActionFailed
from host.tools import polymarket
from host.tools.polymarket import PolymarketTool
from host.tools.shared.web import WebRequestError

from test_tools import FakeHostAPI


MARKET_RECORD: JSONObject = {
    "id": "512329",
    "question": "Will it rain in NYC tomorrow?",
    "slug": "will-it-rain-nyc",
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0.34", "0.66"]',
    "clobTokenIds": '["1234", "5678"]',
    "volume24hr": 12345.6,
    "liquidityNum": 999.5,
    "endDate": "2026-08-01T00:00:00Z",
    "active": True,
    "closed": False,
    "description": "Resolves YES if measurable rain falls.",
}


class PolymarketToolTests(unittest.TestCase):
    def test_manifest_is_read_only_and_enable_only(self) -> None:
        tool = PolymarketTool()
        self.assertEqual(tool.manifest.connection, "enable_only")
        self.assertIsNone(tool.credentials)
        self.assertEqual(tool.manifest.config, ())
        self.assertEqual(
            [spec.id for spec in tool.manifest.actions],
            ["list_markets", "list_events", "search", "get_market", "get_order_book", "price_history"],
        )

    def test_list_markets_filters_and_maps_fields(self) -> None:
        calls: list[str] = []
        headers: list[dict[str, str]] = []

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            calls.append(url)
            headers.append(kwargs["headers"])
            return {"items": [MARKET_RECORD, "not-a-dict"]}

        with patch.object(polymarket, "json_request", fake_json_request):
            result = PolymarketTool().execute("list_markets", {"limit": 5}, FakeHostAPI())
        self.assertIsInstance(result, ActionExecuted)
        assert isinstance(result, ActionExecuted)
        self.assertEqual(len(calls), 1)
        self.assertIn("gamma-api.polymarket.com/markets?", calls[0])
        self.assertIn("limit=5", calls[0])
        self.assertIn("active=true", calls[0])
        self.assertIn("closed=false", calls[0])
        self.assertIn("order=volume24hr", calls[0])
        self.assertEqual(headers, [polymarket.POLYMARKET_REQUEST_HEADERS])
        markets = result.result["markets"]
        assert isinstance(markets, list)
        self.assertEqual(len(markets), 1)
        market = markets[0]
        assert isinstance(market, dict)
        self.assertEqual(market["id"], "512329")
        self.assertEqual(market["question"], "Will it rain in NYC tomorrow?")
        self.assertEqual(market["volume_24h"], 12345.6)
        self.assertEqual(market["liquidity"], 999.5)

    def test_list_markets_include_closed_drops_active_filter(self) -> None:
        seen: dict[str, str] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["url"] = url
            return {"items": []}

        with patch.object(polymarket, "json_request", fake_json_request):
            result = PolymarketTool().execute("list_markets", {"include_closed": True}, FakeHostAPI())
        self.assertIsInstance(result, ActionExecuted)
        self.assertNotIn("active=", seen["url"])

    def test_listings_enforce_requested_limit_on_provider_response(self) -> None:
        records = [dict(MARKET_RECORD, id=str(index)) for index in range(5)]
        with patch.object(polymarket, "json_request", return_value={"items": records}):
            markets = PolymarketTool().execute("list_markets", {"limit": "2"}, FakeHostAPI())
            events = PolymarketTool().execute("list_events", {"limit": "2"}, FakeHostAPI())
        assert isinstance(markets, ActionExecuted)
        assert isinstance(events, ActionExecuted)
        self.assertEqual(len(markets.result["markets"]), 2)
        self.assertEqual(len(events.result["events"]), 2)

    def test_bounded_inputs_are_rejected_instead_of_silently_changed(self) -> None:
        tool = PolymarketTool()
        for action, tool_input in (
            ("list_markets", {"limit": "101"}),
            ("list_events", {"offset": "10001"}),
            ("search", {"query": "x" * 501}),
            ("get_market", {"market_id": "x" * 201}),
        ):
            with self.subTest(action=action, tool_input=tool_input):
                self.assertIsInstance(tool.execute(action, tool_input, FakeHostAPI()), ActionFailed)

    def test_list_markets_rejects_unknown_fields(self) -> None:
        result = PolymarketTool().execute("list_markets", {"unexpected": 1}, FakeHostAPI())
        self.assertIsInstance(result, ActionFailed)
        assert isinstance(result, ActionFailed)
        self.assertIn("only supports", result.error)

    def test_search_requires_query(self) -> None:
        result = PolymarketTool().execute("search", {}, FakeHostAPI())
        self.assertIsInstance(result, ActionFailed)
        assert isinstance(result, ActionFailed)
        self.assertIn("query", result.error)

    def test_search_maps_events_and_markets(self) -> None:
        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            self.assertIn("public-search", url)
            self.assertIn("q=fed+rate", url)
            return {
                "events": [{"id": "9", "title": "Fed decision", "slug": "fed", "markets": [MARKET_RECORD]}],
                "pagination": {"hasMore": False, "totalResults": 1},
            }

        with patch.object(polymarket, "json_request", fake_json_request):
            result = PolymarketTool().execute("search", {"query": "fed rate"}, FakeHostAPI())
        assert isinstance(result, ActionExecuted)
        events = result.result["events"]
        assert isinstance(events, list)
        event = events[0]
        assert isinstance(event, dict)
        self.assertEqual(event["title"], "Fed decision")
        nested_markets = event["markets"]
        assert isinstance(nested_markets, list)
        self.assertEqual(len(nested_markets), 1)
        markets = result.result["markets"]
        assert isinstance(markets, list)
        self.assertEqual([market["id"] for market in markets if isinstance(market, dict)], ["512329"])

    def test_get_market_requires_exactly_one_selector(self) -> None:
        tool = PolymarketTool()
        for tool_input in ({}, {"market_id": "1", "slug": "s"}):
            result = tool.execute("get_market", tool_input, FakeHostAPI())
            self.assertIsInstance(result, ActionFailed)

    def test_get_market_by_slug(self) -> None:
        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            self.assertIn("slug=will-it-rain-nyc", url)
            return {"items": [MARKET_RECORD]}

        with patch.object(polymarket, "json_request", fake_json_request):
            result = PolymarketTool().execute("get_market", {"slug": "will-it-rain-nyc"}, FakeHostAPI())
        assert isinstance(result, ActionExecuted)
        market = result.result["market"]
        assert isinstance(market, dict)
        self.assertEqual(market["description"], "Resolves YES if measurable rain falls.")

    def test_get_market_by_id_enforces_numeric_grammar(self) -> None:
        captured: dict[str, str] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            captured["url"] = url
            return dict(MARKET_RECORD)

        # Gamma ids are integers: an id carrying path/query characters is
        # rejected before any request (stricter than the old behavior of
        # percent-encoding it into one segment), and a numeric id still
        # lands as a single quoted path segment.
        result = PolymarketTool().execute("get_market", {"market_id": "512329/../events?x=1"}, FakeHostAPI())
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("numeric", result.error)
        with patch.object(polymarket, "json_request", fake_json_request):
            result = PolymarketTool().execute("get_market", {"market_id": "512329"}, FakeHostAPI())
        assert isinstance(result, ActionExecuted)
        self.assertIn("/markets/512329", captured["url"])

    def test_get_order_book_validates_token_and_maps_levels(self) -> None:
        result = PolymarketTool().execute("get_order_book", {"token_id": "not-digits"}, FakeHostAPI())
        self.assertIsInstance(result, ActionFailed)

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/book" in url:
                return {"bids": [{"price": "0.32", "size": "100"}], "asks": [{"price": "0.36", "size": "50"}]}
            return {"mid": "0.34"}

        with patch.object(polymarket, "json_request", fake_json_request):
            result = PolymarketTool().execute("get_order_book", {"token_id": "1234"}, FakeHostAPI())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["midpoint"], "0.34")
        bids = result.result["bids"]
        assert isinstance(bids, list)
        self.assertEqual(bids[0], {"price": "0.32", "size": "100"})

    def test_numeric_inputs_reject_unicode_digits_with_curated_errors(self) -> None:
        for action, tool_input in (
            ("list_markets", {"limit": "²"}),
            ("get_order_book", {"token_id": "²"}),
            ("price_history", {"token_id": "²"}),
        ):
            with self.subTest(action=action):
                result = PolymarketTool().execute(action, tool_input, FakeHostAPI())
                assert isinstance(result, ActionFailed)
                self.assertNotIn("invalid literal", result.error)

    def test_price_history_validates_interval_and_maps_points(self) -> None:
        result = PolymarketTool().execute("price_history", {"token_id": "1234", "interval": "2y"}, FakeHostAPI())
        self.assertIsInstance(result, ActionFailed)

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            self.assertIn("prices-history", url)
            self.assertIn("interval=1w", url)
            return {"history": [{"t": 1751000000, "p": 0.31}, {"t": "bad", "p": 0.32}]}

        with patch.object(polymarket, "json_request", fake_json_request):
            result = PolymarketTool().execute("price_history", {"token_id": "1234", "interval": "1w"}, FakeHostAPI())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["history"], [{"t": 1751000000, "p": 0.31}])

    def test_rate_limit_maps_to_specific_error(self) -> None:
        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            raise WebRequestError("Polymarket market listing request failed.", status=429)

        with patch.object(polymarket, "json_request", fake_json_request):
            result = PolymarketTool().execute("list_markets", {}, FakeHostAPI())
        assert isinstance(result, ActionFailed)
        self.assertEqual(result.error, "Polymarket API rate limit was reached.")

    def test_unsupported_action_and_no_approvals(self) -> None:
        tool = PolymarketTool()
        result = tool.execute("place_order", {}, FakeHostAPI())
        self.assertIsInstance(result, ActionFailed)
        approved = tool.execute_approved("approval-1", FakeHostAPI())
        self.assertIsInstance(approved, ActionFailed)


if __name__ == "__main__":
    unittest.main()
