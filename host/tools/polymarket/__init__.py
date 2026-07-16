"""Polymarket tool package (read-only market data)."""

from __future__ import annotations

import urllib.parse
from typing import cast

from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import ActionSpec, DataSummary, DataSummaryCard, DataSummaryLink, DataSummaryPoint, SetupStep, ToolManifest
from host.tools.results import ActionExecuted, ActionFailed, ActionResult, ApprovalResult
from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.shared.web import WebRequestError, encode_query, json_request

GAMMA_API_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_API_BASE_URL = "https://clob.polymarket.com"
POLYMARKET_REQUEST_HEADERS = {
    "accept": "*/*",
    # Polymarket's official API client supplies an explicit client identity.
    # Do the same rather than relying on urllib's commonly blocked default.
    "user-agent": "trustyclaw",
}
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_QUERY_CHARS = 200
MAX_TEXT_CHARS = 1_500
POLYMARKET_READ_POLICY = (
    "Read-only. Sends only the listed query parameters to Polymarket's public "
    "market-data APIs (the APIs are unauthenticated, so there is no account identity or "
    "credential to send) and returns public market data into active model context. Runs directly with no "
    "approval."
)


class ToolInputValidationError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _schema(properties: JSONObject, required: list[str] | None = None) -> JSONObject:
    schema: JSONObject = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = cast(list[JSONValue], required)
    return schema


POLYMARKET_OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}},
    "additionalProperties": True,
}


