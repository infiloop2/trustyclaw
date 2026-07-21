"""Outbound request parameter guard.

One host-owned check applied to every agent-controlled free-text parameter of
an automatic action bound for a public or third-party destination: tool call
sites receive it as ``HostAPI.outbound.guard_request_parameter_string(value)``,
and the network-integration guards run the same rules over decoded URL values.
The guard returns the value unchanged or denies the action with a descriptive,
value-free error; it never redacts or rewrites, because a silently modified
query is an action the caller did not request.

``docs/architecture/tools/outbound-request-filtering.md`` is the authoritative
specification: the guard ids (G1-G22), the data classes each protects, and the
false-positive/false-negative trade-offs all live there. This module keeps the
same ids in code comments so the two stay diffable.

Everything here is deterministic and offline: expressions compile at import,
the word data is a generated, checked-in module, and no rule fetches anything
at runtime.
"""

from __future__ import annotations

import math
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass

from host.param_guard_words import BIP39_WORDS, COMMON_WORDS

# Fixed limits: host constants, never caller-supplied (see the architecture
# doc's rejected alternatives for why there are no knobs at call sites).
MAX_PARAMETER_BYTES = 1024
MAX_UNBROKEN_TOKEN_CHARS = 128
MIN_BARE_DIGIT_RUN = 10
MAX_BARE_DIGIT_RUN = 16
MIN_UNNATURAL_TOKEN_CHARS = 14

REASON_TOO_LARGE = "request_param_too_large"
REASON_ENCODED_BLOB = "request_param_encoded_blob_denied"
REASON_SECRET = "request_param_secret_denied"
REASON_PII = "request_param_pii_denied"

# The one standard guide line for tools whose request parameters are guarded.
# Kept here so every Integration Guide renders the identical sentence and a
# wording change is a one-line diff.
PARAM_GUARD_PROTECTION = (
    "Request parameters pass the host parameter guard: values shaped like a "
    "secret, credential, or sensitive identifier are denied before the request "
    "is sent."
)

# The expanded guide description (Integration Guides technical details).
# Defined next to the short line so the two cannot drift apart.
PARAM_GUARD_TECHNICAL_DETAIL = (
    "Parameter guard: free-text request parameters sent without operator "
    "approval are limited to 1,024 bytes and checked against deterministic "
    "rules for secrets, credentials, personal and financial identifiers, and "
    "encoded or random-looking payloads; a match denies the action before "
    "anything is sent."
)


@dataclass(frozen=True)
class GuardDenial:
    """One denial: which guard fired, the reason category, and an agent-safe,
    actionable message. Never contains the value or the matched span."""

    guard: str
    reason: str
    message: str


class ParamGuardDenied(ValueError):
    """Raised by guard_request_parameter_string; str(err) is agent-visible."""

    def __init__(self, denial: GuardDenial) -> None:
        super().__init__(denial.message)
        self.denial = denial


def guard_request_parameter_string(value: str, *, allow_identifiers: bool = False) -> str:
    """Apply the standard guard set; return ``value`` unchanged or raise.

    The error message is descriptive so the agent can rephrase and retry, and
    it never echoes the offending value.

    ``allow_identifiers=True`` skips the personal-identifier guards (email, phone,
    card, digit runs, etc.) while still denying secret/credential shapes and
    encoded payloads. Use it only for a query against an account the operator
    already connected (a mailbox search), where a personal identifier is
    legitimate search syntax (``from:alice@example.com``) and the destination
    already holds that data, but a credential from another context still does
    not belong in the query.
    """
    denial = find_denial(value, allow_identifiers=allow_identifiers)
    if denial is not None:
        raise ParamGuardDenied(denial)
    return value



