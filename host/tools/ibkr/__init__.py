"""Interactive Brokers read-only portfolio tool package (live Web API).

Talks to IBKR's Web API at api.ibkr.com using the first-party OAuth 1.0a
mechanism: the operator self-services a consumer key, an access token, and an
RSA-encrypted access token secret through IBKR's dedicated OAuth page, and the
tool derives a
short-lived live session token (LST) per action call — an RSA-SHA256-signed
Diffie-Hellman exchange — then signs the actual data requests with
HMAC-SHA256 under that LST. Real-time data is structurally limited by this
package to three fixed read actions. The IBKR OAuth credential itself is not
read-only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import string
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink, DataSummaryPoint, SetupStep, ToolManifest
from host.tools.results import ActionExecuted, ActionFailed, ActionResult, ApprovalResult
from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.shared.rsa_pkcs1 import (
    RSAKeyError,
    RSAPrivateKey,
    decrypt_pkcs1_v1_5,
    load_rsa_private_key,
    sign_sha256_pkcs1_v1_5,
)
from host.tools.shared.web import WebRequestError, encode_query, json_request

IBKR_API_BASE_URL = "https://api.ibkr.com/v1/api"
# IBKR rejects requests without a User-Agent.
IBKR_USER_AGENT = "trustyclaw-tools/1.0"
# First-party production realm; "test_realm" exists only for IBKR's shared
# TESTCONS test consumer, which this tool does not use.
IBKR_REALM = "limited_poa"
DH_GENERATOR = 2
NONCE_ALPHABET = string.ascii_letters + string.digits
MAX_POSITIONS = 100  # one page of /portfolio/{acct}/positions
MAX_TRADES = 100
MAX_ACCOUNTS = 100
MAX_TRADE_DAYS = 7  # the /iserver/account/trades window IBKR serves
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")
CONSUMER_KEY_RE = re.compile(r"^[A-Z]{9}$")
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
SUMMARY_KEYS = (
    "netliquidation",
    "totalcashvalue",
    "settledcash",
    "availablefunds",
    "buyingpower",
    "excessliquidity",
    "grosspositionvalue",
    "initmarginreq",
    "maintmarginreq",
)
IBKR_READ_POLICY = (
    "Read-only. Sends only OAuth-signed requests for the operator's own account "
    "to Interactive Brokers' Web API and returns live portfolio data (accounts, "
    "positions, balances, trades) into the host and active model context. Runs directly with no approval. "
    "This tool has no trading actions."
)
IBKR_UNAUTHORIZED_MESSAGE = (
    "IBKR rejected the request as unauthorized. Check the OAuth config values; "
    "note that a newly created consumer key only activates after IBKR's weekend "
    "server restart."
)


class ToolInputValidationError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


IBKR_OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}},
    "additionalProperties": True,
}

_ACCOUNT_INPUT: JSONObject = {
    "account_id": {
        "type": "string",
        "description": "IBKR account id (e.g. U1234567). Defaults to the first account of the login.",
    }
}


def _schema(properties: JSONObject) -> JSONObject:
    return {"type": "object", "properties": properties, "additionalProperties": False}


MANIFEST = ToolManifest(
    tool_id="ibkr",
    display_name="Interactive Brokers",
    description="Connect your Interactive Brokers account and let your agent read live positions, balances, margin, and executed trades. Trading is not available.",
    connection="enable_only",
    actions=(
        ActionSpec(id="get_positions",
            description="Read up to 100 current open positions for one IBKR account, including quantity, mark/value, cost, and unrealized/realized PnL. This is a live portfolio snapshot, not executions or an order book.",
            data_policy=IBKR_READ_POLICY,
            input_schema=_schema(dict(_ACCOUNT_INPUT)),
            output_schema=IBKR_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="get_account_summary",
            description="Read one IBKR account's live financial summary: net liquidation, cash, available funds, buying power, excess liquidity, position value, and initial/maintenance margin with currencies. This does not list positions or trades.",
            data_policy=IBKR_READ_POLICY,
            input_schema=_schema(dict(_ACCOUNT_INPUT)),
            output_schema=IBKR_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="get_trades",
            description="Read up to 100 completed executions for one IBKR account from the last 1-7 days, including side, size, price, commission, exchange, and time. This is trade history, not open positions or pending orders.",
            data_policy=IBKR_READ_POLICY,
            input_schema=_schema(
                {
                    **_ACCOUNT_INPUT,
                    "days": {"type": "string", "description": "How many days back, 1-7 (default 7)."},
                }
            ),
            output_schema=IBKR_OUTPUT_SCHEMA,
        ),
    ),
    config=(
        ConfigRequirement(key="IBKR_OAUTH_CONSUMER_KEY", description="The nine-uppercase-letter public identifier you choose in IBKR's OAuth self-service page."),
        ConfigRequirement(key="IBKR_OAUTH_ACCESS_TOKEN", description="Access token generated in the OAuth self-service portal."),
        ConfigRequirement(key="IBKR_OAUTH_ACCESS_TOKEN_SECRET", description="Access token secret from the portal (base64; it is encrypted to your public encryption key)."),
        ConfigRequirement(key="IBKR_SIGNATURE_KEY", description="Your private RSA signature key (PEM or base64 DER; whitespace/newlines optional, so it can be pasted as one line)."),
        ConfigRequirement(key="IBKR_ENCRYPTION_KEY", description="Your private RSA encryption key (PEM or base64 DER; whitespace/newlines optional)."),
        ConfigRequirement(key="IBKR_DH_PRIME", description="Hex prime from your dhparam.pem (openssl dhparam -in dhparam.pem -text; strip colons/whitespace)."),
    ),
    protections=(
        "The package exposes only three read actions. It contains no order, transfer, account-change, or trading endpoint and signs requests only to api.ibkr.com.",
        "TrustyClaw uses the credentials only for these reads, but IBKR does not make the OAuth credential read-only. Anyone with the six values could use the permissions of the authorized IBKR username.",
    ),
    setup_steps=(
        SetupStep(
            title="Generate the signature keypair",
            description="On a trusted machine run `openssl genrsa -out private_signature.pem 2048`, then `openssl rsa -in private_signature.pem -pubout -out public_signature.pem`. Keep private_signature.pem secret for IBKR_SIGNATURE_KEY; upload only public_signature.pem to IBKR. Do not reuse this keypair for encryption.",
            link_url="https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/",
            link_label="Open the IBKR Client Portal API guide",
        ),
        SetupStep(
            title="Generate the encryption keypair and DH parameters",
            description="Run `openssl genrsa -out private_encryption.pem 2048` and `openssl rsa -in private_encryption.pem -pubout -out public_encryption.pem`, then `openssl dhparam -out dhparam.pem 2048`. Keep private_encryption.pem secret for IBKR_ENCRYPTION_KEY. IBKR receives public_encryption.pem and dhparam.pem, never either private key.",
            link_url="https://www.interactivebrokers.com/campus/ibkr-api-page/oauth-1-0a-extended/",
            link_label="Review IBKR OAuth 1.0a key handling",
        ),
        SetupStep(
            title="Register first-party OAuth through IBKR's dedicated login",
            description="IBKR does not place this flow in the normal Client Portal menus. Open the dedicated OAuth self-service login and sign in with the exact live or paper username the agent will use. If it opens ordinary Client Portal instead of OAuth Configuration, stop and contact IBKR API support because that username does not currently have the self-service flow. Registration and authorization finish entirely on IBKR's side, with no TrustyClaw callback.",
            link_url="https://ndcdyn.interactivebrokers.com/sso/Login?RL=1&action=OAUTH",
            link_label="Open IBKR OAuth self-service",
        ),
        SetupStep(
            title="Choose the consumer key and upload the public files",
            description="Choose any available nine-letter identifier using only uppercase A-Z. This consumer key names the OAuth registration; it is not an IBKR account number and it is not secret. Upload public_signature.pem as the signature key, public_encryption.pem as the encryption key, and dhparam.pem as the Diffie-Hellman parameters. Generate the access token and encrypted access-token secret and copy both immediately; the secret may be shown only once.",
        ),
        SetupStep(
            title="Enable IBKR OAuth access",
            description="On the same OAuth Configuration page, enable API access for the registered consumer and verify the consumer key is enabled for the expected live or paper username. A newly registered key can remain inactive until IBKR's weekend server restart, so an otherwise correct first request may return 401 for up to one week.",
        ),
        SetupStep(
            title="Understand the IBKR permission boundary",
            description="The OAuth credential inherits the authorized username's IBKR permissions; the self-service flow does not give it a read-only scope. TrustyClaw exposes only fixed reads, but the same credential used by different software could trade when that username has trading permissions. For IBKR-side enforcement, ask IBKR Account Configuration for a separate reduced-permission username with no direct trading access. That stronger setup can prevent the completed-trades action from working because IBKR serves it through a brokerage session.",
            link_url="https://www.interactivebrokers.com/campus/ibkr-api-page/market-data-subscriptions/#market-data-users",
            link_label="Review IBKR reduced-permission users",
        ),
        SetupStep(
            title="Extract the Diffie-Hellman prime",
            description="Run `openssl dhparam -in dhparam.pem -text -noout`. Copy only the hexadecimal Prime block, remove the leading label plus all colons, spaces, and line breaks, and keep the resulting hexadecimal string for IBKR_DH_PRIME. Do not paste the full PEM file or the generator line into that field.",
        ),
        SetupStep(
            title="Paste the six values into TrustyClaw",
            show_config=True,
            description="Expand Interactive Brokers in Internet Access and Tools. Set IBKR_OAUTH_CONSUMER_KEY to the nine-letter key, IBKR_OAUTH_ACCESS_TOKEN and IBKR_OAUTH_ACCESS_TOKEN_SECRET to the portal values, IBKR_SIGNATURE_KEY to the complete private_signature.pem contents, IBKR_ENCRYPTION_KEY to the complete private_encryption.pem contents, and IBKR_DH_PRIME to the cleaned hex prime. Save each write-only value, then enable the tool. TrustyClaw has no IBKR Connect button; its side is only this value paste and request signing.",
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                points=(
                    DataSummaryPoint(label="Reads", text="The tool sends only fixed read requests to IBKR. The agent can choose one of your returned account ids and a 1-7 day trade-history window; it cannot send arbitrary request text, orders, or trading instructions."),
                    DataSummaryPoint(label="Authentication", text="Login sends the consumer key, access token, an OAuth signature, and key-exchange values. The private signature and encryption keys, decrypted token secret, and derived session key never leave this host."),
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                description=(
                    "Everything goes to IBKR's Web API for your own brokerage account, and what comes back (positions, balances, "
                    "margin, completed executions) is data IBKR already holds. Nothing is sent anywhere else."
                ),
            ),
            DataSummaryCard(
                title="What IBKR can do with it",
                description=(
                    "IBKR processes this API access like any other access to your brokerage account: account service, security, "
                    "fraud prevention, compliance, and legal and regulatory duties under its privacy notice."
                ),
                links=(
                    DataSummaryLink(label="IBKR privacy notice", url="https://www.interactivebrokers.com/en/general/privacy-notice.php"),
                    DataSummaryLink(label="IBKR Client Portal API documentation", url="https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/"),
                ),
            ),
            DataSummaryCard(
                title="How long IBKR retains it",
                description=(
                    "IBKR keeps brokerage and access records under its regulatory retention duties, which outlast this "
                    "integration. Clearing TrustyClaw config removes the local secrets only; disable or revoke the OAuth "
                    "consumer in IBKR Client Portal to end API access on IBKR's side."
                ),
                links=(
                    DataSummaryLink(label="IBKR privacy notice", url="https://www.interactivebrokers.com/en/general/privacy-notice.php"),
                ),
            ),
        ),
    ),
)


@dataclass(frozen=True)
class _OAuthMaterial:
    consumer_key: str
    access_token: str
    access_token_secret: bytes
    signature_key: RSAPrivateKey
    encryption_key: RSAPrivateKey
    dh_prime: int


def _oauth_material(api: HostAPI) -> _OAuthMaterial:
    consumer_key = api.config["IBKR_OAUTH_CONSUMER_KEY"].strip()
    access_token = api.config["IBKR_OAUTH_ACCESS_TOKEN"].strip()
    if not consumer_key or not access_token:
        raise RuntimeError("Tool config IBKR_OAUTH_CONSUMER_KEY and IBKR_OAUTH_ACCESS_TOKEN must be set.")
    if not CONSUMER_KEY_RE.fullmatch(consumer_key):
        raise RuntimeError("Tool config IBKR_OAUTH_CONSUMER_KEY must be exactly nine uppercase letters A-Z.")
    try:
        access_token_secret = base64.b64decode("".join(api.config["IBKR_OAUTH_ACCESS_TOKEN_SECRET"].split()), validate=True)
    except Exception:
        raise RuntimeError("Tool config IBKR_OAUTH_ACCESS_TOKEN_SECRET is not valid base64.") from None
    try:
        signature_key = load_rsa_private_key(api.config["IBKR_SIGNATURE_KEY"])
    except RSAKeyError as exc:
        raise RuntimeError(f"Tool config IBKR_SIGNATURE_KEY is invalid: {exc}") from None
    try:
        encryption_key = load_rsa_private_key(api.config["IBKR_ENCRYPTION_KEY"])
    except RSAKeyError as exc:
        raise RuntimeError(f"Tool config IBKR_ENCRYPTION_KEY is invalid: {exc}") from None
    prime_text = "".join(api.config["IBKR_DH_PRIME"].split()).replace(":", "")
    if not HEX_RE.fullmatch(prime_text):
        raise RuntimeError("Tool config IBKR_DH_PRIME must be the hex prime from dhparam.pem.")
    return _OAuthMaterial(
        consumer_key=consumer_key,
        access_token=access_token,
        access_token_secret=access_token_secret,
        signature_key=signature_key,
        encryption_key=encryption_key,
        dh_prime=int(prime_text, 16),
    )


def _nonce() -> str:
    return "".join(secrets.choice(NONCE_ALPHABET) for _ in range(16))


def _oauth_params(material: _OAuthMaterial, signature_method: str) -> dict[str, str]:
    return {
        "oauth_consumer_key": material.consumer_key,
        "oauth_nonce": _nonce(),
        "oauth_signature_method": signature_method,
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": material.access_token,
    }


def _base_string(method: str, url: str, params: Mapping[str, str], *, prepend: str = "") -> str:
    pairs = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
    return f"{prepend}{method}&{urllib.parse.quote_plus(url)}&{urllib.parse.quote_plus(pairs)}"


def _authorization_header(params: Mapping[str, str]) -> str:
    rendered = ", ".join(f'{key}="{value}"' for key, value in sorted(params.items()))
    return f'OAuth realm="{IBKR_REALM}", {rendered}'


def _signed_big_endian(value: int) -> bytes:
    """Big-endian bytes with a leading zero byte when the top bit is set —
    Java BigInteger.toByteArray semantics, which IBKR's LST HMAC key uses."""
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if value.bit_length() % 8 == 0:
        raw = b"\x00" + raw
    return raw


