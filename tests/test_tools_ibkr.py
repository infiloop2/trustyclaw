"""Unit tests for the IBKR live Web API tool package (all provider calls mocked).

The fake provider below implements IBKR's side of the live-session-token
exchange for real: it decodes the tool's DH challenge from the Authorization
header, computes the same shared secret, and returns a consistent
``live_session_token_signature`` — so these tests prove the whole crypto dance
round-trips, not just that requests were made.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import unittest
import urllib.parse
from typing import Any
from unittest.mock import patch

from host.tools.json_types import JSONObject
from host.tools.results import ActionExecuted, ActionFailed
from host.tools import ibkr
from host.tools.ibkr import IBKRTool, _signed_big_endian
from host.tools.shared.rsa_pkcs1 import load_rsa_private_key
from host.tools.shared.web import WebRequestError

from test_tools import FakeHostAPI
from test_tools_rsa_pkcs1 import PKCS1_PEM, PKCS8_PEM, encrypt_pkcs1_v1_5

# 2**127 - 1 is prime; small enough for fast tests, real enough for the math.
TEST_DH_PRIME_HEX = format(2**127 - 1, "x")
TEST_CONSUMER_KEY = "TESTCLAWK"
TEST_ACCESS_TOKEN = "abc123token"
TEST_SECRET_PLAIN = bytes.fromhex("00112233445566778899aabbccddeeff")
TEST_SERVER_DH_EXPONENT = 0x5F1E_D00D_CAFE_F00D


def configured_api() -> FakeHostAPI:
    api = FakeHostAPI()
    key = load_rsa_private_key(PKCS1_PEM)
    api.config["IBKR_OAUTH_CONSUMER_KEY"] = TEST_CONSUMER_KEY
    api.config["IBKR_OAUTH_ACCESS_TOKEN"] = TEST_ACCESS_TOKEN
    api.config["IBKR_OAUTH_ACCESS_TOKEN_SECRET"] = base64.b64encode(
        encrypt_pkcs1_v1_5(key, TEST_SECRET_PLAIN)
    ).decode("ascii")
    api.config["IBKR_SIGNATURE_KEY"] = PKCS1_PEM
    api.config["IBKR_ENCRYPTION_KEY"] = PKCS8_PEM
    api.config["IBKR_DH_PRIME"] = TEST_DH_PRIME_HEX
    return api


def parse_oauth_header(header: str) -> dict[str, str]:
    assert header.startswith('OAuth realm="limited_poa", ')
    pairs = header[len('OAuth realm="limited_poa", ') :].split(", ")
    return {key: value.strip('"') for key, value in (pair.split("=", 1) for pair in pairs)}


class FakeIBKRServer:
    """Scriptable IBKR: a correct /oauth/live_session_token plus canned data."""

    def __init__(self, responses: dict[str, JSONObject] | None = None) -> None:
        self.responses = responses or {}
        self.requests: list[dict[str, Any]] = []
        self.live_session_token = b""

    def json_request(self, method: str, url: str, **kwargs: Any) -> JSONObject:
        headers = kwargs.get("headers") or {}
        self.requests.append({"method": method, "url": url, **kwargs})
        if url.endswith("/oauth/live_session_token"):
            oauth = parse_oauth_header(headers["authorization"])
            assert oauth["oauth_signature_method"] == "RSA-SHA256"
            prime = int(TEST_DH_PRIME_HEX, 16)
            challenge = int(oauth["diffie_hellman_challenge"], 16)
            shared_secret = pow(challenge, TEST_SERVER_DH_EXPONENT, prime)
            self.live_session_token = hmac.new(
                _signed_big_endian(shared_secret), TEST_SECRET_PLAIN, hashlib.sha1
            ).digest()
            return {
                "diffie_hellman_response": format(pow(2, TEST_SERVER_DH_EXPONENT, prime), "x"),
                "live_session_token_signature": hmac.new(
                    self.live_session_token, TEST_CONSUMER_KEY.encode(), hashlib.sha1
                ).hexdigest(),
                "live_session_token_expiration": 1_800_000_000_000,
            }
        oauth = parse_oauth_header(headers["authorization"])
        assert oauth["oauth_signature_method"] == "HMAC-SHA256"
        self._verify_hmac_signature(method, url, oauth, kwargs.get("body"))
        path = urllib.parse.urlparse(url).path.removeprefix("/v1/api")
        assert path in self.responses, f"unexpected request: {path}"
        return self.responses[path]

    def _verify_hmac_signature(self, method: str, url: str, oauth: dict[str, str], body: object) -> None:
        parsed = urllib.parse.urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        params = {key: value for key, value in oauth.items() if key != "oauth_signature"}
        params.update(dict(urllib.parse.parse_qsl(parsed.query)))
        pairs = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
        base_string = f"{method}&{urllib.parse.quote_plus(base_url)}&{urllib.parse.quote_plus(pairs)}"
        expected = urllib.parse.quote_plus(
            base64.b64encode(
                hmac.new(self.live_session_token, base_string.encode(), hashlib.sha256).digest()
            ).decode("ascii")
        )
        assert oauth["oauth_signature"] == expected, "request HMAC signature did not verify"


ACCOUNTS_RESPONSE: JSONObject = {"items": [{"accountId": "U1234567"}, {"accountId": "U7654321"}]}


class IBKRToolTests(unittest.TestCase):
    def test_manifest_shape(self) -> None:
        tool = IBKRTool()
        self.assertEqual(tool.manifest.tool_id, "ibkr")
        self.assertEqual(tool.manifest.display_name, "Interactive Brokers")
        self.assertEqual(tool.manifest.connection, "enable_only")
        self.assertIsNone(tool.credentials)
        self.assertEqual(
            [spec.id for spec in tool.manifest.actions],
            ["get_accounts", "get_positions", "get_account_summary", "get_trades"],
        )
        self.assertEqual(len(tool.manifest.config), 6)
        self.assertIn(
            "cannot send arbitrary request text, orders, or trading instructions",
            tool.manifest.data_summary.cards[0].points[0].text,
        )
        self.assertIn(
            "IBKR does not make the OAuth credential read-only",
            " ".join(tool.manifest.protections),
        )
        # The reduced-permission-username recommendation precedes registration,
        # because the OAuth consumer binds to whichever username registers it.
        self.assertEqual(
            tool.manifest.setup_steps[2].title,
            "Recommended: prepare a reduced-permission username",
        )
        self.assertEqual(
            tool.manifest.setup_steps[3].link_url,
            "https://ndcdyn.interactivebrokers.com/sso/Login?RL=1&action=OAUTH",
        )

    def test_get_positions_round_trips_the_oauth_dance(self) -> None:
        server = FakeIBKRServer(
            {
                "/portfolio/accounts": ACCOUNTS_RESPONSE,
                "/portfolio/U1234567/positions/0": {
                    "items": [
                        {
                            "ticker": "AAPL",
                            "contractDesc": "AAPL",
                            "assetClass": "STK",
                            "currency": "USD",
                            "position": 100.0,
                            "mktPrice": 210.5,
                            "mktValue": 21050.0,
                            "avgCost": 180.0,
                            "unrealizedPnl": 3050.0,
                            "realizedPnl": 0.0,
                        }
                    ]
                },
            }
        )
        with patch.object(ibkr, "json_request", server.json_request):
            result = IBKRTool().execute("get_positions", {"account_id": "U1234567"}, configured_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["account_id"], "U1234567")
        positions = result.result["positions"]
        assert isinstance(positions, list)
        position = positions[0]
        assert isinstance(position, dict)
        self.assertEqual(position["symbol"], "AAPL")
        self.assertEqual(position["quantity"], 100.0)
        self.assertEqual(position["mark_price"], 210.5)
        # Every data request carried a User-Agent and the OAuth realm header.
        for request in server.requests:
            self.assertEqual(request["headers"]["user-agent"], ibkr.IBKR_USER_AGENT)

    def test_get_positions_honors_requested_account(self) -> None:
        server = FakeIBKRServer(
            {
                "/portfolio/accounts": ACCOUNTS_RESPONSE,
                "/portfolio/U7654321/positions/0": {"items": []},
            }
        )
        with patch.object(ibkr, "json_request", server.json_request):
            result = IBKRTool().execute("get_positions", {"account_id": "U7654321"}, configured_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["account_id"], "U7654321")

    def test_unknown_account_fails_with_the_available_ids(self) -> None:
        server = FakeIBKRServer({"/portfolio/accounts": ACCOUNTS_RESPONSE})
        with patch.object(ibkr, "json_request", server.json_request):
            result = IBKRTool().execute("get_positions", {"account_id": "U9999999"}, configured_api())
        assert isinstance(result, ActionFailed)
        self.assertIn("U9999999", result.error)
        self.assertIn("U1234567", result.error)

    def test_missing_account_id_fails_with_the_available_ids(self) -> None:
        server = FakeIBKRServer({"/portfolio/accounts": ACCOUNTS_RESPONSE})
        with patch.object(ibkr, "json_request", server.json_request):
            result = IBKRTool().execute("get_positions", {}, configured_api())
        assert isinstance(result, ActionFailed)
        self.assertIn("account_id is required", result.error)
        self.assertIn("U1234567", result.error)
        self.assertIn("U7654321", result.error)

    def test_get_accounts_lists_the_login_accounts(self) -> None:
        server = FakeIBKRServer(
            {
                "/portfolio/accounts": {
                    "items": [
                        {"accountId": "U1234567", "accountTitle": "Alice", "currency": "USD", "type": "INDIVIDUAL"},
                        {"accountId": "U7654321"},
                    ]
                },
            }
        )
        with patch.object(ibkr, "json_request", server.json_request):
            result = IBKRTool().execute("get_accounts", {}, configured_api())
        assert isinstance(result, ActionExecuted)
        accounts = result.result["accounts"]
        assert isinstance(accounts, list)
        self.assertEqual([account["account_id"] for account in accounts], ["U1234567", "U7654321"])
        self.assertEqual(accounts[0]["title"], "Alice")
        self.assertEqual(accounts[0]["currency"], "USD")

    def test_get_accounts_rejects_unsupported_input(self) -> None:
        result = IBKRTool().execute("get_accounts", {"account_id": "U1234567"}, configured_api())
        assert isinstance(result, ActionFailed)
        self.assertIn("no fields", result.error)

    def test_provider_account_ids_are_validated_before_use_in_a_path(self) -> None:
        server = FakeIBKRServer(
            {
                "/portfolio/accounts": {
                    "items": [
                        {"accountId": "../iserver/account/orders"},
                        {"accountId": "U1234567"},
                        {"accountId": "U1234567"},
                    ]
                },
                "/portfolio/U1234567/positions/0": {"items": []},
            }
        )
        with patch.object(ibkr, "json_request", server.json_request):
            listed = IBKRTool().execute("get_accounts", {}, configured_api())
            result = IBKRTool().execute("get_positions", {"account_id": "U1234567"}, configured_api())
        # The hostile provider id is dropped from the listing (charset-validated,
        # deduplicated) and never lands in a request path.
        assert isinstance(listed, ActionExecuted)
        self.assertEqual(
            [account["account_id"] for account in listed.result["accounts"]], ["U1234567"]
        )
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["account_id"], "U1234567")
        self.assertFalse(any("/orders" in request["url"] for request in server.requests))

    def test_get_account_summary_maps_selected_keys(self) -> None:
        server = FakeIBKRServer(
            {
                "/portfolio/accounts": ACCOUNTS_RESPONSE,
                "/portfolio/U1234567/summary": {
                    "netliquidation": {"amount": 125000.5, "currency": "USD"},
                    "buyingpower": {"amount": 250000.0, "currency": "USD"},
                    "irrelevantkey": {"amount": 1.0, "currency": "USD"},
                },
            }
        )
        with patch.object(ibkr, "json_request", server.json_request):
            result = IBKRTool().execute("get_account_summary", {"account_id": "U1234567"}, configured_api())
        assert isinstance(result, ActionExecuted)
        summary = result.result["summary"]
        assert isinstance(summary, dict)
        self.assertEqual(summary["netliquidation"], {"amount": 125000.5, "currency": "USD"})
        self.assertNotIn("irrelevantkey", summary)

    def test_get_trades_opens_a_brokerage_session_first(self) -> None:
        server = FakeIBKRServer(
            {
                "/portfolio/accounts": ACCOUNTS_RESPONSE,
                "/iserver/auth/ssodh/init": {"authenticated": True, "competing": False, "connected": True},
                "/iserver/accounts": {
                    "accounts": ["U1234567", "U7654321"],
                    "selectedAccount": "U1234567",
                },
                "/iserver/account/trades": {
                    "items": [
                        {
                            "execution_id": "0000e0d5.1234",
                            "trade_time": "20260712-14:30:05",
                            "symbol": "MSFT",
                            "order_description": "Bought 10 MSFT",
                            "sec_type": "STK",
                            "side": "B",
                            "size": 10,
                            "price": "500.10",
                            "commission": "1.00",
                            "net_amount": 5001.0,
                            "exchange": "NASDAQ",
                            "currency": "USD",
                            "account": "U1234567",
                        },
                        {"execution_id": "other-account", "account": "U0000001"},
                    ]
                },
            }
        )
        with patch.object(ibkr, "json_request", server.json_request):
            result = IBKRTool().execute("get_trades", {"account_id": "U1234567", "days": "3"}, configured_api())
        assert isinstance(result, ActionExecuted)
        init_request = next(r for r in server.requests if "/ssodh/init" in r["url"])
        # compete=False so this read never force-closes the operator's own live
        # brokerage session (TWS / Client Portal).
        self.assertEqual(init_request["body"], {"publish": True, "compete": False})
        trades_request = next(r for r in server.requests if "/account/trades" in r["url"])
        self.assertIn("days=3", trades_request["url"])
        self.assertNotIn("accountId=", trades_request["url"])
        trades = result.result["trades"]
        assert isinstance(trades, list)
        self.assertEqual(len(trades), 1)  # the other account's trade is filtered out
        trade = trades[0]
        assert isinstance(trade, dict)
        self.assertEqual(trade["symbol"], "MSFT")

    def test_get_trades_switches_to_the_requested_brokerage_account(self) -> None:
        server = FakeIBKRServer(
            {
                "/portfolio/accounts": ACCOUNTS_RESPONSE,
                "/iserver/auth/ssodh/init": {"authenticated": True, "competing": False, "connected": True},
                "/iserver/accounts": {
                    "accounts": ["U1234567", "U7654321"],
                    "selectedAccount": "U1234567",
                },
                "/iserver/account": {"set": True, "acctId": "U7654321"},
                "/iserver/account/trades": {
                    "items": [{"execution_id": "other", "account": "U7654321", "symbol": "AAPL"}]
                },
            }
        )
        with patch.object(ibkr, "json_request", server.json_request):
            result = IBKRTool().execute("get_trades", {"account_id": "U7654321"}, configured_api())
        assert isinstance(result, ActionExecuted)
        switch = next(r for r in server.requests if urllib.parse.urlparse(r["url"]).path.endswith("/iserver/account"))
        self.assertEqual(switch["method"], "POST")
        self.assertEqual(switch["body"], {"acctId": "U7654321"})
        self.assertEqual(result.result["account_id"], "U7654321")
        self.assertEqual(len(result.result["trades"]), 1)

    def test_get_trades_applies_the_cap_after_filtering_to_the_account(self) -> None:
        other_account_trades = [
            {"execution_id": f"other-{index}", "account": "U7654321"}
            for index in range(ibkr.MAX_TRADES)
        ]
        server = FakeIBKRServer(
            {
                "/portfolio/accounts": ACCOUNTS_RESPONSE,
                "/iserver/auth/ssodh/init": {"authenticated": True, "competing": False, "connected": True},
                "/iserver/accounts": {"accounts": ["U1234567"], "selectedAccount": "U1234567"},
                "/iserver/account/trades": {
                    "items": [
                        *other_account_trades,
                        {"execution_id": "requested-account", "account": "U1234567", "symbol": "MSFT"},
                    ]
                },
            }
        )
        with patch.object(ibkr, "json_request", server.json_request):
            result = IBKRTool().execute("get_trades", {"account_id": "U1234567"}, configured_api())
        assert isinstance(result, ActionExecuted)
        trades = result.result["trades"]
        assert isinstance(trades, list)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["execution_id"], "requested-account")
        self.assertEqual(trades[0]["symbol"], "MSFT")

    def test_get_trades_fails_if_requested_account_is_not_in_brokerage_session(self) -> None:
        server = FakeIBKRServer(
            {
                "/portfolio/accounts": ACCOUNTS_RESPONSE,
                "/iserver/auth/ssodh/init": {"authenticated": True, "competing": False, "connected": True},
                "/iserver/accounts": {"accounts": ["U1234567"], "selectedAccount": "U1234567"},
            }
        )
        with patch.object(ibkr, "json_request", server.json_request):
            result = IBKRTool().execute("get_trades", {"account_id": "U7654321"}, configured_api())
        assert isinstance(result, ActionFailed)
        self.assertIn("not available to the brokerage session", result.error)
        self.assertFalse(any("/account/trades" in r["url"] for r in server.requests))

    def test_get_trades_fails_when_brokerage_session_does_not_open(self) -> None:
        server = FakeIBKRServer(
            {
                "/portfolio/accounts": ACCOUNTS_RESPONSE,
                "/iserver/auth/ssodh/init": {"authenticated": False},
            }
        )
        with patch.object(ibkr, "json_request", server.json_request):
            result = IBKRTool().execute("get_trades", {"account_id": "U1234567"}, configured_api())
        assert isinstance(result, ActionFailed)
        self.assertIn("brokerage session", result.error)

    def test_get_trades_does_not_take_over_a_competing_session(self) -> None:
        # When the operator already has a live session, IBKR reports competing;
        # the read must decline rather than disconnect the human.
        server = FakeIBKRServer(
            {
                "/portfolio/accounts": ACCOUNTS_RESPONSE,
                "/iserver/auth/ssodh/init": {"authenticated": False, "competing": True},
            }
        )
        with patch.object(ibkr, "json_request", server.json_request):
            result = IBKRTool().execute("get_trades", {"account_id": "U1234567"}, configured_api())
        assert isinstance(result, ActionFailed)
        self.assertIn("another live brokerage session", result.error)
        self.assertFalse(any("/account/trades" in r["url"] for r in server.requests))

    def test_bad_lst_signature_fails_closed(self) -> None:
        server = FakeIBKRServer()

        def tampered(method: str, url: str, **kwargs: Any) -> JSONObject:
            response = server.json_request(method, url, **kwargs)
            if "live_session_token_signature" in response:
                response["live_session_token_signature"] = "00" * 20
            return response

        with patch.object(ibkr, "json_request", tampered):
            result = IBKRTool().execute("get_positions", {}, configured_api())
        assert isinstance(result, ActionFailed)
        self.assertIn("verification", result.error)

    def test_config_errors_name_the_config_key(self) -> None:
        for bad_consumer_key in ("TOOSHORT", "lowercase", "ABCDEFGHI0"):
            api = configured_api()
            api.config["IBKR_OAUTH_CONSUMER_KEY"] = bad_consumer_key
            result = IBKRTool().execute("get_positions", {}, api)
            assert isinstance(result, ActionFailed)
            self.assertIn("IBKR_OAUTH_CONSUMER_KEY", result.error)

        api = configured_api()
        api.config["IBKR_SIGNATURE_KEY"] = "not-a-key"
        result = IBKRTool().execute("get_positions", {}, api)
        assert isinstance(result, ActionFailed)
        self.assertIn("IBKR_SIGNATURE_KEY", result.error)

        api = configured_api()
        api.config["IBKR_OAUTH_ACCESS_TOKEN_SECRET"] = "///not-base64///"
        result = IBKRTool().execute("get_positions", {}, api)
        assert isinstance(result, ActionFailed)
        self.assertIn("IBKR_OAUTH_ACCESS_TOKEN_SECRET", result.error)

    def test_401_maps_to_operator_actionable_message(self) -> None:
        def rejecting(method: str, url: str, **kwargs: Any) -> JSONObject:
            raise WebRequestError("failed", status=401)

        with patch.object(ibkr, "json_request", rejecting):
            result = IBKRTool().execute("get_positions", {}, configured_api())
        assert isinstance(result, ActionFailed)
        self.assertIn("weekend", result.error)

    def test_input_validation(self) -> None:
        tool = IBKRTool()
        api = configured_api()
        self.assertIsInstance(tool.execute("get_positions", {"account_id": "../x"}, api), ActionFailed)
        self.assertIsInstance(tool.execute("get_positions", {"days": "3"}, api), ActionFailed)
        self.assertIsInstance(tool.execute("get_trades", {"days": "nope"}, api), ActionFailed)
        self.assertIsInstance(tool.execute("get_trades", {"days": "0"}, api), ActionFailed)
        self.assertIsInstance(tool.execute("get_trades", {"days": "8"}, api), ActionFailed)
        self.assertIsInstance(tool.execute("get_trades", {"days": "9" * 100}, api), ActionFailed)
        self.assertIsInstance(tool.execute("get_trades", {"days": "²"}, api), ActionFailed)
        self.assertIsInstance(tool.execute("place_order", {}, api), ActionFailed)

    def test_no_approval_actions(self) -> None:
        result = IBKRTool().execute_approved("approval-1", configured_api())
        self.assertIsInstance(result, ActionFailed)


if __name__ == "__main__":
    unittest.main()