def find_denial(
    value: str, *, token_rules: bool = True, allow_identifiers: bool = False
) -> GuardDenial | None:
    """Run the guards over one decoded value and return the first denial.

    ``token_rules=False`` (the GitHub read path) disables the two token-shape
    guards - the unbroken-token rule and the unnatural-token score - because
    revision ids, blob shas, and ref names are legitimately long and
    machine-shaped there.

    ``allow_identifiers=True`` (connected-account mailbox queries) skips the
    personal-identifier guards, because a personal identifier is legitimate
    search syntax against an account the operator already owns, while still
    denying secret/credential shapes and encoded payloads.
    """
    # G1 LENGTH - the floor that works when every other guard misses. Lone
    # surrogates (JSON escapes can produce them) cannot encode; treat them
    # as the non-text characters they are rather than crashing.
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return GuardDenial(
            "PRINTABLE",
            REASON_ENCODED_BLOB,
            "The value contains invalid text (unpaired surrogate characters). "
            "Resend it as plain text.",
        )
    if encoded_length > MAX_PARAMETER_BYTES:
        return GuardDenial(
            "LENGTH",
            REASON_TOO_LARGE,
            f"The value is longer than the {MAX_PARAMETER_BYTES}-byte limit for "
            "request parameters. Shorten it and retry.",
        )
    # G2 PRINTABLE - character-level rule; every later guard sees clean text.
    if _has_non_text_character(value):
        return GuardDenial(
            "PRINTABLE",
            REASON_ENCODED_BLOB,
            "The value contains control or invisible characters. Resend it as "
            "plain text.",
        )
    tokens = value.split()
    # G3 TOKEN_RUN - anti-smuggling: no legitimate query or prompt needs an
    # unbroken token this long, but base64/hex payloads do.
    if token_rules:
        for token in tokens:
            if len(token) > MAX_UNBROKEN_TOKEN_CHARS and not _is_plain_https_url(token):
                return GuardDenial(
                    "TOKEN_RUN",
                    REASON_ENCODED_BLOB,
                    "The value contains an unbroken run of more than "
                    f"{MAX_UNBROKEN_TOKEN_CHARS} characters, which looks like an "
                    "encoded payload. Remove it and retry.",
                )
    return _find_pattern_denial(
        value, tokens, token_rules=token_rules, allow_identifiers=allow_identifiers
    )


# Public wallet addresses are exempt: they are broadcast-public identifiers
# and crypto discovery queries legitimately carry them, including the
# operator's own. Address-shaped tokens are replaced with a neutral word
# before pattern scanning so neither the gibberish rule nor a digit run
# inside the address denies them. The cost is that ~20-30 bytes can be
# smuggled as a fake address; accepted.
_PUBLIC_ADDRESS_RES = (
    re.compile(r"^0x[0-9a-fA-F]{40}$"),  # Ethereum
    re.compile(r"^[13][1-9A-HJ-NP-Za-km-z]{25,34}$"),  # Bitcoin P2PKH/P2SH
    re.compile(r"^bc1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{6,87}$"),  # Bitcoin bech32
)
_TOKEN_EDGE_PUNCT = ".,;:!?()[]{}\"'"


_ADDRESS_PREFILTER_RE = re.compile(
    r"0x[0-9a-fA-F]{6}|bc1[0-9a-z]{6}|[13][1-9A-HJ-NP-Za-km-z]{25}"
)


def _mask_public_addresses(value: str, tokens: list[str]) -> tuple[str, list[str]]:
    # Most values contain no wallet address; a single prefilter scan avoids the
    # per-token pattern loop for them.
    if not _ADDRESS_PREFILTER_RE.search(value):
        return value, tokens
    masked_tokens: list[str] = []
    masked_value = value
    for token in tokens:
        core = token.strip(_TOKEN_EDGE_PUNCT)
        if core and any(pattern.match(core) for pattern in _PUBLIC_ADDRESS_RES):
            masked_tokens.append(token.replace(core, "walletaddress"))
            masked_value = masked_value.replace(core, "walletaddress")
        else:
            masked_tokens.append(token)
    return masked_value, masked_tokens