MANIFEST = ToolManifest(
    tool_id="polymarket",
    display_name="Polymarket",
    description="Lets your agent browse and search prediction markets and events on Polymarket and read prices. Trading is not available.",
    connection="enable_only",
    actions=(
        ActionSpec(id="list_markets",
            description="List a flat set of individual tradable questions, defaulting to active markets ranked by 24-hour volume. Each summary includes outcomes, current prices, and outcome token ids for get_order_book/price_history; use list_events when related questions should stay grouped.",
            data_policy=POLYMARKET_READ_POLICY,
            input_schema=_schema(
                {
                    "limit": {"type": "string", "description": "Page size, 1-100 (default 20)."},
                    "offset": {"type": "string", "description": "Listing offset (default 0)."},
                    "order": {"type": "string", "enum": ["volume24hr", "volume", "liquidity", "startDate", "endDate"], "description": "Descending sort field (default volume24hr)."},
                    "include_closed": {"type": "boolean", "description": "Include inactive and resolved markets instead of active/open only (default false)."},
                }
            ),
            output_schema=POLYMARKET_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="list_events",
            description="List umbrella topics that group related tradable markets, defaulting to active events ranked by 24-hour volume. Each event carries aggregate metadata and up to 10 nested market summaries; use list_markets for a flat ranking of individual questions.",
            data_policy=POLYMARKET_READ_POLICY,
            input_schema=_schema(
                {
                    "limit": {"type": "string", "description": "Page size, 1-100 (default 20)."},
                    "offset": {"type": "string", "description": "Listing offset (default 0)."},
                    "order": {"type": "string", "enum": ["volume24hr", "volume", "liquidity", "startDate", "endDate"], "description": "Descending sort field (default volume24hr)."},
                    "include_closed": {"type": "boolean", "description": "Include inactive and resolved events instead of active/open only (default false)."},
                }
            ),
            output_schema=POLYMARKET_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="search",
            description="Search Polymarket by public keyword and return both umbrella events and individual tradable markets in separate arrays. Use an event to preserve related questions, or a market's outcome token ids for price/order-book reads.",
            data_policy=POLYMARKET_READ_POLICY,
            input_schema=_schema({"query": {"type": "string", "description": "Public topic or question keywords, up to 200 characters."}, "limit_per_type": {"type": "string", "description": "Maximum events and maximum markets returned separately, 1-50 each (default 10)."}}, ["query"]),
            output_schema=POLYMARKET_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="get_market",
            description="Read one individual tradable market by exactly one Gamma market id or slug. Returns its question, outcomes, current prices, and outcome-specific CLOB token ids used by get_order_book and price_history.",
            data_policy=POLYMARKET_READ_POLICY,
            input_schema=_schema({"market_id": {"type": "string", "description": "Gamma market id; mutually exclusive with slug."}, "slug": {"type": "string", "description": "Market URL slug; mutually exclusive with market_id."}}),
            output_schema=POLYMARKET_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="get_order_book",
            description="Read the top 10 bids, top 10 asks, and midpoint for one outcome token within a market. Pass a decimal token id from a market's clob_token_ids, not the market or event id.",
            data_policy=POLYMARKET_READ_POLICY,
            input_schema=_schema({"token_id": {"type": "string", "description": "Decimal CLOB outcome token id from get_market, list_markets, or an event's nested market."}}, ["token_id"]),
            output_schema=POLYMARKET_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="price_history",
            description="Read up to 200 timestamp/price points for one outcome token over a selected interval. Pass a decimal token id from a market's clob_token_ids; this is outcome price history, not aggregate market volume.",
            data_policy=POLYMARKET_READ_POLICY,
            input_schema=_schema(
                {
                    "token_id": {"type": "string", "description": "Decimal CLOB outcome token id from a market summary."},
                    "interval": {"type": "string", "enum": ["1h", "6h", "1d", "1w", "1m", "max"], "description": "History window (default 1d)."},
                },
                ["token_id"],
            ),
            output_schema=POLYMARKET_OUTPUT_SCHEMA,
        ),
    ),
    protections=(
        "All actions use unauthenticated public market-data GET endpoints. The package has no wallet, private key, token, order, approval, or trading action.",
        "Queries, pagination, result sizes, order-book depth, and history points are bounded; provider payloads are normalized before entering model context.",
    ),
    setup_steps=(
        SetupStep(
            title="Enable Polymarket",
            description="Enable the Polymarket bundled tool in Internet Access and Tools. No Polymarket account, wallet, API key, OAuth connection, or provider-side setup is required.",
            link_url="https://docs.polymarket.com/",
            link_label="Review Polymarket API documentation",
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                description=(
                    "Only public query parameters: listing and search keywords, market ids or slugs, outcome token ids, sort, "
                    "pagination, and interval values, plus standard web request metadata. There is no account, wallet, or "
                    "credential to send, and nothing else on this host is sent."
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(label="Polymarket", text="Requests go to Polymarket's public Gamma and CLOB market-data services and the infrastructure providers that serve them."),
                    DataSummaryPoint(label="Request logs", text="Search keywords and ids are received and logged like any other web request, so query text is itself data sent to Polymarket."),
                ),
            ),
            DataSummaryCard(
                title="What Polymarket can do with it",
                description=(
                    "Polymarket processes requests and ordinary request metadata (source IP, time, User-Agent) under its privacy "
                    "policy to operate, secure, and analyze the service and to meet legal duties. This tool sends no wallet or "
                    "account data, so requests are not tied to any Polymarket account."
                ),
                links=(
                    DataSummaryLink(label="Polymarket privacy policy", url="https://polymarket.com/privacy"),
                    DataSummaryLink(label="Polymarket API documentation", url="https://docs.polymarket.com/"),
                ),
            ),
            DataSummaryCard(
                title="How long Polymarket retains it",
                description="Polymarket's privacy policy states no fixed retention period for request logs; it keeps data as long as needed for the purposes it lists.",
                links=(
                    DataSummaryLink(label="Polymarket privacy policy", url="https://polymarket.com/privacy"),
                ),
            ),
        ),
    ),
)


def _string_field(tool_input: JSONObject, key: str, *, max_chars: int, required: bool = False) -> str:
    value = tool_input.get(key)
    if value is None:
        if required:
            raise ToolInputValidationError(f"Polymarket tool_input.{key} is required.")
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ToolInputValidationError(f"Polymarket tool_input.{key} must be a non-empty string.")
    value = value.strip()
    if len(value) > max_chars:
        raise ToolInputValidationError(
            f"Polymarket tool_input.{key} must be at most {max_chars} characters."
        )
    return value