def _mapped_web_error(exc: WebRequestError, what: str) -> Exception:
    if exc.status == 401:
        return RuntimeError(IBKR_UNAUTHORIZED_MESSAGE)
    if exc.status == 429:
        return RuntimeError("IBKR Web API rate limit was reached.")
    if exc.status:
        return RuntimeError(f"IBKR Web API returned HTTP {exc.status} on the {what} request.")
    return RuntimeError(f"IBKR {what} request failed.")


def _live_session_token(material: _OAuthMaterial) -> str:
    """The OAuth 1.0a live-session-token dance: send an RSA-SHA256-signed DH
    challenge, then derive the LST as HMAC-SHA1(DH shared secret, decrypted
    access token secret) and verify it against IBKR's returned signature."""
    dh_random = secrets.randbits(256)
    dh_challenge = format(pow(DH_GENERATOR, dh_random, material.dh_prime), "x")
    try:
        secret_bytes = decrypt_pkcs1_v1_5(material.encryption_key, material.access_token_secret)
    except RSAKeyError:
        raise RuntimeError(
            "IBKR access token secret could not be decrypted. Check that "
            "IBKR_ENCRYPTION_KEY matches the public encryption key uploaded to the portal."
        ) from None
    prepend = secret_bytes.hex()
    url = f"{IBKR_API_BASE_URL}/oauth/live_session_token"
    params = _oauth_params(material, "RSA-SHA256")
    params["diffie_hellman_challenge"] = dh_challenge
    signature = sign_sha256_pkcs1_v1_5(
        material.signature_key, _base_string("POST", url, params, prepend=prepend).encode("utf-8")
    )
    header_params = dict(params)
    header_params["oauth_signature"] = urllib.parse.quote_plus(base64.b64encode(signature).decode("ascii"))
    try:
        response = json_request(
            "POST",
            url,
            headers={"authorization": _authorization_header(header_params), "user-agent": IBKR_USER_AGENT},
            failure_message="IBKR live session token request failed.",
            invalid_response_message="IBKR live session token request returned an invalid response.",
        )
    except WebRequestError as exc:
        raise _mapped_web_error(exc, "live session token") from exc
    dh_response = response.get("diffie_hellman_response")
    lst_signature = response.get("live_session_token_signature")
    if not isinstance(dh_response, str) or not HEX_RE.fullmatch(dh_response) or not isinstance(lst_signature, str):
        raise RuntimeError("IBKR live session token request returned an invalid response.")
    shared_secret = pow(int(dh_response, 16), dh_random, material.dh_prime)
    live_session_token = hmac.new(_signed_big_endian(shared_secret), secret_bytes, hashlib.sha1).digest()
    expected = hmac.new(live_session_token, material.consumer_key.encode("utf-8"), hashlib.sha1).hexdigest()
    if not hmac.compare_digest(expected, lst_signature.lower()):
        raise RuntimeError("IBKR live session token failed verification. Check the OAuth config values.")
    return base64.b64encode(live_session_token).decode("ascii")