def _find_pattern_denial(
    value: str, tokens: list[str], *, token_rules: bool, allow_identifiers: bool
) -> GuardDenial | None:
    value, tokens = _mask_public_addresses(value, tokens)
    # --- Secret-shaped values -------------------------------------------
    # G5 CRED_PREFIX
    if _CRED_PREFIX_RE.search(value):
        return _secret("CRED_PREFIX", "a provider API credential")
    # G6 KEY_BLOCK
    if _KEY_BLOCK_RE.search(value):
        return _secret("KEY_BLOCK", "a private-key block")
    # G7 JWT
    if _JWT_RE.search(value):
        return _secret("JWT", "a signed token (JWT)")
    # G8 SEED_PHRASE
    if _has_seed_phrase(tokens):
        return _secret("SEED_PHRASE", "a crypto wallet seed phrase")
    # G9 CRYPTO_KEY
    if _HEX64_RE.search(value) or _WIF_RE.search(value):
        return _secret("CRYPTO_KEY", "a raw cryptographic key")
    # G10 CRED_URL
    if _has_credential_url(tokens):
        return _secret("CRED_URL", "a URL embedding credentials or a token")
    # G18 ENTROPY_NEAR_KEYWORD
    if _has_entropy_near_keyword(value):
        return _secret("ENTROPY_NEAR_KEYWORD", "an API key or token")
    # G19 PASSWORD_KEYWORD
    if _PASSWORD_RE.search(value):
        return _secret("PASSWORD_KEYWORD", "a password")

    # --- Structured personal/financial identifiers ----------------------
    # Skipped for connected-account queries (allow_identifiers): an email address or
    # phone number is legitimate mailbox-search syntax against an account the
    # operator already owns.
    if not allow_identifiers:
        # G16 EMAIL
        if _EMAIL_RE.search(value):
            return _pii("EMAIL", "an email address")
        # G12 CARD (before the generic digit run so the message is specific)
        if _has_payment_card(value):
            return _pii("CARD", "a payment card number")
        # G15 IBAN
        if _has_iban(value):
            return _pii("IBAN", "a bank account number (IBAN)")
        # G14 ABA
        if _has_aba_routing(value):
            return _pii("ABA", "a bank routing number")
        # G13 SSN
        if _has_ssn(value):
            return _pii("SSN", "a Social Security number")
        # G17 PHONE
        if _has_phone(value):
            return _pii("PHONE", "a phone number")
        # G20 OTP_KEYWORD
        if _OTP_NEAR_KEYWORD_RE.search(value):
            return _pii("OTP_KEYWORD", "a one-time or verification code")
        # G21 DOB_KEYWORD
        if _DOB_NEAR_KEYWORD_RE.search(value):
            return _pii("DOB_KEYWORD", "a date of birth")
        # G22 GOV_ID_KEYWORD
        if _GOV_ID_NEAR_KEYWORD_RE.search(value):
            return _pii("GOV_ID_KEYWORD", "a government identity document number")
        # G11 DIGIT_RUN - bare digit runs with no keyword context.
        if _DIGIT_RUN_RE.search(value):
            return _pii("DIGIT_RUN", f"an unbroken run of {MIN_BARE_DIGIT_RUN}-{MAX_BARE_DIGIT_RUN} digits (phone-, card-, or account-number length)")
    # G5 UNNATURAL_TOKEN - scored gibberish check, run last in code because
    # everything with a more specific shape should be named first.
    if token_rules and _has_unnatural_token(tokens):
        return GuardDenial(
            "UNNATURAL_TOKEN",
            REASON_ENCODED_BLOB,
            "The value contains a random-looking token that resembles an "
            "encoded secret or identifier. Remove it or rewrite it as natural "
            "text and retry.",
        )
    return None


def _secret(guard: str, what: str) -> GuardDenial:
    return GuardDenial(
        guard,
        REASON_SECRET,
        f"The value appears to contain {what}, which must not be sent in a "
        "request parameter. Remove it and retry.",
    )


def _pii(guard: str, what: str) -> GuardDenial:
    return GuardDenial(
        guard,
        REASON_PII,
        f"The value appears to contain {what}, which must not be sent in a "
        "request parameter. Remove it and retry.",
    )


# --- G2 helpers ---------------------------------------------------------

# ASCII control characters except tab/newline/CR. One compiled class runs at
# C speed for the common all-ASCII case; unicodedata is consulted only for the
# actual non-ASCII code points (rare in request parameters).
_ASCII_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _has_non_text_character(value: str) -> bool:
    if _ASCII_CONTROL_RE.search(value):
        return True
    if value.isascii():
        return False
    return any(
        char >= "\x80" and unicodedata.category(char).startswith("C") for char in value
    )