def _int_field(tool_input: JSONObject, key: str, *, default: int, low: int, high: int) -> int:
    """Accept a digit string (or raw int) and reject out-of-range values."""
    value = tool_input.get(key)
    if value is None:
        return default
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        digits = value.strip()
        if len(digits) > 10:
            raise ToolInputValidationError(
                f"Polymarket tool_input.{key} must be between {low} and {high}."
            )
        value = int(digits)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolInputValidationError(f"Polymarket tool_input.{key} must be an integer or digit string.")
    if not low <= value <= high:
        raise ToolInputValidationError(
            f"Polymarket tool_input.{key} must be between {low} and {high}."
        )
    return value


def _reject_unknown_fields(tool_input: JSONObject, allowed: frozenset[str]) -> None:
    extra = set(tool_input) - allowed
    if extra:
        raise ToolInputValidationError(f"Polymarket tool input only supports {', '.join(sorted(allowed))}.")


def _clip(value: JSONValue, max_chars: int = MAX_TEXT_CHARS) -> str:
    return value.strip()[:max_chars] if isinstance(value, str) else ""


def _number(value: JSONValue) -> JSONValue:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _gamma_request(path: str, params: dict[str, str], *, what: str) -> JSONObject:
    return json_request(
        "GET",
        f"{GAMMA_API_BASE_URL}{path}?{encode_query(params)}",
        headers=POLYMARKET_REQUEST_HEADERS,
        failure_message=f"Polymarket {what} request failed.",
        invalid_response_message=f"Polymarket {what} returned an invalid response.",
    )


def _clob_request(path: str, params: dict[str, str], *, what: str) -> JSONObject:
    return json_request(
        "GET",
        f"{CLOB_API_BASE_URL}{path}?{encode_query(params)}",
        headers=POLYMARKET_REQUEST_HEADERS,
        failure_message=f"Polymarket {what} request failed.",
        invalid_response_message=f"Polymarket {what} returned an invalid response.",
    )


def _market_summary(market: JSONObject) -> JSONObject:
    return {
        "id": _clip(market.get("id"), 40),
        "question": _clip(market.get("question"), 300),
        "slug": _clip(market.get("slug"), 200),
        "outcomes": _clip(market.get("outcomes"), 400),
        "outcome_prices": _clip(market.get("outcomePrices"), 400),
        "clob_token_ids": _clip(market.get("clobTokenIds"), 400),
        "volume_24h": _number(market.get("volume24hr")),
        "liquidity": _number(market.get("liquidityNum") if market.get("liquidityNum") is not None else market.get("liquidity")),
        "end_date": _clip(market.get("endDate"), 40),
        "active": market.get("active") is True,
        "closed": market.get("closed") is True,
    }


def _event_summary(event: JSONObject) -> JSONObject:
    markets = event.get("markets")
    market_summaries: list[JSONValue] = []
    if isinstance(markets, list):
        for market in markets[:10]:
            if isinstance(market, dict):
                market_summaries.append(_market_summary(cast(JSONObject, market)))
    return {
        "id": _clip(event.get("id"), 40),
        "title": _clip(event.get("title"), 300),
        "slug": _clip(event.get("slug"), 200),
        "description": _clip(event.get("description"), 600),
        "volume_24h": _number(event.get("volume24hr")),
        "liquidity": _number(event.get("liquidity")),
        "end_date": _clip(event.get("endDate"), 40),
        "markets": market_summaries,
    }


def _records(response: JSONObject) -> list[JSONObject]:
    items = response.get("items")
    if not isinstance(items, list):
        return []
    return [cast(JSONObject, item) for item in items if isinstance(item, dict)]