def _signed_request(
    material: _OAuthMaterial,
    live_session_token: str,
    method: str,
    path: str,
    *,
    query: Mapping[str, str] | None = None,
    json_body: JSONObject | None = None,
    what: str,
) -> JSONObject:
    url = f"{IBKR_API_BASE_URL}{path}"
    params = _oauth_params(material, "HMAC-SHA256")
    # Query parameters are part of the signature; JSON bodies are not.
    signed = {**params, **(query or {})}
    digest = hmac.new(
        base64.b64decode(live_session_token),
        _base_string(method, url, signed).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    header_params = dict(params)
    header_params["oauth_signature"] = urllib.parse.quote_plus(base64.b64encode(digest).decode("ascii"))
    full_url = f"{url}?{encode_query(query)}" if query else url
    try:
        return json_request(
            method,
            full_url,
            headers={"authorization": _authorization_header(header_params), "user-agent": IBKR_USER_AGENT},
            body=json_body,
            failure_message=f"IBKR {what} request failed.",
            invalid_response_message=f"IBKR {what} returned an invalid response.",
        )
    except WebRequestError as exc:
        raise _mapped_web_error(exc, what) from exc


def _account_id_input(tool_input: JSONObject) -> str | None:
    account_id = tool_input.get("account_id")
    if account_id is None:
        return None
    if not isinstance(account_id, str) or not ACCOUNT_ID_RE.fullmatch(account_id.strip()):
        raise ToolInputValidationError("IBKR tool_input.account_id must be an alphanumeric account id.")
    return account_id.strip()


def _resolve_account(material: _OAuthMaterial, live_session_token: str, requested: str | None) -> str:
    response = _signed_request(material, live_session_token, "GET", "/portfolio/accounts", what="accounts")
    items = response.get("items")
    account_ids: list[str] = []
    for entry in (items if isinstance(items, list) else [])[:MAX_ACCOUNTS]:
        if not isinstance(entry, dict):
            continue
        account_id = str(entry.get("accountId") or entry.get("id") or "")
        if ACCOUNT_ID_RE.fullmatch(account_id) and account_id not in account_ids:
            account_ids.append(account_id)
    if not account_ids:
        raise RuntimeError("IBKR returned no accounts for this login.")
    if requested is None:
        return account_ids[0]
    if requested not in account_ids:
        raise ToolInputValidationError(
            f"IBKR account {requested} was not found for this login (accounts: {', '.join(account_ids)})."
        )
    return requested


POSITION_FIELDS = (
    ("symbol", "ticker"),
    ("description", "contractDesc"),
    ("asset_category", "assetClass"),
    ("currency", "currency"),
    ("quantity", "position"),
    ("mark_price", "mktPrice"),
    ("position_value", "mktValue"),
    ("avg_cost", "avgCost"),
    ("unrealized_pnl", "unrealizedPnl"),
    ("realized_pnl", "realizedPnl"),
)

TRADE_FIELDS = (
    ("execution_id", "execution_id"),
    ("trade_time", "trade_time"),
    ("symbol", "symbol"),
    ("description", "order_description"),
    ("sec_type", "sec_type"),
    ("side", "side"),
    ("size", "size"),
    ("price", "price"),
    ("commission", "commission"),
    ("net_amount", "net_amount"),
    ("exchange", "exchange"),
    ("currency", "currency"),
)


def _picked(entry: JSONObject, fields: tuple[tuple[str, str], ...]) -> JSONObject:
    output: JSONObject = {}
    for output_key, source_key in fields:
        value = entry.get(source_key)
        output[output_key] = value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""
    return output


def _positions_result(material: _OAuthMaterial, live_session_token: str, account_id: str) -> JSONObject:
    response = _signed_request(
        material, live_session_token, "GET", f"/portfolio/{account_id}/positions/0", what="positions"
    )
    items = response.get("items")
    positions: list[JSONValue] = [
        _picked(entry, POSITION_FIELDS)
        for entry in (items if isinstance(items, list) else [])[:MAX_POSITIONS]
        if isinstance(entry, dict)
    ]
    return {
        "status": "success_executed",
        "message": f"IBKR returned {len(positions)} open position(s) for account {account_id} (live).",
        "account_id": account_id,
        "positions": positions,
    }


def _summary_result(material: _OAuthMaterial, live_session_token: str, account_id: str) -> JSONObject:
    response = _signed_request(
        material, live_session_token, "GET", f"/portfolio/{account_id}/summary", what="account summary"
    )
    summary: JSONObject = {}
    for key in SUMMARY_KEYS:
        row = response.get(key)
        if isinstance(row, dict):
            amount = row.get("amount")
            currency = row.get("currency")
            summary[key] = {
                "amount": amount if isinstance(amount, (int, float)) and not isinstance(amount, bool) else None,
                "currency": currency if isinstance(currency, str) else "",
            }
    return {
        "status": "success_executed",
        "message": f"IBKR account summary loaded for account {account_id} (live).",
        "account_id": account_id,
        "summary": summary,
    }


def _trades_result(
    material: _OAuthMaterial, live_session_token: str, account_id: str, tool_input: JSONObject
) -> JSONObject:
    days_value = tool_input.get("days")
    days = MAX_TRADE_DAYS
    if days_value is not None:
        if isinstance(days_value, str) and days_value.strip().isascii() and days_value.strip().isdecimal():
            digits = days_value.strip()
            if len(digits) > 2:
                raise ToolInputValidationError("IBKR tool_input.days must be between 1 and 7.")
            days_value = int(digits)
        if not isinstance(days_value, int) or isinstance(days_value, bool):
            raise ToolInputValidationError("IBKR tool_input.days must be an integer or digit string.")
        if not 1 <= days_value <= MAX_TRADE_DAYS:
            raise ToolInputValidationError("IBKR tool_input.days must be between 1 and 7.")
        days = days_value
    # Executed trades live behind /iserver, which needs a brokerage session on
    # top of the OAuth session; open one for this call. compete=False so this
    # read never force-closes the operator's own live session (TWS, Client
    # Portal): IBKR permits one brokerage session at a time, and compete=True
    # would silently disconnect the human every time the agent calls get_trades.
    init = _signed_request(
        material,
        live_session_token,
        "POST",
        "/iserver/auth/ssodh/init",
        json_body={"publish": True, "compete": False},
        what="brokerage session",
    )
    if init.get("competing") is True:
        raise RuntimeError(
            "IBKR already has another live brokerage session (for example TWS or Client Portal). "
            "Close it and try get_trades again; this read will not take over your session."
        )
    if init.get("authenticated") is not True:
        raise RuntimeError("IBKR could not open a brokerage session for trade data. Try the action again.")
    brokerage_accounts = _signed_request(
        material,
        live_session_token,
        "GET",
        "/iserver/accounts",
        what="brokerage accounts",
    )
    available = brokerage_accounts.get("accounts")
    if not isinstance(available, list) or account_id not in available:
        raise ToolInputValidationError(
            f"IBKR account {account_id} is not available to the brokerage session."
        )
    selected_account = brokerage_accounts.get("selectedAccount")
    if not isinstance(selected_account, str) or not selected_account:
        raise RuntimeError("IBKR did not report the selected brokerage account.")
    if selected_account != account_id:
        switched = _signed_request(
            material,
            live_session_token,
            "POST",
            "/iserver/account",
            json_body={"acctId": account_id},
            what="account selection",
        )
        if switched.get("set") is not True or switched.get("acctId") != account_id:
            raise RuntimeError(f"IBKR could not select brokerage account {account_id}.")
    response = _signed_request(
        material,
        live_session_token,
        "GET",
        "/iserver/account/trades",
        query={"days": str(days)},
        what="trades",
    )
    # The endpoint returns the selected account. Still require the account field
    # on every retained execution so an inconsistent provider response fails
    # closed instead of surfacing another account's trade under this label.
    items = response.get("items")
    matching_trades = [
        entry
        for entry in (items if isinstance(items, list) else [])
        if isinstance(entry, dict) and str(entry.get("account") or entry.get("accountCode") or "") == account_id
    ]
    trades: list[JSONValue] = [
        _picked(entry, TRADE_FIELDS)
        for entry in matching_trades[:MAX_TRADES]
    ]
    return {
        "status": "success_executed",
        "message": f"IBKR returned {len(trades)} trade(s) from the last {days} day(s) for account {account_id}.",
        "account_id": account_id,
        "trades": trades,
    }


class IBKRTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        if action not in {"get_positions", "get_account_summary", "get_trades"}:
            return ActionFailed("Unsupported IBKR action.")
        allowed = {"account_id", "days"} if action == "get_trades" else {"account_id"}
        if set(tool_input) - allowed:
            return ActionFailed(f"IBKR {action} tool input only supports {', '.join(sorted(allowed))}.")
        try:
            material = _oauth_material(api)
            live_session_token = _live_session_token(material)
            account_id = _resolve_account(material, live_session_token, _account_id_input(tool_input))
            if action == "get_positions":
                return ActionExecuted(_positions_result(material, live_session_token, account_id))
            if action == "get_account_summary":
                return ActionExecuted(_summary_result(material, live_session_token, account_id))
            return ActionExecuted(_trades_result(material, live_session_token, account_id, tool_input))
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except (ValueError, RuntimeError) as exc:
            # The tool's own errors (validation, config-unset, WebRequestError)
            # carry curated, secret-free messages. Anything else is unexpected
            # and must not leak its raw text to the agent.
            return ActionFailed(str(exc) or "IBKR tool request failed.")
        except Exception:
            return ActionFailed("IBKR tool request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        del approval, api
        return ActionFailed("The IBKR tool has no approval-gated actions.")


# The instance the host discovers (see host.runtime.tools.tools_host).
BUNDLED_TOOL = IBKRTool()