# --- G3 helper ----------------------------------------------------------


def _is_plain_https_url(token: str) -> bool:
    if not token.startswith("https://"):
        return False
    try:
        parsed = urllib.parse.urlsplit(token)
    except ValueError:
        return False
    return bool(parsed.hostname) and "@" not in parsed.netloc


# --- Secret rules -------------------------------------------------------

# G5: known provider credential prefixes, adapted from the MIT-licensed
# Gitleaks rule set (v8 defaults). Deliberately a subset: prefixes distinctive
# enough that a match in a request parameter is near-certain to be a secret.
_CRED_PREFIX_RE = re.compile(
    r"""
    \b(?:
        (?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}        # AWS access key id
      | ghp_[A-Za-z0-9]{36,}                                    # GitHub PAT
      | gh[ours]_[A-Za-z0-9]{36,}                               # GitHub app tokens
      | github_pat_[A-Za-z0-9_]{22,}                            # GitHub fine-grained
      | glpat-[A-Za-z0-9_-]{20,}                                # GitLab PAT
      | xox[baprs]-[A-Za-z0-9-]{10,}                            # Slack tokens
      | sk-(?:proj-|ant-|live-)?[A-Za-z0-9_-]{20,}              # OpenAI/Anthropic/Stripe-style
      | [rp]k_live_[A-Za-z0-9]{20,}                             # Stripe live keys
      | AIza[0-9A-Za-z_-]{35}                                   # Google API key
      | ya29\.[0-9A-Za-z_-]{20,}                                # Google OAuth access token
      | SG\.[A-Za-z0-9_-]{16,32}\.[A-Za-z0-9_-]{16,64}          # SendGrid
      | npm_[A-Za-z0-9]{36}                                     # npm token
      | pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{20,}                  # PyPI token
      | dop_v1_[a-f0-9]{64}                                     # DigitalOcean PAT
      | hf_[A-Za-z0-9]{30,}                                     # Hugging Face
      | AGE-SECRET-KEY-1[QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L]{58}  # age
    )\b
    """,
    re.VERBOSE,
)

# G6: PEM/OpenSSH/PGP private key block markers.
_KEY_BLOCK_RE = re.compile(r"-----BEGIN [A-Z0-9 ]{0,32}PRIVATE KEY( BLOCK)?-----")

# G7: three dot-separated base64url segments with a JSON-object first segment
# ("eyJ" is base64 for '{"'). Segment lengths bounded to keep matching cheap.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,4096}\b")

# G9: raw key material by shape - 64 hex chars is the exact length of an
# Ethereum/ed25519 private key; WIF is Bitcoin's key-export format. All
# printable characters, hence a shape rule G2 can never cover.
_HEX64_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
_WIF_RE = re.compile(r"\b[5KL][1-9A-HJ-NP-Za-km-z]{50,51}\b")

_SEED_PHRASE_RUN = 12


def _has_seed_phrase(tokens: list[str]) -> bool:
    # G8: 12+ consecutive BIP-39 words. Function words ("the", "of", "was")
    # are not in the BIP-39 list, so ordinary prose breaks the run quickly.
    run = 0
    for token in tokens:
        if token.lower() in BIP39_WORDS:
            run += 1
            if run >= _SEED_PHRASE_RUN:
                return True
        else:
            run = 0
    return False


# Query keys that name a credential. One explicit list, shared by the
# CRED_URL guard (URLs inside tool parameters) and the proxy pair check
# (which sees keys and values separately): a 16+ char value under one of
# these keys is a transmitted secret regardless of the value's shape.
CREDENTIAL_QUERY_KEYS: frozenset[str] = frozenset({
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "bearer",
    "credential",
    "key",
    "password",
    "secret",
    "sig",
    "signature",
    "token",
})


def is_credential_query_key(key: str) -> bool:
    """Whether a query key names a credential (case-insensitive exact match
    against CREDENTIAL_QUERY_KEYS)."""
    return key.lower() in CREDENTIAL_QUERY_KEYS