def _listing_params(tool_input: JSONObject) -> dict[str, str]:
    limit = _int_field(tool_input, "limit", default=DEFAULT_LIMIT, low=1, high=MAX_LIMIT)
    offset = _int_field(tool_input, "offset", default=0, low=0, high=10_000)
    order = _string_field(tool_input, "order", max_chars=40) or "volume24hr"
    if order not in {"volume24hr", "volume", "liquidity", "startDate", "endDate"}:
        raise ToolInputValidationError("Polymarket tool_input.order is not a supported ordering.")
    params = {
        "limit": str(limit),
        "offset": str(offset),
        "order": order,
        "ascending": "false",
    }
    if tool_input.get("include_closed") is not True:
        params["active"] = "true"
        params["closed"] = "false"
    return params


def _list_markets(tool_input: JSONObject) -> JSONObject:
    _reject_unknown_fields(tool_input, frozenset({"limit", "offset", "order", "include_closed"}))
    params = _listing_params(tool_input)
    response = _gamma_request("/markets", params, what="market listing")
    markets = [
        cast(JSONValue, _market_summary(market))
        for market in _records(response)[: int(params["limit"])]
    ]
    return {
        "status": "success_executed",
        "message": f"Polymarket returned {len(markets)} market(s).",
        "markets": markets,
    }


def _list_events(tool_input: JSONObject) -> JSONObject:
    _reject_unknown_fields(tool_input, frozenset({"limit", "offset", "order", "include_closed"}))
    params = _listing_params(tool_input)
    response = _gamma_request("/events", params, what="event listing")
    events = [
        cast(JSONValue, _event_summary(event))
        for event in _records(response)[: int(params["limit"])]
    ]
    return {
        "status": "success_executed",
        "message": f"Polymarket returned {len(events)} event(s).",
        "events": events,
    }


def _search(tool_input: JSONObject) -> JSONObject:
    _reject_unknown_fields(tool_input, frozenset({"query", "limit_per_type"}))
    query = _string_field(tool_input, "query", max_chars=MAX_QUERY_CHARS, required=True)
    limit_per_type = _int_field(tool_input, "limit_per_type", default=10, low=1, high=50)
    response = _gamma_request(
        "/public-search",
        {"q": query, "limit_per_type": str(limit_per_type), "events_status": "active"},
        what="search",
    )
    events_raw = response.get("events")
    markets: list[JSONValue] = []
    events: list[JSONValue] = []
    seen_markets: set[str] = set()
    if isinstance(events_raw, list):
        for event in events_raw[:limit_per_type]:
            if isinstance(event, dict):
                event_object = cast(JSONObject, event)
                events.append(_event_summary(event_object))
                nested_markets = event_object.get("markets")
                if not isinstance(nested_markets, list):
                    continue
                for market in nested_markets:
                    if len(markets) >= limit_per_type:
                        break
                    if not isinstance(market, dict):
                        continue
                    market_object = cast(JSONObject, market)
                    key = _clip(market_object.get("id"), 40) or _clip(market_object.get("slug"), 200)
                    if not key or key in seen_markets:
                        continue
                    seen_markets.add(key)
                    markets.append(_market_summary(market_object))
    return {
        "status": "success_executed",
        "message": f"Polymarket search returned {len(events)} event(s) and {len(markets)} market(s).",
        "query": query,
        "events": events,
        "markets": markets,
    }


def _get_market(tool_input: JSONObject) -> JSONObject:
    _reject_unknown_fields(tool_input, frozenset({"market_id", "slug"}))
    market_id = _string_field(tool_input, "market_id", max_chars=60)
    slug = _string_field(tool_input, "slug", max_chars=200)
    if bool(market_id) == bool(slug):
        raise ToolInputValidationError("Polymarket get_market requires exactly one of tool_input.market_id or tool_input.slug.")
    if market_id:
        # Quote the id as a single path segment so it cannot inject a different
        # Gamma path or query (the manifest reads exactly one market by id).
        response = _gamma_request(f"/markets/{urllib.parse.quote(market_id, safe='')}", {}, what="market lookup")
        record: JSONObject | None = response if response.get("items") is None else None
        if record is None:
            records = _records(response)
            record = records[0] if records else None
    else:
        response = _gamma_request("/markets", {"slug": slug}, what="market lookup")
        records = _records(response)
        record = records[0] if records else None
    if not record or not record.get("id"):
        return {"status": "success_executed", "message": "Polymarket market was not found.", "market": None}
    market = _market_summary(record)
    market["description"] = _clip(record.get("description"), MAX_TEXT_CHARS)
    return {"status": "success_executed", "message": "Polymarket market loaded.", "market": market}