def _has_credential_url(tokens: list[str]) -> bool:
    # G10: userinfo in a URL, or a long value under a credential-named query key.
    for token in tokens:
        if "://" not in token:
            continue
        try:
            parsed = urllib.parse.urlsplit(token)
        except ValueError:
            return True  # unparseable URL-shaped token: treat as suspect
        if parsed.hostname and "@" in parsed.netloc:
            return True
        for key, val in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            if is_credential_query_key(key) and len(val) >= 16:
                return True
    return False


# G18: a high-entropy token near a credential keyword. Entropy alone is never
# a denial (hashes and cursors are normal traffic); the keyword supplies the
# context that makes the signal precise.
_CRED_KEYWORD_RE = re.compile(
    r"(?i)\b(?:api[ _-]?key|access[ _-]?key|secret|token|bearer|credential|private[ _-]?key)\b"
)
_ENTROPY_CANDIDATE_RE = re.compile(r"[A-Za-z0-9+/=_-]{16,}")
_ENTROPY_WINDOW_CHARS = 40
_ENTROPY_MIN_BITS_PER_CHAR = 3.2


def _shannon_bits_per_char(token: str) -> float:
    counts: dict[str, int] = {}
    for char in token:
        counts[char] = counts.get(char, 0) + 1
    total = len(token)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _has_entropy_near_keyword(value: str) -> bool:
    keyword_spans = [match.span() for match in _CRED_KEYWORD_RE.finditer(value)]
    if not keyword_spans:
        return False
    for match in _ENTROPY_CANDIDATE_RE.finditer(value):
        start, end = match.span()
        near = any(
            start <= k_end + _ENTROPY_WINDOW_CHARS and k_start <= end + _ENTROPY_WINDOW_CHARS
            for k_start, k_end in keyword_spans
        )
        if near and _shannon_bits_per_char(match.group()) >= _ENTROPY_MIN_BITS_PER_CHAR:
            return True
    return False


# G19: a value explicitly introduced as a password. The connective ("is", ":",
# "=") is required so "how to change password on iphone" stays a valid query.
_PASSWORD_RE = re.compile(r"(?i)\b(?:password|passwd|pwd)\b\s*(?:is|:|=)\s*\S{4,}")


# --- Identifier rules ---------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")

# G12: 13-19 digits with optional single space/dash separators, then Luhn and
# an issuer-prefix check. Luhn alone accepts ~1 in 10 arbitrary digit runs and
# bundled-provider numeric ids are everywhere, so the issuer check and the
# provider-id negative corpus in the tests are load-bearing.
_CARD_CANDIDATE_RE = re.compile(r"(?<![\dA-Za-z])\d(?:[ -]?\d){12,18}(?![\dA-Za-z])")
_CARD_ISSUER_RE = re.compile(
    r"^(?:4\d{12,18}"  # Visa
    r"|5[1-5]\d{14}|2(?:22[1-9]|2[3-9]\d|[3-6]\d{2}|7[01]\d|720)\d{12}"  # Mastercard
    r"|3[47]\d{13}"  # Amex
    r"|6(?:011|5\d{2}|4[4-9]\d)\d{12}"  # Discover
    r"|35(?:2[89]|[3-8]\d)\d{12}"  # JCB
    r"|3(?:0[0-5]|[68]\d)\d{11}"  # Diners
    r"|62\d{14,17}"  # UnionPay
    r")$"
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = ord(char) - 48
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _has_payment_card(value: str) -> bool:
    for match in _CARD_CANDIDATE_RE.finditer(value):
        digits = re.sub(r"[ -]", "", match.group())
        if 13 <= len(digits) <= 19 and _CARD_ISSUER_RE.match(digits) and _luhn_ok(digits):
            return True
    return False


# G15: IBAN with country-specific length and the ISO 7064 mod-97 check.
# Body groups are uppercase/digits only so a trailing lowercase word is not
# swallowed into the candidate.
_IBAN_CANDIDATE_RE = re.compile(r"\b([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{1,4}){3,8})\b")
_IBAN_LENGTHS = {
    "AT": 20, "BE": 16, "CH": 21, "CZ": 24, "DE": 22, "DK": 18, "ES": 24,
    "FI": 18, "FR": 27, "GB": 22, "IE": 22, "IT": 27, "LU": 20, "NL": 18,
    "NO": 15, "PL": 28, "PT": 25, "SE": 24,
}


def _has_iban(value: str) -> bool:
    for match in _IBAN_CANDIDATE_RE.finditer(value):
        compact = match.group(1).replace(" ", "").upper()
        expected = _IBAN_LENGTHS.get(compact[:2])
        if expected is None or len(compact) < expected:
            continue
        compact = compact[:expected]
        rearranged = compact[4:] + compact[:4]
        numeric = "".join(str(int(c, 36)) for c in rearranged)
        if int(numeric) % 97 == 1:
            return True
    return False


# G14: 9-digit runs passing the ABA checksum with a valid Federal Reserve
# district prefix.
_NINE_DIGIT_RE = re.compile(r"(?<!\d)(\d{9})(?!\d)")


def _has_aba_routing(value: str) -> bool:
    for match in _NINE_DIGIT_RE.finditer(value):
        digits = match.group(1)
        prefix = int(digits[:2])
        if not (0 <= prefix <= 12 or 21 <= prefix <= 32 or 61 <= prefix <= 72 or prefix == 80):
            continue
        d = [ord(c) - 48 for c in digits]
        checksum = 3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])
        if checksum % 10 == 0:
            return True
    return False


# G13: formatted US SSN after invalid-range filtering (bare nine-digit runs
# are covered by the digit-run rule).
_SSN_RE = re.compile(r"\b(\d{3})-(\d{2})-(\d{4})\b")


def _has_ssn(value: str) -> bool:
    for match in _SSN_RE.finditer(value):
        area, group, serial = match.groups()
        if area in ("000", "666") or area.startswith("9"):
            continue
        if group == "00" or serial == "0000":
            continue
        return True
    return False


# G17: international (+ prefix) and separator-formatted phone candidates with
# plausible digit counts. Bare digit runs fall to the digit-run rule.
_PHONE_INTL_RE = re.compile(r"(?<![\dA-Za-z])\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}(?![\dA-Za-z])")
_PHONE_SEP_RE = re.compile(r"(?<![\dA-Za-z])\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\dA-Za-z])")


def _has_phone(value: str) -> bool:
    for pattern in (_PHONE_INTL_RE, _PHONE_SEP_RE):
        for match in pattern.finditer(value):
            digit_count = sum(char.isdigit() for char in match.group())
            if 8 <= digit_count <= 15:
                return True
    return False


# G20: 4-8 digits near a code keyword. With the keyword present the threshold
# safely reaches below the bare digit-run floor ("PIN 1234").
# "zip code 90210" and friends are location/status phrases, not one-time
# codes; each fixed-width lookbehind excludes one such qualifier.
_OTP_NEAR_KEYWORD_RE = re.compile(
    r"(?i)\b"
    r"(?:(?<!zip )(?<!area )(?<!postal )(?<!country )(?<!dial )(?<!error )"
    r"(?<!status )(?<!http )code|otp|verification|verify|2fa|passcode|pin|one[ -]?time)\b"
    r"[^\d\n]{0,24}(?<!\d)\d{4,8}(?!\d)"
)

# G21: a date shape near a birth keyword; plain dates stay allowed.
_DOB_NEAR_KEYWORD_RE = re.compile(
    r"(?i)\b(?:dob|date of birth|born|birth\s?date|birthday)\b"
    r"[^\n]{0,24}?(?:\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2})"
)

# G22: an id-shaped token near a document keyword. Formats vary too much for
# a bare shape, so this stays keyword-adjacent (recall is deliberately partial).
_GOV_ID_NEAR_KEYWORD_RE = re.compile(
    r"(?i)\b(?:passport|driver'?s?\s?licen[cs]e|licen[cs]e\s?(?:number|no\.?)|national\s?id|aadhaar)\b"
    r"[^\n]{0,24}?\b(?=[A-Z0-9-]{5,20}\b)[A-Z0-9-]*\d[A-Z0-9-]*\b"
)