def _get_order_book(tool_input: JSONObject) -> JSONObject:
    _reject_unknown_fields(tool_input, frozenset({"token_id"}))
    token_id = _string_field(tool_input, "token_id", max_chars=120, required=True)
    if not token_id.isascii() or not token_id.isdecimal():
        raise ToolInputValidationError("Polymarket tool_input.token_id must be a decimal token id string.")
    book = _clob_request("/book", {"token_id": token_id}, what="order book")
    midpoint = _clob_request("/midpoint", {"token_id": token_id}, what="midpoint")

    def _levels(side: JSONValue) -> list[JSONValue]:
        if not isinstance(side, list):
            return []
        levels: list[JSONValue] = []
        for level in side[:10]:
            if isinstance(level, dict):
                levels.append({"price": _clip(level.get("price"), 20), "size": _clip(level.get("size"), 20)})
        return levels

    return {
        "status": "success_executed",
        "message": "Polymarket order book loaded.",
        "token_id": token_id,
        "bids": _levels(book.get("bids")),
        "asks": _levels(book.get("asks")),
        "midpoint": _clip(midpoint.get("mid"), 20),
    }


def _price_history(tool_input: JSONObject) -> JSONObject:
    _reject_unknown_fields(tool_input, frozenset({"token_id", "interval"}))
    token_id = _string_field(tool_input, "token_id", max_chars=120, required=True)
    if not token_id.isascii() or not token_id.isdecimal():
        raise ToolInputValidationError("Polymarket tool_input.token_id must be a decimal token id string.")
    interval = _string_field(tool_input, "interval", max_chars=10) or "1d"
    if interval not in {"1h", "6h", "1d", "1w", "1m", "max"}:
        raise ToolInputValidationError("Polymarket tool_input.interval is not a supported interval.")
    response = _clob_request("/prices-history", {"market": token_id, "interval": interval, "fidelity": "60"}, what="price history")
    history_raw = response.get("history")
    points: list[JSONValue] = []
    if isinstance(history_raw, list):
        for point in history_raw[-200:]:
            if isinstance(point, dict):
                timestamp = point.get("t")
                price = point.get("p")
                if isinstance(timestamp, int) and isinstance(price, (int, float)) and not isinstance(price, bool):
                    points.append({"t": timestamp, "p": price})
    return {
        "status": "success_executed",
        "message": f"Polymarket returned {len(points)} price point(s).",
        "token_id": token_id,
        "interval": interval,
        "history": points,
    }


class PolymarketTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        del api  # No credentials and no config: the Polymarket data APIs are public.
        try:
            if action == "list_markets":
                return ActionExecuted(_list_markets(tool_input))
            if action == "list_events":
                return ActionExecuted(_list_events(tool_input))
            if action == "search":
                return ActionExecuted(_search(tool_input))
            if action == "get_market":
                return ActionExecuted(_get_market(tool_input))
            if action == "get_order_book":
                return ActionExecuted(_get_order_book(tool_input))
            if action == "price_history":
                return ActionExecuted(_price_history(tool_input))
            return ActionFailed("Unsupported Polymarket action.")
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except WebRequestError as exc:
            if exc.status == 429:
                return ActionFailed("Polymarket API rate limit was reached.")
            if exc.status:
                return ActionFailed(f"Polymarket API returned HTTP {exc.status}.")
            return ActionFailed(str(exc) or "Polymarket API request failed.")
        except Exception:
            # Curated errors are handled above; an unexpected exception must not
            # leak its raw text to the agent.
            return ActionFailed("Polymarket tool request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        del approval, api
        return ActionFailed("Polymarket has no approval-gated actions.")


# The instance the host discovers (see host.runtime.tools_host).
BUNDLED_TOOL = PolymarketTool()