# G12: unbroken bare digit runs of 10-16 digits - the window where phones,
# cards, and account numbers live. Shorter runs (codes, dates, order numbers)
# and longer runs (64-bit provider ids: tweet/Discord/Meta ids) are allowed;
# separator-formatted financial numbers are caught by the checksummed rules,
# and valid 17-19 digit cards still fail Luhn+issuer first.
_DIGIT_RUN_RE = re.compile(r"(?<!\d)\d{10,16}(?!\d)")


# --- G4 UNNATURAL_TOKEN ------------------------------------------------
# Scored gibberish check. Raw Shannon entropy is meaningless on short tokens,
# so this uses discrete signals that each kill a false-positive class, and
# denies only when three agree. Tuned against the handles/brands corpus in
# tests/test_param_guard.py.

_ALNUM_RUN_RE = re.compile(rf"[A-Za-z0-9]{{{MIN_UNNATURAL_TOKEN_CHARS},}}")
_UNNATURAL_SCORE_THRESHOLD = 3
_BIGRAM_LOGPROB_THRESHOLD = -2.7


def _build_bigram_logprobs() -> dict[str, float]:
    counts: dict[str, int] = {}
    total = 0
    for word in COMMON_WORDS:
        for pair in zip(word, word[1:]):
            bigram = pair[0] + pair[1]
            counts[bigram] = counts.get(bigram, 0) + 1
            total += 1
    # Add-one smoothing over the full 26x26 space so unseen bigrams get a
    # finite, strongly negative log-probability.
    smoothed_total = total + 26 * 26
    floor = math.log10(1 / smoothed_total)
    table = {bigram: math.log10((count + 1) / smoothed_total) for bigram, count in counts.items()}
    table["__floor__"] = floor
    return table


_BIGRAM_LOGPROBS = _build_bigram_logprobs()


def _mean_bigram_logprob(token: str) -> float | None:
    letters = re.sub(r"[^a-z]", " ", token.lower())
    logprobs: list[float] = []
    floor = _BIGRAM_LOGPROBS["__floor__"]
    for word in letters.split():
        for pair in zip(word, word[1:]):
            logprobs.append(_BIGRAM_LOGPROBS.get(pair[0] + pair[1], floor))
    if len(logprobs) < 3:
        return None
    return sum(logprobs) / len(logprobs)


def _transition_blocks(token: str) -> int:
    transitions = 0
    previous_is_digit: bool | None = None
    for char in token:
        is_digit = char.isdigit()
        if previous_is_digit is not None and is_digit != previous_is_digit:
            transitions += 1
        previous_is_digit = is_digit
    return transitions


def _has_dictionary_segment(token: str) -> bool:
    lowered = token.lower()
    for start in range(len(lowered)):
        for length in range(4, 9):
            segment = lowered[start : start + length]
            if len(segment) < 4:
                break
            if segment in COMMON_WORDS:
                return True
    return False


def _has_unnatural_token(tokens: list[str]) -> bool:
    for token in tokens:
        for match in _ALNUM_RUN_RE.finditer(token):
            run = match.group()
            score = 0
            if len(run) >= 16:
                score += 1
            if _transition_blocks(run) >= 2:
                score += 1
            mean_logprob = _mean_bigram_logprob(run)
            if mean_logprob is not None and mean_logprob < _BIGRAM_LOGPROB_THRESHOLD:
                score += 1
            classes = sum(
                1
                for predicate in (str.islower, str.isupper, str.isdigit)
                if any(predicate(char) for char in run)
            )
            if classes >= 3:
                score += 1
            if not _has_dictionary_segment(run):
                score += 1
            if score >= _UNNATURAL_SCORE_THRESHOLD:
                return True
    return False


class OutboundGuardService:
    """The concrete ``HostAPI.outbound`` service.

    Stateless by construction, so the runtime and test hosts share one
    implementation and tools exercise the real guard everywhere.
    """

    def guard_request_parameter_string(self, value: str, *, allow_identifiers: bool = False) -> str:
        return guard_request_parameter_string(value, allow_identifiers=allow_identifiers)
